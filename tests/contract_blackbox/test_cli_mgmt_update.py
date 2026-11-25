"""P4-T12 `setup` 更新與 `setup --rollback` 黑箱契約（SC-23）。

規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 12；所有 tty 互動經隔離 P2 bridge。
"""
from __future__ import annotations

import ctypes
import json
import os
import select
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bb_harness import isolated_env, repo_root
from bb_l15 import (
    OLD_BUNDLE_VERSION,
    NEW_BUNDLE_VERSION,
    BridgeTarget,
    RaisingThread,
    assert_no_tx_after,
    current_release_path,
    daemon_pid,
    finish_test_rig,
    installed_version,
    json_stdout,
    make_push_file,
    prepare_profile,
    resume_pause,
    rpc,
    run_cli,
    select_bundle,
    start_file_push,
    start_python_daemon,
    stop_current_daemon,
    stop_owned_python_daemon,
    make_install_root as setup_install_root,
    tx_bytes,
    update_bundles,
    wait_pause,
    wait_rx_contains,
    wait_session,
    wait_wire_contains,
    wal_rows,
)

pytestmark = [pytest.mark.posix_only]

# busy 案例的 push 大小（依 ACK-gated 路徑實測吞吐決定，見該案例 docstring）。
BUSY_PUSH_BYTES = 16 * 1024
# 首個 chunk 的 ACK 延遲（毫秒）：須大於 setup 被拒所需時間。量測（WSL2 bridge）：in-flight push 期間
# `setup --on-demand` 被拒約 7.2 s；取 20 s（餘裕約 13 s）。
# 單一 chunk 等待逾時須大於此延遲，否則 CLI 在 ACK 到來前已逾時並送出清理指令（`SW_XFER_CLEANUP_`），
# 該 TX 會落在拒絕視窗內、破壞零 TX 斷言；故另以 `chunk_timeout_s` 放寬（見 BUSY_CHUNK_TIMEOUT_S）。
BUSY_FIRST_ACK_DELAY_MS = 20000
BUSY_CHUNK_TIMEOUT_S = BUSY_FIRST_ACK_DELAY_MS / 1000 + 40
# 交接排空窗口長度（sw_core/daemon.py `HANDOFF_DRAIN_GRACE_S`）；黑箱不得 import sw_core，故於此重述。
HANDOFF_DRAIN_GRACE_S = 3.0

_BY_ID_SCAN_PRELOAD_C = r"""
#define _GNU_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static __thread DIR *tracked_dir;
static __thread int tracked_link_present;
static __thread uint64_t tracked_handle;
static int marker_delay_applied;

static uint64_t monotonic_ns(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0;
    }
    return (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
}

static void trace_event(const char *phase, int link_present, uint64_t event_ns, uint64_t handle) {
    const char *path = getenv("SW_TEST_BY_ID_SCAN_TRACE");
    if (!path || event_ns == 0) {
        return;
    }
    char row[512];
    int length = snprintf(
        row,
        sizeof(row),
        "%s\t%ld\t%ld\t%llu\t%d\t%llu\n",
        phase,
        (long)getpid(),
        (long)syscall(SYS_gettid),
        (unsigned long long)event_ns,
        link_present,
        (unsigned long long)handle
    );
    if (length <= 0 || length >= (int)sizeof(row)) {
        return;
    }
    int fd = (int)syscall(SYS_openat, AT_FDCWD, path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
    if (fd >= 0) {
        (void)syscall(SYS_write, fd, row, (size_t)length);
        (void)syscall(SYS_close, fd);
    }
}

static void trace_handoff_request(const void *buffer, size_t length) {
    static const char method[] = "daemon.handoff_stop";
    if (buffer && memmem(buffer, length, method, sizeof(method) - 1)) {
        trace_event("handoff_request", 0, monotonic_ns(), 0);
    }
}

static int flags_need_mode(int flags) {
    int needed = (flags & O_CREAT) != 0;
#ifdef O_TMPFILE
    needed = needed || (flags & O_TMPFILE) == O_TMPFILE;
#endif
    return needed;
}

static void delay_handoff_reached_marker(const char *pathname) {
    const char *pause_dir = getenv("SW_TEST_UPDATE_PAUSE_DIR");
    const char *delay_value = getenv("SW_TEST_MARKER_WRITE_DELAY_MS");
    const char marker[] = "handoff_stop_replied.reached";
    if (!pause_dir || !delay_value || !pathname || marker_delay_applied) {
        return;
    }
    size_t pause_len = strlen(pause_dir);
    if (strncmp(pathname, pause_dir, pause_len) != 0 || pathname[pause_len] != '/') {
        return;
    }
    const char *base = strrchr(pathname, '/');
    if (!base || strcmp(base + 1, marker) != 0) {
        return;
    }
    char *end = NULL;
    long delay_ms = strtol(delay_value, &end, 10);
    if (!end || *end || delay_ms <= 0 || delay_ms > 10000) {
        return;
    }
    if (__sync_lock_test_and_set(&marker_delay_applied, 1)) {
        return;
    }
    struct timespec remaining = {
        .tv_sec = delay_ms / 1000,
        .tv_nsec = (delay_ms % 1000) * 1000000L,
    };
    while (nanosleep(&remaining, &remaining) != 0) {
        if (errno != EINTR) {
            break;
        }
    }
    trace_event("marker_delay", 0, monotonic_ns(), 0);
}

int open(const char *pathname, int flags, ...) {
    static int (*real_open)(const char *, int, ...);
    if (!real_open) {
        real_open = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open");
    }
    mode_t mode = 0;
    int has_mode = flags_need_mode(flags);
    if (has_mode) {
        va_list args;
        va_start(args, flags);
        mode = (mode_t)va_arg(args, int);
        va_end(args);
    }
    delay_handoff_reached_marker(pathname);
    return has_mode ? real_open(pathname, flags, mode) : real_open(pathname, flags);
}

int open64(const char *pathname, int flags, ...) {
    static int (*real_open64)(const char *, int, ...);
    if (!real_open64) {
        real_open64 = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open64");
    }
    mode_t mode = 0;
    int has_mode = flags_need_mode(flags);
    if (has_mode) {
        va_list args;
        va_start(args, flags);
        mode = (mode_t)va_arg(args, int);
        va_end(args);
    }
    delay_handoff_reached_marker(pathname);
    return has_mode ? real_open64(pathname, flags, mode) : real_open64(pathname, flags);
}

int openat(int dirfd, const char *pathname, int flags, ...) {
    static int (*real_openat)(int, const char *, int, ...);
    if (!real_openat) {
        real_openat = (int (*)(int, const char *, int, ...))dlsym(RTLD_NEXT, "openat");
    }
    mode_t mode = 0;
    int has_mode = flags_need_mode(flags);
    if (has_mode) {
        va_list args;
        va_start(args, flags);
        mode = (mode_t)va_arg(args, int);
        va_end(args);
    }
    delay_handoff_reached_marker(pathname);
    return has_mode ? real_openat(dirfd, pathname, flags, mode) : real_openat(dirfd, pathname, flags);
}

ssize_t write(int fd, const void *buffer, size_t count) {
    static ssize_t (*real_write)(int, const void *, size_t);
    if (!real_write) {
        real_write = (ssize_t (*)(int, const void *, size_t))dlsym(RTLD_NEXT, "write");
    }
    trace_handoff_request(buffer, count);
    return real_write(fd, buffer, count);
}

ssize_t send(int fd, const void *buffer, size_t length, int flags) {
    static ssize_t (*real_send)(int, const void *, size_t, int);
    if (!real_send) {
        real_send = (ssize_t (*)(int, const void *, size_t, int))dlsym(RTLD_NEXT, "send");
    }
    trace_handoff_request(buffer, length);
    return real_send(fd, buffer, length, flags);
}

DIR *opendir(const char *name) {
    static DIR *(*real_opendir)(const char *);
    if (!real_opendir) {
        real_opendir = (DIR *(*)(const char *))dlsym(RTLD_NEXT, "opendir");
    }
    const char *watched = getenv("SW_TEST_BY_ID_SCAN_DIR");
    int should_track = watched && strcmp(name, watched) == 0;
    uint64_t open_started_ns = should_track ? monotonic_ns() : 0;
    DIR *result = real_opendir(name);
    if (result && should_track) {
        const char *link = getenv("SW_TEST_BY_ID_SCAN_LINK");
        struct stat stat_result;
        tracked_dir = result;
        tracked_link_present = link && lstat(link, &stat_result) == 0;
        tracked_handle = (uint64_t)(uintptr_t)result;
        trace_event("open", tracked_link_present, open_started_ns, tracked_handle);
    }
    return result;
}

int closedir(DIR *directory) {
    static int (*real_closedir)(DIR *);
    if (!real_closedir) {
        real_closedir = (int (*)(DIR *))dlsym(RTLD_NEXT, "closedir");
    }
    if (tracked_dir == directory) {
        trace_event("close", tracked_link_present, monotonic_ns(), tracked_handle);
        tracked_dir = NULL;
        tracked_handle = 0;
    }
    return real_closedir(directory);
}
"""


class _ByIdScanObserver:
    """由 test preload 的 daemon-local trace 辨識特定 PID／TID 的 by-id 目錄掃描。"""

    _EVENT = struct.Struct("iIII")
    _IN_OPEN = 0x00000020
    _IN_CLOSE_NOWRITE = 0x00000010
    _IN_CREATE = 0x00000100
    _IN_DELETE = 0x00000200
    _IN_ONLYDIR = 0x01000000
    _IN_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
    _IN_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
    _IN_Q_OVERFLOW = 0x00004000

    def __init__(
        self,
        directory: Path,
        link_name: str,
        *,
        process_pid: int | None = None,
        thread_name: str | None = None,
        scan_trace: Path | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.link_name = link_name
        self.process_pid = process_pid if process_pid is not None else os.getpid()
        self.thread_name = thread_name
        self.scan_trace = Path(scan_trace) if scan_trace is not None else None
        self._trace_offset = 0
        self._trace_pending = b""
        self._process_task: Path | None = None
        self.events: list[dict[str, int | str | bool | None]] = []
        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._barriers_requested = 0
        self._barriers_handled = 0
        self._sampler_barriers_handled = 0
        self._sampler_holds_directory = False
        self._reader_error: BaseException | None = None
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_init1.restype = ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        self._fd = self._libc.inotify_init1(self._IN_CLOEXEC | self._IN_NONBLOCK)
        if self._fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), "inotify_init1")
        mask = self._IN_OPEN | self._IN_CLOSE_NOWRITE | self._IN_CREATE | self._IN_DELETE | self._IN_ONLYDIR
        self._watch = self._libc.inotify_add_watch(self._fd, os.fsencode(self.directory), mask)
        if self._watch < 0:
            error = ctypes.get_errno()
            os.close(self._fd)
            raise OSError(error, os.strerror(error), str(self.directory))
        self._control_read, self._control_write = os.pipe2(self._IN_CLOEXEC | self._IN_NONBLOCK)
        self._directory_stat = self.directory.stat()
        self._reader = threading.Thread(target=self._read_loop, name="by-id-scan-observer", daemon=True)
        self._sampler = threading.Thread(
            target=self._sample_process_fds, name="by-id-scan-pid-observer", daemon=True,
        )
        self._reader.start()
        self._sampler.start()

    def close(self) -> None:
        self._stop_event.set()
        if getattr(self, "_reader", None) is not None and self._reader.is_alive():
            try:
                os.write(self._control_write, b"x")
            except OSError:
                pass
        for thread in (getattr(self, "_reader", None), getattr(self, "_sampler", None)):
            if thread is not None:
                thread.join(timeout=2.0)
                if thread.is_alive():
                    raise RuntimeError(f"{thread.name} 未停止")
        for fd_name in ("_control_read", "_control_write"):
            fd = getattr(self, fd_name, -1)
            if fd >= 0:
                os.close(fd)
                setattr(self, fd_name, -1)
        if getattr(self, "_fd", -1) >= 0:
            os.close(self._fd)
            self._fd = -1

    def _process_holds_directory(self) -> bool:
        task = self._process_task
        if self.thread_name is not None and task is None:
            tasks = Path(f"/proc/{self.process_pid}/task")
            try:
                for candidate in tasks.iterdir():
                    comm = (candidate / "comm").read_text(encoding="utf-8").strip()
                    if comm == self.thread_name[:15]:
                        self._process_task = candidate
                        task = candidate
                        break
            except OSError:
                return False
        if self.thread_name is not None:
            task_rows = [task] if task is not None else []
        else:
            tasks = Path(f"/proc/{self.process_pid}/task")
            try:
                task_rows = list(tasks.iterdir())
            except OSError:
                return False
        for task in task_rows:
            if task is None:
                continue
            try:
                fds = list((task / "fd").iterdir())
            except OSError:
                continue
            for fd in fds:
                try:
                    stat = fd.stat()
                except OSError:
                    continue
                if (stat.st_dev, stat.st_ino) == (self._directory_stat.st_dev, self._directory_stat.st_ino):
                    return True
        return False

    def _drain_inotify(self) -> None:
        while select.select([self._fd], [], [], 0)[0]:
            data = os.read(self._fd, 65536)
            offset = 0
            while offset + self._EVENT.size <= len(data):
                _watch, mask, _cookie, name_len = self._EVENT.unpack_from(data, offset)
                offset += self._EVENT.size
                raw_name = data[offset : offset + name_len]
                offset += name_len
                if mask & self._IN_Q_OVERFLOW:
                    raise AssertionError("by-id 目錄 inotify queue overflow")
                name = os.fsdecode(raw_name.split(b"\0", 1)[0]) if raw_name else ""
                with self._condition:
                    self.events.append({
                        "sequence": len(self.events) + 1,
                        "mask": mask,
                        "name": name,
                        "observed_ns": time.monotonic_ns(),
                        "process_pid": None,
                        "source": "inotify",
                    })
                    self._condition.notify_all()

    def _append_process_event(self, mask: int) -> None:
        with self._condition:
            self.events.append({
                "sequence": len(self.events) + 1,
                "mask": mask,
                "name": "",
                "observed_ns": time.monotonic_ns(),
                "process_pid": self.process_pid,
                "source": "proc_fd",
            })
            self._condition.notify_all()

    def _drain_scan_trace(self) -> None:
        if self.scan_trace is None:
            return
        try:
            with self.scan_trace.open("rb") as stream:
                stream.seek(self._trace_offset)
                chunk = stream.read()
        except FileNotFoundError:
            return
        self._trace_offset += len(chunk)
        data = self._trace_pending + chunk
        lines = data.splitlines(keepends=True)
        if lines and not lines[-1].endswith(b"\n"):
            self._trace_pending = lines.pop()
        else:
            self._trace_pending = b""
        for raw in lines:
            try:
                phase, pid, tid, mono_ns, link_present, handle_id = raw.decode(
                    "ascii",
                ).strip().split("\t")
                pid_int = int(pid)
                if phase not in {"open", "close"} or pid_int != self.process_pid:
                    continue
                tid_int = int(tid)
                thread_comm: str | None = None
                if self.thread_name is not None:
                    comm_path = Path(f"/proc/{pid_int}/task/{tid_int}/comm")
                    try:
                        thread_comm = comm_path.read_text(encoding="utf-8").strip()
                    except OSError:
                        continue
                    if thread_comm != self.thread_name[:15]:
                        continue
                event = {
                    "sequence": 0,
                    "mask": self._IN_OPEN if phase == "open" else self._IN_CLOSE_NOWRITE,
                    "name": "",
                    "observed_ns": int(mono_ns),
                    "process_pid": pid_int,
                    "thread_id": tid_int,
                    "handle_id": int(handle_id),
                    "thread_name": thread_comm,
                    "link_present": int(link_present) == 1,
                    "source": "scan_trace",
                }
            except (UnicodeDecodeError, ValueError):
                raise AssertionError(f"無效 by-id 掃描 trace：{raw!r}") from None
            with self._condition:
                event["sequence"] = len(self.events) + 1
                self.events.append(event)
                self._condition.notify_all()

    def _read_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                ready, _, _ = select.select(
                    [self._fd, self._control_read], [], [], 0.05,
                )
                if self._fd in ready:
                    self._drain_inotify()
                if self._control_read in ready:
                    payload = os.read(self._control_read, 4096)
                    self._drain_inotify()
                    if payload:
                        with self._condition:
                            self._barriers_handled += payload.count(b"m")
                            self._condition.notify_all()
        except BaseException as exc:
            with self._condition:
                self._reader_error = exc
                self._condition.notify_all()

    def _sample_process_fds(self) -> None:
        try:
            while not self._stop_event.is_set():
                with self._condition:
                    sampled_generation = self._barriers_requested
                if self.scan_trace is not None:
                    self._drain_scan_trace()
                    time.sleep(0.001)
                else:
                    holds = self._process_holds_directory()
                    if holds != self._sampler_holds_directory:
                        self._sampler_holds_directory = holds
                        self._append_process_event(
                            self._IN_OPEN if holds else self._IN_CLOSE_NOWRITE
                        )
                    os.sched_yield()
                with self._condition:
                    self._sampler_barriers_handled = max(
                        self._sampler_barriers_handled, sampled_generation,
                    )
                    self._condition.notify_all()
        except BaseException as exc:
            with self._condition:
                self._reader_error = exc
                self._condition.notify_all()

    def mark(self) -> int:
        """等待 reader 排空此刻前事件，再回傳 bridge action 的序列基準。"""
        with self._condition:
            self._barriers_requested += 1
            wanted = self._barriers_requested
        os.write(self._control_write, b"m")
        with self._condition:
            while (
                (
                    self._barriers_handled < wanted
                    or self._sampler_barriers_handled < wanted
                )
                and self._reader_error is None
            ):
                self._condition.wait(timeout=0.1)
            if self._reader_error is not None:
                raise RuntimeError("by-id 掃描觀測器 reader 失敗") from self._reader_error
            return len(self.events)

    def _wait_for(self, predicate, *, timeout_s: float, deadline_ns: int | None, label: str):
        local_deadline = time.monotonic_ns() + int(timeout_s * 1_000_000_000)
        effective_deadline = min(
            local_deadline,
            deadline_ns if deadline_ns is not None else local_deadline,
        )
        with self._condition:
            while True:
                if self._reader_error is not None:
                    raise RuntimeError("by-id 掃描觀測器 reader 失敗") from self._reader_error
                result = predicate()
                if result is not None:
                    if deadline_ns is None or all(
                        event["observed_ns"] <= deadline_ns for event in result
                    ):
                        return result
                    raise TimeoutError(
                        f"{label} 事件在 handoff 排空窗口截止後才被觀察到：{result!r}"
                    )
                remaining_ns = effective_deadline - time.monotonic_ns()
                if remaining_ns <= 0:
                    raise TimeoutError(f"等待 {label} 逾時；events={self.events!r}")
                self._condition.wait(timeout=min(remaining_ns / 1_000_000_000, 0.1))

    def wait_link_event(
        self, *, after_seq: int, action: str, timeout_s: float, deadline_ns: int | None = None,
    ) -> dict[str, int | str]:
        wanted_mask = self._IN_DELETE if action == "remove" else self._IN_CREATE

        def find():
            return next((event for event in self.events
                         if event["sequence"] > after_seq and event["name"] == self.link_name
                         and int(event["mask"]) & wanted_mask), None)

        return self._wait_for(
            lambda: (event,) if (event := find()) is not None else None,
            timeout_s=timeout_s, deadline_ns=deadline_ns, label=f"symlink {action}",
        )[0]

    def wait_scan_after(
        self,
        *,
        after_seq: int,
        timeout_s: float,
        deadline_ns: int | None = None,
        link_present: bool | None = None,
        started_after_ns: int | None = None,
    ) -> tuple[dict[str, int | str], dict[str, int | str]]:
        def find():
            opened_by_handle: dict[tuple[int, int | None, int | None], dict] = {}
            for event in self.events:
                if event["sequence"] <= after_seq or event["name"]:
                    continue
                source = event.get("source")
                if source not in {"scan_trace", "proc_fd"}:
                    continue
                pid = event.get("process_pid")
                if pid != self.process_pid:
                    continue
                thread_id = event.get("thread_id")
                handle_id = event.get("handle_id")
                if source == "scan_trace" and (thread_id is None or handle_id is None):
                    continue
                key = (int(pid), thread_id, handle_id)
                if int(event["mask"]) & self._IN_OPEN:
                    if (
                        started_after_ns is not None
                        and int(event["observed_ns"]) < started_after_ns
                    ):
                        continue
                    if (
                        link_present is not None
                        and event.get("link_present") != link_present
                    ):
                        continue
                    opened_by_handle[key] = event
                elif int(event["mask"]) & self._IN_CLOSE_NOWRITE:
                    opened = opened_by_handle.get(key)
                    if opened is not None:
                        return opened, event
            return None

        return self._wait_for(
            find, timeout_s=timeout_s, deadline_ns=deadline_ns, label="symlink 變更後的 by-id 目錄掃描",
        )


def _scan_by_id_directory(directory: Path, *, hold_s: float = 0.05) -> None:
    with os.scandir(directory) as entries:
        next(entries, None)
        if hold_s:
            time.sleep(hold_s)


def _append_scan_event(
    observer: _ByIdScanObserver,
    *,
    phase: str,
    thread_id: int,
    handle_id: int,
    observed_ns: int,
    link_present: bool,
) -> None:
    with observer._condition:
        observer.events.append({
            "sequence": len(observer.events) + 1,
            "mask": observer._IN_OPEN if phase == "open" else observer._IN_CLOSE_NOWRITE,
            "name": "",
            "observed_ns": observed_ns,
            "process_pid": observer.process_pid,
            "thread_id": thread_id,
            "handle_id": handle_id,
            "link_present": link_present,
            "source": "scan_trace",
        })
        observer._condition.notify_all()


def _prepare_by_id_scan_trace(tmp_path: Path, env: dict[str, str]) -> Path:
    compiler = shutil.which("cc")
    assert compiler is not None, "LD_PRELOAD 掃描觀測器需要 cc"
    preload = tmp_path / "by-id-scan-trace.so"
    result = subprocess.run(
        [
            compiler,
            "-shared",
            "-fPIC",
            "-O2",
            "-x",
            "c",
            "-",
            "-ldl",
            "-o",
            str(preload),
        ],
        input=_BY_ID_SCAN_PRELOAD_C,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    trace = tmp_path / "by-id-scan.trace"
    env["LD_PRELOAD"] = os.pathsep.join(
        part for part in (str(preload), env.get("LD_PRELOAD", "")) if part
    )
    env["SW_TEST_BY_ID_SCAN_TRACE"] = str(trace)
    env["SW_TEST_BY_ID_SCAN_DIR"] = env["SERIALWRAP_BY_ID_DIR"]
    env["SW_TEST_BY_ID_SCAN_LINK"] = str(
        Path(env["SERIALWRAP_BY_ID_DIR"]) / "usb-FAKE_TARGET_0-if00"
    )
    return trace


def _wait_scan_trace_event(
    trace: Path,
    phase: str,
    *,
    after_offset: int,
    timeout_s: float,
    process_pid: int | None = None,
) -> dict[str, int | str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            raw_lines = trace.read_bytes()[after_offset:].splitlines()
        except FileNotFoundError:
            raw_lines = []
        for raw in raw_lines:
            try:
                event_phase, pid, tid, mono_ns, link_present, handle_id = raw.decode("ascii").split("\t")
                event = {
                    "phase": event_phase,
                    "process_pid": int(pid),
                    "thread_id": int(tid),
                    "observed_ns": int(mono_ns),
                    "link_present": int(link_present),
                    "handle_id": int(handle_id),
                }
            except (UnicodeDecodeError, ValueError):
                continue
            if event["phase"] != phase:
                continue
            if process_pid is not None and event["process_pid"] != process_pid:
                continue
            return event
        time.sleep(0.01)
    raise TimeoutError(f"等待 preload trace {phase} 逾時：{trace}")


def _install_old(tmp_path: Path, env: dict[str, str], bundle: Path) -> tuple[Path, Path]:
    cli = select_bundle(env, bundle)
    setup_install_root(tmp_path, env)
    prepare_profile(env)
    result = run_cli(cli, ["setup", "--on-demand"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    return cli, Path(env["SERIALWRAP_INSTALL_ROOT"])


def _start_rig(tmp_path: Path, env: dict[str, str], *, delay_ms: int = 1):
    profile_dir = prepare_profile(env)
    bridge = BridgeTarget.start(tmp_path / "bridge", env, delay_ms=delay_ms)
    installed_daemon = (
        Path(env["SERIALWRAP_INSTALL_ROOT"]) / "bin" / "serialwrapd"
    )
    assert installed_daemon.is_file() and os.access(installed_daemon, os.X_OK), (
        installed_daemon
    )
    try:
        daemon = start_python_daemon(
            env,
            profile_dir,
            bridge=bridge,
            daemon_command=[str(installed_daemon)],
        )
    except BaseException:
        bridge.stop()
        raise
    return bridge, daemon


def _stop_rig(bridge: BridgeTarget, daemon, cli: Path, env: dict[str, str]) -> None:
    finish_test_rig(bridge, daemon, cli, env)


@pytest.mark.process_rpc
def test_by_id_scan_observer_rejects_a_pre_change_scan_and_times_out_without_new_scan(tmp_path: Path) -> None:
    directory = tmp_path / "by-id"
    directory.mkdir()
    target = tmp_path / "device"
    target.touch()
    link = directory / "usb-FAKE_TARGET_0-if00"
    link.symlink_to(target)
    (directory / "padding").touch()
    observer = _ByIdScanObserver(directory, link.name)
    try:
        before_change = observer.mark()
        _scan_by_id_directory(directory)
        old_scan = observer.wait_scan_after(after_seq=before_change, timeout_s=1.0)

        before_remove = observer.mark()
        link.unlink()
        removed = observer.wait_link_event(after_seq=before_remove, action="remove", timeout_s=1.0)
        assert old_scan[1]["sequence"] < removed["sequence"]
        foreign_scan = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os, sys, time; "
                    "entries=os.scandir(sys.argv[1]); next(entries, None); "
                    "time.sleep(0.05); entries.close()"
                ),
                str(directory),
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        assert foreign_scan.returncode == 0, foreign_scan.stderr
        with pytest.raises(TimeoutError, match="等待 symlink 變更後的 by-id 目錄掃描 逾時"):
            observer.wait_scan_after(after_seq=int(removed["sequence"]), timeout_s=0.05)
    finally:
        observer.close()


@pytest.mark.memory_only
def test_by_id_scan_observer_requires_a_separate_scan_after_link_restore(tmp_path: Path) -> None:
    directory = tmp_path / "by-id"
    directory.mkdir()
    target = tmp_path / "device"
    target.touch()
    link = directory / "usb-FAKE_TARGET_0-if00"
    link.symlink_to(target)
    (directory / "padding").touch()
    observer = _ByIdScanObserver(directory, link.name)
    try:
        before_remove = observer.mark()
        link.unlink()
        removed = observer.wait_link_event(after_seq=before_remove, action="remove", timeout_s=1.0)
        _scan_by_id_directory(directory)
        remove_scan = observer.wait_scan_after(after_seq=int(removed["sequence"]), timeout_s=1.0)

        before_restore = observer.mark()
        link.symlink_to(target)
        restored = observer.wait_link_event(after_seq=before_restore, action="restore", timeout_s=1.0)
        assert remove_scan[1]["sequence"] < restored["sequence"]
        with pytest.raises(TimeoutError, match="等待 symlink 變更後的 by-id 目錄掃描 逾時"):
            observer.wait_scan_after(after_seq=int(restored["sequence"]), timeout_s=0.05)

        _scan_by_id_directory(directory)
        restore_scan = observer.wait_scan_after(after_seq=int(restored["sequence"]), timeout_s=1.0)
        assert restored["sequence"] < restore_scan[0]["sequence"]
        assert remove_scan[0]["sequence"] < remove_scan[1]["sequence"]
        assert restore_scan[0]["sequence"] < restore_scan[1]["sequence"]
    finally:
        observer.close()


@pytest.mark.memory_only
def test_by_id_scan_observer_rejects_open_started_before_link_action_completed(tmp_path: Path) -> None:
    directory = tmp_path / "by-id"
    directory.mkdir()
    target = tmp_path / "device"
    target.touch()
    link = directory / "usb-FAKE_TARGET_0-if00"
    link.symlink_to(target)
    observer = _ByIdScanObserver(directory, link.name)
    try:
        before_remove = observer.mark()
        link.unlink()
        action_completed_ns = time.monotonic_ns()
        removed = observer.wait_link_event(
            after_seq=before_remove, action="remove", timeout_s=1.0,
        )
        _append_scan_event(
            observer,
            phase="open",
            thread_id=101,
            handle_id=1001,
            observed_ns=action_completed_ns - 1,
            link_present=False,
        )
        _append_scan_event(
            observer,
            phase="close",
            thread_id=101,
            handle_id=1001,
            observed_ns=time.monotonic_ns(),
            link_present=False,
        )
        with pytest.raises(TimeoutError, match="等待 symlink 變更後的 by-id 目錄掃描 逾時"):
            observer.wait_scan_after(
                after_seq=int(removed["sequence"]),
                timeout_s=0.05,
                link_present=False,
                started_after_ns=action_completed_ns,
            )
        _append_scan_event(
            observer,
            phase="open",
            thread_id=101,
            handle_id=1002,
            observed_ns=time.monotonic_ns(),
            link_present=False,
        )
        _append_scan_event(
            observer,
            phase="close",
            thread_id=101,
            handle_id=1002,
            observed_ns=time.monotonic_ns(),
            link_present=False,
        )
        accepted = observer.wait_scan_after(
            after_seq=int(removed["sequence"]),
            timeout_s=0.5,
            link_present=False,
            started_after_ns=action_completed_ns,
        )
        assert accepted[0]["handle_id"] == accepted[1]["handle_id"] == 1002, accepted
    finally:
        observer.close()


@pytest.mark.memory_only
def test_by_id_scan_observer_rejects_open_close_from_different_threads(tmp_path: Path) -> None:
    directory = tmp_path / "by-id"
    directory.mkdir()
    target = tmp_path / "device"
    target.touch()
    link = directory / "usb-FAKE_TARGET_0-if00"
    link.symlink_to(target)
    observer = _ByIdScanObserver(directory, link.name)
    try:
        before_remove = observer.mark()
        link.unlink()
        action_completed_ns = time.monotonic_ns()
        removed = observer.wait_link_event(
            after_seq=before_remove, action="remove", timeout_s=1.0,
        )
        _append_scan_event(
            observer,
            phase="open",
            thread_id=101,
            handle_id=1001,
            observed_ns=action_completed_ns + 1,
            link_present=False,
        )
        _append_scan_event(
            observer,
            phase="close",
            thread_id=102,
            handle_id=1001,
            observed_ns=time.monotonic_ns(),
            link_present=False,
        )
        _append_scan_event(
            observer,
            phase="open",
            thread_id=101,
            handle_id=1002,
            observed_ns=time.monotonic_ns(),
            link_present=False,
        )
        _append_scan_event(
            observer,
            phase="close",
            thread_id=101,
            handle_id=1003,
            observed_ns=time.monotonic_ns(),
            link_present=False,
        )
        with pytest.raises(TimeoutError, match="等待 symlink 變更後的 by-id 目錄掃描 逾時"):
            observer.wait_scan_after(
                after_seq=int(removed["sequence"]),
                timeout_s=0.05,
                link_present=False,
                started_after_ns=action_completed_ns,
            )
        _append_scan_event(
            observer,
            phase="open",
            thread_id=101,
            handle_id=1002,
            observed_ns=time.monotonic_ns(),
            link_present=False,
        )
        _append_scan_event(
            observer,
            phase="close",
            thread_id=101,
            handle_id=1002,
            observed_ns=time.monotonic_ns(),
            link_present=False,
        )
        accepted = observer.wait_scan_after(
            after_seq=int(removed["sequence"]),
            timeout_s=0.5,
            link_present=False,
            started_after_ns=action_completed_ns,
        )
        assert accepted[0]["thread_id"] == accepted[1]["thread_id"] == 101, accepted
        assert accepted[0]["handle_id"] == accepted[1]["handle_id"] == 1002, accepted
    finally:
        observer.close()


@pytest.mark.rust_only
@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.process_rpc
@pytest.mark.pidns_rig
def test_fresh_install_blackbox(tmp_path: Path) -> None:
    """隔離 HOME＋空 install root＋stub PATH：setup --on-demand → daemon start → session list；profiles／config 存在。"""
    env = isolated_env(tmp_path)
    # 預設讀本測試專用的 mkbundle 產物 target/mkbundle-pytest（非共享的 target/mkbundle-dev，後者可能是舊版無標記目錄）。
    sw_bundle = os.environ.get("SW_BUNDLE", str(repo_root() / "target" / "mkbundle-pytest"))
    cli_bin = Path(sw_bundle) / "bin" / "serialwrap"
    assert cli_bin.is_file(), f"缺少 bundle CLI：{cli_bin}（請先 scripts/rust/mkbundle.sh --dev --out {sw_bundle}，或設 SW_BUNDLE）"
    daemon_started = False
    start_attempted = False
    try:
        res = subprocess.run([str(cli_bin), "setup", "--on-demand"], env=env, capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stdout + res.stderr
        assert (Path(env["SERIALWRAP_CONFIG_DIR"]) / "config.yaml").exists()
        assert (Path(env["SERIALWRAP_CONFIG_DIR"]) / "profiles" / "default.yaml").exists()
        start_attempted = True
        res_start = subprocess.run([str(cli_bin), "daemon", "start"], env=env, capture_output=True, text=True, timeout=30)
        daemon_started = res_start.returncode == 0
        assert res_start.returncode == 0, res_start.stdout + res_start.stderr
        res_list = subprocess.run([str(cli_bin), "session", "list"], env=env, capture_output=True, text=True, timeout=30)
        assert res_list.returncode == 0, res_list.stdout + res_list.stderr
    finally:
        active_error = sys.exc_info()[1]
        if start_attempted:
            try:
                status = subprocess.run(
                    [str(cli_bin), "daemon", "status"], env=env, capture_output=True, text=True,
                    timeout=30, check=False,
                )
                payload = json.loads(status.stdout) if status.stdout else {}
                if daemon_started or payload.get("ok") is True:
                    stopped = subprocess.run(
                        [str(cli_bin), "daemon", "stop"], env=env, capture_output=True, text=True,
                        timeout=30, check=False,
                    )
                    if stopped.returncode != 0:
                        raise RuntimeError(stopped.stdout + stopped.stderr)
            except BaseException as cleanup_error:
                if active_error is None:
                    raise
                active_error.add_note(
                    f"隔離 fresh-install daemon 清理失敗：{type(cleanup_error).__name__}: {cleanup_error}"
                )


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.pidns_rig
def test_update_rollback_ab_isolated(tmp_path: Path, env: dict[str, str]) -> None:
    """透過 bridge 以 0.4.1 安裝、更新至目前版本，再以真正 rollback 還原 0.4.1。"""
    old, new = update_bundles()
    old_cli, _root = _install_old(tmp_path, env, old)
    bridge, daemon = _start_rig(tmp_path, env)
    new_cli = select_bundle(env, new)
    try:
        before_pid = daemon_pid(env)
        update = run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=180.0)
        assert update.returncode == 0, update.stdout + update.stderr
        assert installed_version(env) == NEW_BUNDLE_VERSION
        after_update_pid = daemon_pid(env)
        assert after_update_pid != before_pid
        assert wait_session(env)["state"] == "READY"

        rollback = run_cli(new_cli, ["setup", "--rollback"], env, timeout_s=180.0)
        assert rollback.returncode == 0, rollback.stdout + rollback.stderr
        assert installed_version(env) == OLD_BUNDLE_VERSION
        status = rpc(env, "health.status")
        assert status.get("ok") is True and status.get("version") == OLD_BUNDLE_VERSION, status
        assert daemon_pid(env) != after_update_pid
    finally:
        _stop_rig(bridge, daemon, old_cli, env)


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.pidns_rig
def test_update_busy_l15_zero_tx(tmp_path: Path, env: dict[str, str]) -> None:
    """file push 正在 target 上處理 chunk 時更新回 SESSION_BUSY，更新期間 bridge DUT TX 無新增。

    時序設計（量測依據）：push 採 ACK-gated（`ack_mode=auto`，每個 chunk 等板端 `SW_XFER_CHUNK_42`），
    DUT 於首個 chunk 延遲 `BUSY_FIRST_ACK_DELAY_MS` 才回 ACK。CLI 在 ACK 到來前不送下一個 chunk，故首個 chunk TX 之後、
    ACK 之前的視窗內 TX 靜止；更新必須在該視窗內被拒，TX 差值為零。`wait_wire_contains` 對 TX 比對的是
    chunk 指令文字（`SW_XFER_CHUNK_` 前綴，數值由目標端 `$((6*7))` 展開），而非 ACK 字串 `SW_XFER_CHUNK_42`。

    檔案大小依量測：ACK-gated 路徑於 WSL2 bridge 實測約 2.2 KiB/s（64 KiB 約 30 s），因此本案例改用 16 KiB
    （4 個 chunk，約 8 s 完成＋首個 ACK 延遲 `BUSY_FIRST_ACK_DELAY_MS`），仍為多 chunk 且 push 全程在 join 逾時內結束。
    """
    old, new = update_bundles()
    old_cli, _root = _install_old(tmp_path, env, old)
    bridge, daemon = _start_rig(tmp_path, env, delay_ms=BUSY_FIRST_ACK_DELAY_MS)
    new_cli = select_bundle(env, new)
    source = tmp_path / "busy-push.bin"
    expected_md5, remote = make_push_file(source, size=BUSY_PUSH_BYTES)
    push_thread = None
    try:
        push_thread, push_rows = start_file_push(new_cli, env, source, remote, ack_mode="auto", chunk_timeout_s=BUSY_CHUNK_TIMEOUT_S)
        # 等整行 chunk 指令（含行尾換行）都已寫入 TX，避免快照落在半行上；此後 CLI 等待首個 ACK，TX 靜止。
        wait_wire_contains(bridge.evidence_dir, b"SW_XFER_CHUNK_'$((6*7))\n", timeout_s=30.0)
        tx_before = tx_bytes(bridge.evidence_dir)
        result = run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=30.0)
        data = json_stdout(result)
        assert result.returncode == 2, result.stdout + result.stderr
        assert data.get("error_code") == "SESSION_BUSY", data
        assert tx_bytes(bridge.evidence_dir) == tx_before
        assert daemon_pid(env) == daemon.proc.pid
        push_thread.join(180.0)
        assert not push_thread.is_alive(), f"{BUSY_PUSH_BYTES} B file push 未結束"
        assert len(push_rows) == 1
        push = json_stdout(push_rows[0])
        assert push_rows[0].returncode == 0, push_rows[0].stdout + push_rows[0].stderr
        assert push.get("bytes") == BUSY_PUSH_BYTES and push.get("md5") == expected_md5
        assert (bridge.remote_root / Path(remote).name).read_bytes() == source.read_bytes()
    finally:
        if push_thread is not None and push_thread.is_alive():
            push_thread.join(180.0)
        _stop_rig(bridge, daemon, old_cli, env)


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.pidns_rig
def test_update_file_push_after_last_probe_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """最後預檢後再開始 1 MiB push，交接拒絕且 Current／PID 不變；push 完成並驗 MD5。"""
    old, new = update_bundles()
    old_cli, root = _install_old(tmp_path, env, old)
    bridge, daemon = _start_rig(tmp_path, env, delay_ms=500)
    new_cli = select_bundle(env, new)
    source = tmp_path / "last-probe.bin"
    expected_md5, remote = make_push_file(source)
    pause_dir = tmp_path / "pause-safety-check"
    env.update({"SW_TEST_UPDATE_PAUSE_AFTER": "safety_check", "SW_TEST_UPDATE_PAUSE_DIR": str(pause_dir)})
    update_rows = []
    update_thread = RaisingThread(
        lambda: update_rows.append(run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=240.0)),
    )
    push_thread = None
    old_pid = daemon_pid(env)
    current_bytes = current_release_path(root).read_bytes()
    try:
        update_thread.start()
        wait_pause(pause_dir, "safety_check")
        push_thread, push_rows = start_file_push(new_cli, env, source, remote)
        wait_rx_contains(bridge.evidence_dir, b"SW_XFER_CHUNK_42", timeout_s=30.0)
        assert push_thread.is_alive()
        resume_pause(pause_dir, "safety_check")
        update_thread.join(120.0)
        assert not update_thread.is_alive(), "setup update 未在 120 秒內結束"
        update = update_rows[0]
        data = json_stdout(update)
        assert update.returncode == 2, update.stdout + update.stderr
        assert data.get("error_code") == "SESSION_BUSY", data
        assert data.get("handoff") == "refused", data
        assert daemon_pid(env) == old_pid
        assert installed_version(env) == OLD_BUNDLE_VERSION
        assert current_release_path(root).read_bytes() == current_bytes
        assert not any("shutdown" in json.dumps(row, ensure_ascii=False).lower() for row in wal_rows(env))
        push_thread.join(180.0)
        assert not push_thread.is_alive(), "1 MiB file push 未結束"
        assert len(push_rows) == 1
        push = json_stdout(push_rows[0])
        assert push_rows[0].returncode == 0, push_rows[0].stdout + push_rows[0].stderr
        assert push.get("bytes") == 1024 * 1024 and push.get("md5") == expected_md5
        assert (bridge.remote_root / Path(remote).name).read_bytes() == source.read_bytes()
    finally:
        if update_thread.is_alive():
            resume_pause(pause_dir, "safety_check")
            update_thread.join(15.0)
        if push_thread is not None and push_thread.is_alive():
            push_thread.join(180.0)
        env.pop("SW_TEST_UPDATE_PAUSE_AFTER", None)
        env.pop("SW_TEST_UPDATE_PAUSE_DIR", None)
        _stop_rig(bridge, daemon, old_cli, env)


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.pidns_rig
def test_revert_new_daemon_push_not_aborted_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """新版 daemon 已啟動後 push 持續阻止回滾 handoff；回滾待復原，push 完成後由舊 bundle 恢復。"""
    old, new = update_bundles()
    old_cli, root = _install_old(tmp_path, env, old)
    bridge, daemon = _start_rig(tmp_path, env, delay_ms=500)
    new_cli = select_bundle(env, new)
    pause_dir = tmp_path / "pause-new-daemon"
    env.update({
        "SW_TEST_UPDATE_PAUSE_AFTER": "start_and_verify:started",
        "SW_TEST_UPDATE_PAUSE_DIR": str(pause_dir),
        "SW_TEST_UPDATE_FORCE_VERIFY_FAIL": "smoke_failed",
    })
    update_rows = []
    update_thread = RaisingThread(
        lambda: update_rows.append(run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=240.0)),
    )
    push_thread = None
    old_pid = daemon_pid(env)
    source = tmp_path / "revert-push.bin"
    expected_md5, remote = make_push_file(source)
    try:
        update_thread.start()
        # 這個暫停點在 handoff、停舊 daemon、啟動新 bundle 之後，hosted runner 要超過預設 20 秒
        wait_pause(pause_dir, "start_and_verify:started", timeout_s=90.0)
        new_pid = daemon_pid(env)
        assert new_pid != old_pid
        assert wait_session(env)["state"] == "READY"
        push_thread, push_rows = start_file_push(new_cli, env, source, remote)
        wait_rx_contains(bridge.evidence_dir, b"SW_XFER_CHUNK_42", timeout_s=30.0)
        assert push_thread.is_alive()
        resume_pause(pause_dir, "start_and_verify:started")
        update_thread.join(120.0)
        assert not update_thread.is_alive(), "setup 驗證失敗後未結束"
        update = update_rows[0]
        data = json_stdout(update)
        report = data.get("update", {})
        assert update.returncode == 2, update.stdout + update.stderr
        assert report.get("revert_pending") is True, data
        assert report.get("revert_error", {}).get("error_code") == "SESSION_BUSY", data
        assert daemon_pid(env) == new_pid
        assert installed_version(env) == NEW_BUNDLE_VERSION
        assert not any("shutdown" in json.dumps(row, ensure_ascii=False).lower() for row in wal_rows(env))
        push_thread.join(180.0)
        assert not push_thread.is_alive(), "1 MiB file push 未結束"
        assert len(push_rows) == 1
        push = json_stdout(push_rows[0])
        assert push_rows[0].returncode == 0, push_rows[0].stdout + push_rows[0].stderr
        assert push.get("bytes") == 1024 * 1024 and push.get("md5") == expected_md5
        assert (bridge.remote_root / Path(remote).name).read_bytes() == source.read_bytes()

        env.pop("SW_TEST_UPDATE_PAUSE_AFTER", None)
        env.pop("SW_TEST_UPDATE_PAUSE_DIR", None)
        env.pop("SW_TEST_UPDATE_FORCE_VERIFY_FAIL", None)
        select_bundle(env, old)
        recovery = run_cli(old_cli, ["setup", "--on-demand"], env, timeout_s=180.0)
        assert recovery.returncode == 0, recovery.stdout + recovery.stderr
        assert installed_version(env) == OLD_BUNDLE_VERSION
        assert rpc(env, "health.status").get("version") == OLD_BUNDLE_VERSION
    finally:
        if update_thread.is_alive():
            resume_pause(pause_dir, "start_and_verify:started")
            update_thread.join(15.0)
        if push_thread is not None and push_thread.is_alive():
            push_thread.join(180.0)
        env.pop("SW_TEST_UPDATE_PAUSE_AFTER", None)
        env.pop("SW_TEST_UPDATE_PAUSE_DIR", None)
        env.pop("SW_TEST_UPDATE_FORCE_VERIFY_FAIL", None)
        _stop_rig(bridge, daemon, old_cli, env)


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.pidns_rig
def test_update_push_after_freeze_zero_tx_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """handoff_stop 回 ok 後 file.push 回 FLUSHED_BY_SHUTDOWN；凍結期間 DUT TX 為零且無部分檔案。"""
    old, new = update_bundles()
    old_cli, _root = _install_old(tmp_path, env, old)
    bridge, daemon = _start_rig(tmp_path, env)
    new_cli = select_bundle(env, new)
    source = tmp_path / "frozen-push.bin"
    make_push_file(source)
    pause_dir = tmp_path / "pause-handoff"
    env.update({"SW_TEST_UPDATE_PAUSE_AFTER": "handoff_stop:replied", "SW_TEST_UPDATE_PAUSE_DIR": str(pause_dir)})
    update_rows = []
    update_thread = RaisingThread(
        lambda: update_rows.append(run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=240.0)),
    )
    try:
        update_thread.start()
        wait_pause(pause_dir, "handoff_stop:replied")
        bridge.mark("handoff-frozen", os.getpid())
        tx_at_freeze = tx_bytes(bridge.evidence_dir)
        push = run_cli(new_cli, ["file", "push", "--selector", "COM0", "--local", str(source), "--remote", "/tmp/frozen-push.bin", "--ack-mode", "echo"], env, timeout_s=30.0)
        data = json_stdout(push)
        assert push.returncode == 2, push.stdout + push.stderr
        assert data.get("error_code") == "FLUSHED_BY_SHUTDOWN", data
        assert data.get("reason") == "handoff_frozen", data
        assert tx_bytes(bridge.evidence_dir) == tx_at_freeze
        assert not list(bridge.remote_root.glob("*"))
        resume_pause(pause_dir, "handoff_stop:replied")
        update_thread.join(120.0)
        assert not update_thread.is_alive(), "setup update 未在 120 秒內結束"
        update = update_rows[0]
        assert update.returncode == 0, update.stdout + update.stderr
        assert installed_version(env) == NEW_BUNDLE_VERSION
        assert wait_session(env)["state"] == "READY"
    finally:
        if update_thread.is_alive():
            resume_pause(pause_dir, "handoff_stop:replied")
            update_thread.join(15.0)
        env.pop("SW_TEST_UPDATE_PAUSE_AFTER", None)
        env.pop("SW_TEST_UPDATE_PAUSE_DIR", None)
        _stop_rig(bridge, daemon, new_cli, env)


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.pidns_rig
def test_update_background_triggers_zero_tx_in_drain_window_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """handoff_stop 回 ok 後的排空窗口內，背景來源不得產生 DUT TX（OD-16 (3) 所需之 L1.5 證據）。

    與 `test_update_push_after_freeze_zero_tx_l15`（只驗 file.push 路徑）不同：本案例窗口內不送任何 RPC，
    觸發全部來自背景引擎，依序為既有 console 輸入一行、DeviceWatcher 在 by-id 連結移除與復原後各自重探。
    inotify 以該私有 by-id 目錄的 symlink 事件分隔兩階段，再以 daemon PID 的 /proc fd 證據等待事件之後的
    目錄 open/close；trace 以 daemon PID、`serialwrap-device-watcher` TID 和同一目錄 handle 配對，open 時間
    取自 `opendir` 入口並須晚於 bridge 控制呼叫完成；不使用 device.list 代替掃描完成訊號，其他行程單獨讀目錄不得滿足判準。

    時序與窗口邊界：preload 在 CLI 寫出 `daemon.handoff_stop` RPC 前記錄 monotonic 下界；該值不晚於
    daemon 建立窗口的實際起點，故加上 HANDOFF_DRAIN_GRACE_S 的截止點保守。preload 另在 CLI 收到回覆、
    建立 reached marker 前注入 150 ms 延遲，確認 marker 延遲不會把推算截止點往後移。斷言分兩段：
    1) 窗口仍開啟的證明：觸發後舊 daemon 尚未停止、`health.ping` 成功，且此時 TX 仍與凍結時一致；
    2) 整段零 TX：舊 daemon 停止（即窗口結束）後，以 bridge mark 作為擷取排空的屏障，TX 須與凍結時一致，
       且 console marker 不得出現於 TX（`assert_no_tx_after`）。

    console 前提：update 的 `update_safety_check` 階段會拒絕持有 interactive lease 的 console（`SESSION_BUSY`）；
    daemon 的 lease 自癒則會在外部程序持有 vtty 時授予 human lease（`_HUMAN_PEER_GRACE_S`）。因此本案例只讀取
    `primary["vtty"]` 路徑，於 `handoff_stop:replied` 暫停點之後才 `os.open`（凍結期間 lease 自癒 tick 停用），
    不得於 update 啟動前開啟。human console（`session.console_attach`）同樣會開啟 interactive lease，
    使 handoff 回 `SESSION_BUSY`，故不作為觸發來源。
    """
    old, new = update_bundles()
    old_cli, root = _install_old(tmp_path, env, old)
    scan_trace = _prepare_by_id_scan_trace(tmp_path, env)
    bridge, daemon = _start_rig(tmp_path, env)
    new_cli = select_bundle(env, new)
    pause_dir = tmp_path / "pause-drain-window"
    scan_observer = _ByIdScanObserver(
        Path(env["SERIALWRAP_BY_ID_DIR"]),
        "usb-FAKE_TARGET_0-if00",
        process_pid=daemon.proc.pid,
        thread_name="serialwrap-device-watcher",
        scan_trace=scan_trace,
    )
    old_daemon = root / "bin" / "serialwrapd"
    old_status = rpc(env, "health.status")
    assert old_daemon.is_file() and os.access(old_daemon, os.X_OK), old_daemon
    assert old_status.get("version") == OLD_BUNDLE_VERSION, old_status
    assert Path(os.readlink(f"/proc/{daemon.proc.pid}/exe")).resolve() == old_daemon.resolve(), {
        "actual_exe": os.readlink(f"/proc/{daemon.proc.pid}/exe"),
        "expected_exe": str(old_daemon),
        "status": old_status,
    }
    env.update({
        "SW_TEST_UPDATE_PAUSE_AFTER": "handoff_stop:replied",
        "SW_TEST_UPDATE_PAUSE_DIR": str(pause_dir),
        "SW_TEST_MARKER_WRITE_DELAY_MS": "150",
    })
    update_rows = []
    trace_offset = scan_trace.stat().st_size if scan_trace.exists() else 0
    update_thread = RaisingThread(
        lambda: update_rows.append(run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=240.0)),
    )
    console_fd: int | None = None
    try:
        consoles = rpc(env, "session.console_list", {"selector": "COM0"}).get("consoles") or []
        primary = next((row for row in consoles if row.get("label") == "primary"), None)
        assert primary is not None and primary.get("vtty"), consoles
        vtty = primary["vtty"]
        marker = f"DRAIN_WINDOW_{time.time_ns()}"
        update_thread.start()
        wait_pause(pause_dir, "handoff_stop:replied")
        handoff_request = _wait_scan_trace_event(
            scan_trace, "handoff_request", after_offset=trace_offset, timeout_s=2.0,
        )
        marker_delay = _wait_scan_trace_event(
            scan_trace,
            "marker_delay",
            after_offset=trace_offset,
            timeout_s=2.0,
            process_pid=handoff_request["process_pid"],
        )
        pause_marker_observed_ns = time.monotonic_ns()
        window_started_lower_bound_ns = handoff_request["observed_ns"]
        assert window_started_lower_bound_ns <= pause_marker_observed_ns, (
            window_started_lower_bound_ns, pause_marker_observed_ns,
        )
        marker_delay_ns = marker_delay["observed_ns"]
        assert marker_delay_ns - handoff_request["observed_ns"] >= 150_000_000, (
            handoff_request, marker_delay,
        )
        assert pause_marker_observed_ns >= marker_delay_ns, (pause_marker_observed_ns, marker_delay)
        window_deadline_ns = window_started_lower_bound_ns + int(
            HANDOFF_DRAIN_GRACE_S * 1_000_000_000
        )
        assert pause_marker_observed_ns <= window_deadline_ns, (
            pause_marker_observed_ns, window_deadline_ns,
        )
        bridge.mark("handoff-frozen", os.getpid())
        tx_at_freeze = tx_bytes(bridge.evidence_dir)
        frozen_pid = daemon.proc.pid
        console_fd = os.open(vtty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        # 1) 既有 console 輸入一行（不經 RPC）。
        os.write(console_fd, f"echo {marker}\n".encode())

        scan_trace = []

        def trigger_and_wait_scan(action: str, change: str) -> None:
            action_mark = scan_observer.mark()
            action_started_ns = time.monotonic_ns()
            bridge.control.call(action, {})
            action_completed_ns = time.monotonic_ns()
            link_event = scan_observer.wait_link_event(
                after_seq=action_mark,
                action=change,
                timeout_s=HANDOFF_DRAIN_GRACE_S,
                deadline_ns=window_deadline_ns,
            )
            scan_open, scan_close = scan_observer.wait_scan_after(
                after_seq=action_mark,
                timeout_s=HANDOFF_DRAIN_GRACE_S,
                deadline_ns=window_deadline_ns,
                link_present=change == "restore",
                started_after_ns=action_completed_ns,
            )
            scan_trace.append({
                "action": action,
                "action_started_ns": action_started_ns,
                "action_completed_ns": action_completed_ns,
                "link_event_sequence": link_event["sequence"],
                "link_event_observed_ns": link_event["observed_ns"],
                "scan_open_sequence": scan_open["sequence"],
                "scan_open_observed_ns": scan_open["observed_ns"],
                "scan_process_pid": scan_open["process_pid"],
                "scan_thread_id": scan_open["thread_id"],
                "scan_thread_name": scan_open["thread_name"],
                "scan_link_present": scan_open["link_present"],
                "scan_open_handle_id": scan_open["handle_id"],
                "scan_close_sequence": scan_close["sequence"],
                "scan_close_observed_ns": scan_close["observed_ns"],
                "scan_close_thread_id": scan_close["thread_id"],
                "scan_close_handle_id": scan_close["handle_id"],
            })

        # 2) 每次都以連結變更事件為分界，且只接受其後新開始的目錄掃描。
        trigger_and_wait_scan("by_id_link_remove", "remove")
        trigger_and_wait_scan("by_id_link_add", "restore")
        # 3) 窗口仍開啟的證明：舊 daemon 尚未停止且仍回應 health.ping（不經 service lock）。
        assert daemon.proc.pid == frozen_pid and daemon.proc.poll() is None, (
            f"{HANDOFF_DRAIN_GRACE_S:g} s 排空窗口已結束，背景觸發未位於窗口內，須調整時序"
        )
        ping_started_ns = time.monotonic_ns()
        ping = rpc(env, "health.ping")
        ping_completed_ns = time.monotonic_ns()
        assert ping_completed_ns <= window_deadline_ns, "最後 health.ping 未於 3 秒排空窗口內完成"
        assert ping.get("ok") is True and ping.get("pong") is True, ping
        assert tx_bytes(bridge.evidence_dir) == tx_at_freeze, "排空窗口內背景觸發產生 DUT TX"
        assert len(scan_trace) == 2, scan_trace
        assert scan_trace[0]["action"] == "by_id_link_remove"
        assert scan_trace[1]["action"] == "by_id_link_add"
        for row in scan_trace:
            assert row["action_completed_ns"] <= row["scan_open_observed_ns"], row
            assert row["scan_process_pid"] == daemon.proc.pid, row
            assert row["scan_thread_name"] == "serialwrap-device-watcher"[:15], row
            assert row["scan_thread_id"] == row["scan_close_thread_id"], row
            assert row["scan_open_handle_id"] == row["scan_close_handle_id"], row
        assert int(scan_trace[0]["scan_close_sequence"]) < int(scan_trace[1]["link_event_sequence"]), scan_trace
        assert int(scan_trace[1]["scan_close_observed_ns"]) <= ping_started_ns, scan_trace
        window_evidence = {
            "window_started_lower_bound_ns": window_started_lower_bound_ns,
            "window_deadline_ns": window_deadline_ns,
            "daemon_pid": daemon.proc.pid,
            "handoff_request": handoff_request,
            "marker_delay": marker_delay,
            "pause_marker_observed_ns": pause_marker_observed_ns,
            "request_to_marker_delay_ns": pause_marker_observed_ns - handoff_request["observed_ns"],
            "scan_trace": scan_trace,
            "ping_started_ns": ping_started_ns,
            "ping_completed_ns": ping_completed_ns,
        }
        diagnostic_path = os.environ.get("SW_CI_THIN_DIAG_PATH")
        if diagnostic_path:
            Path(diagnostic_path).write_text(
                json.dumps(window_evidence, sort_keys=True) + "\n", encoding="utf-8",
            )
        print(json.dumps(window_evidence, sort_keys=True))
        # 4) 整段窗口：舊 daemon 停止即窗口結束；以 bridge mark 作為擷取排空屏障後，全段 TX 須零增量。
        deadline = time.monotonic() + HANDOFF_DRAIN_GRACE_S + 10.0
        while daemon.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert daemon.proc.poll() is not None, "舊 daemon 未於排空窗口結束後停止"
        bridge.mark("handoff-window-end", os.getpid())
        assert_no_tx_after(bridge.evidence_dir, tx_at_freeze, marker.encode())
        resume_pause(pause_dir, "handoff_stop:replied")
        update_thread.join(120.0)
        assert not update_thread.is_alive(), "setup update 未在 120 秒內結束"
        update = update_rows[0]
        assert update.returncode == 0, update.stdout + update.stderr
        assert installed_version(env) == NEW_BUNDLE_VERSION
        assert wait_session(env)["state"] == "READY"
    finally:
        if console_fd is not None:
            os.close(console_fd)
        scan_observer.close()
        if update_thread.is_alive():
            resume_pause(pause_dir, "handoff_stop:replied")
            update_thread.join(15.0)
        env.pop("SW_TEST_UPDATE_PAUSE_AFTER", None)
        env.pop("SW_TEST_UPDATE_PAUSE_DIR", None)
        env.pop("SW_TEST_MARKER_WRITE_DELAY_MS", None)
        _stop_rig(bridge, daemon, new_cli, env)


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.supervisor_runtime
def test_update_systemd_exit1_not_relaunched_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """經 run-systemd.sh 實跑兩版 bundle：舊 daemon exit 1 時 NRestarts 為零、holder 不重疊且新版本就緒。"""
    old, new = update_bundles()
    # 比照 release／run-upgrade：容器只有系統 Python，不能使用綁定 hosted toolcache 的 venv。
    for bundle, version in ((old, OLD_BUNDLE_VERSION), (new, NEW_BUNDLE_VERSION)):
        cfg = bundle / "versions" / version / "py" / "pyvenv.cfg"
        assert cfg.is_file(), f"Docker bundle 缺少 runtime 設定：{cfg}"
        runtime: dict[str, str] = {}
        for line in cfg.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                runtime[key.strip()] = value.strip()
        assert runtime.get("home") == "/usr/bin", f"Docker bundle 必須 home = /usr/bin：{cfg}\n{runtime}"
        assert runtime.get("include-system-site-packages") == "false", (
            f"Docker bundle 必須 include-system-site-packages = false：{cfg}\n{runtime}"
        )
    run_id = f"g5-rust-refactor-{os.getpid()}-{time.time_ns()}"
    out = repo_root() / "target" / "g5-tmp" / f"systemd-evidence-{run_id}"
    script = repo_root() / "scripts" / "docker" / "run-systemd.sh"
    run_env = dict(os.environ)
    run_env["SW_DOCKER_RUN_ID"] = run_id
    try:
        result = subprocess.run(
            ["bash", str(script), "--mode", "systemd-user", "--scenario", "update-exit1",
             "--bundle-old", str(old), "--bundle-new", str(new), "--out", str(out)],
            cwd=repo_root(), env=run_env, capture_output=True, text=True, timeout=2400, check=False,
        )
        report_path = out / "l15-docker-report.json"
        assert report_path.is_file(), result.stdout + result.stderr
        report = json.loads(report_path.read_text(encoding="utf-8"))
        # 字典的 pytest repr 會省略 runner_error／reason；保留完整 JSON 與 runner 輸出以追查 CI 環境差異。
        diagnostic = (
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            + f"\nrunner rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert report.get("evidence_layer") == "L1.5-docker", diagnostic
        assert report.get("producer") == "run-systemd.sh", diagnostic
        assert report.get("results", [{}])[0].get("case_id") == "update-exit1", diagnostic
        assert report.get("results", [{}])[0].get("verdict") == "PASS", diagnostic
        assert result.returncode == 0, diagnostic
    finally:
        # 報告與 journal 先複製到 worktree 的穩定位置再刪暫存目錄；刪除失敗直接拋出，不靜默吞錯。
        if out.is_dir():
            keep = repo_root() / "target" / "systemd-evidence-kept"
            keep.mkdir(parents=True, exist_ok=True)
            for src in out.iterdir():
                if src.is_file():
                    shutil.copy2(src, keep / f"{out.name}-{src.name}")
            shutil.rmtree(out)
