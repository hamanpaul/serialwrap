"""P8（Slice 2.4）黑箱共用 fixture（計畫 09 Task 0 Interfaces；Task 6、9、11、12、14 共用）。

建構於 P3 harness 之上，不重寫 daemon 啟停：
- CLI 一律經 `bb_harness.run_cli`（受測 artifact 由 `SERIALWRAP_CLI_EXE` 決定）；
- daemon 一律經 `bb_daemon.PyDaemon`（`daemon_prefix()`＝Python 基線或 `SERIALWRAP_DAEMON_EXE` 指向的 Rust artifact），
  stop 時由 PyDaemon 確認主行程、子行程與 socket 皆已消失（任一殘留即 raise）；
- 環境一律經 `bb_harness.isolated_env`（RUN／STATE／WAL／CONFIG／BY_ID／BY_PATH… 全部指向案例暫存目錄）。

fixture：`sw_env`（`SwEnv`，function scope，teardown 一律 stop＋清除）、`shell_target`（`shelltarget.ShellTarget`）、
`fake_mcu`（`fakemcu.FakeMcu`）、`throwaway_daemon`（factory：另一組完全獨立目錄的 `SwEnv`）。
`ShellTarget`／`FakeMcu`／`FakeFlasher`／`ExternalHolder`／`HolderSampler` 由各自模組提供，本檔再匯出。

外部 oracle 原則（Review Focus 6）：「零 TX」「不重送」以 DUT 端 `rx_log()` 判定；`SwEnv.wal_tx()` 只供關聯，不得當 oracle。
本模組於 conftest 載入時 import，Windows 也必須可 import：termios／pty／fcntl 一律在 POSIX 專用函式內延遲 import。
stdlib＋pytest，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import shutil
import socket as _socket
import tempfile
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from bb_daemon import PyDaemon
from bb_harness import ab_label, daemon_prefix, isolated_env, run_cli
from extholder import ExternalHolder, ExternalHolderOpenError
from fakeflasher import FakeFlasher
from fakemcu import FakeMcu
from holder_sampler import HolderSampler, scan_holders
from shelltarget import PROMPT, ShellTarget


class _Any:
    """與任何值相等（用於 `version` 等不比對的欄位）。"""

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    def __repr__(self) -> str:
        return "ANY"

    __hash__ = None  # type: ignore[assignment]


ANY: object = _Any()


def drop_version(d: dict) -> dict:
    """回傳去掉頂層 `version` 鍵的淺拷貝（`None` 原樣回傳，方便直接套在 `CliRun.json`）。"""
    if d is None:
        return d
    return {k: v for k, v in d.items() if k != "version"}


@dataclass(frozen=True)
class CliRun:
    exit: int
    json: dict | None
    stdout: bytes
    stderr: str


# ---------------------------------------------------------------------------
# profile 樣板
# ---------------------------------------------------------------------------

_SHELL_PROMPT_REGEX = "(?m)^root@sw-sim:.*# "
_PROFILE_NAMES = {"shell": "p8-shell", "passthrough": "p8-passthrough"}


def _yaml_str(s: str) -> str:
    """YAML 單引號字串（只需把 `'` 加倍）。"""
    return "'" + s.replace("'", "''") + "'"


def _profile_yaml(profile: str, uart_baud: int, targets: list[tuple[str, str]]) -> str:
    if profile not in _PROFILE_NAMES:
        raise ValueError(f"未知 profile {profile!r}（支援 {sorted(_PROFILE_NAMES)}）")
    name = _PROFILE_NAMES[profile]
    lines = ["profiles:", f"  {name}:"]
    if profile == "shell":
        lines += [
            "    platform: shell",
            f"    prompt_regex: {_yaml_str(_SHELL_PROMPT_REGEX)}",
            "    timeout_s: 10",
        ]
    else:
        lines += [
            "    platform: passthrough",
            f"    prompt_regex: {_yaml_str('(?m)^MCU> ')}",
            "    ready_probe: ''",
        ]
    lines += [
        "    uart:",
        f"      baud: {int(uart_baud)}",
        "      data_bits: 8",
        "      parity: N",
        "      stop_bits: 1",
        "      flow_control: none",
        "      xonxoff: false",
    ]
    if targets:
        lines.append("targets:")
        for com, link in targets:
            lines += [f"  - profile: {name}", f"    com: {com}", f"    device_by_id: {_yaml_str(link)}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# SwEnv
# ---------------------------------------------------------------------------

def _free_tcp_port() -> int:
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _rpc_call(endpoint: str, method: str, params: dict, timeout_s: float) -> dict:
    """最小 NDJSON client（同 bb_daemon／slice21 harness）：連線或解析失敗直接拋例外。"""
    if endpoint.startswith("tcp://"):
        host, port = endpoint[len("tcp://"):].rsplit(":", 1)
        sock = _socket.create_connection((host, int(port)), timeout=timeout_s)
    else:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(timeout_s)
        try:
            sock.connect(endpoint)
        except OSError:
            sock.close()
            raise
    try:
        req = json.dumps({"id": 1, "method": method, "params": params}, ensure_ascii=False).encode("utf-8")
        sock.sendall(req + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    line = buf.split(b"\n", 1)[0].strip()
    if not line:
        raise ConnectionError(f"{method}: daemon 未回應")
    obj = json.loads(line.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"{method}: 回應不是 object：{obj!r}")
    return obj


# 已建立的 SwEnv（弱參照）：target fixture 收尾前先停掉仍開著該 tty 的 daemon（見 `_stop_envs_using`）。
_LIVE_ENVS: "weakref.WeakSet[SwEnv]" = weakref.WeakSet()


def _stop_envs_using(slave_path: str) -> None:
    """停止所有以 `slave_path` 建立 by-id 連結的 SwEnv daemon。

    pytest 依 setup 反序收尾：`def test(sw_env, shell_target)` 會先關 PTY 再停 daemon，daemon 因而在 tty hangup 下停止；
    target fixture 收尾時先呼叫本函式，讓 daemon 在 tty 仍存在時有序停止（殘留仍由 PyDaemon.stop raise）。
    """
    if not slave_path:
        return
    for env in list(_LIVE_ENVS):
        if slave_path in env._links.values():
            env.stop()


class SwEnv:
    """一個隔離的 serialwrap 實例（隔離 env＋profile＋by-id＋daemon），所有目錄都在 `base` 之下。

    POSIX：endpoint 為短路徑暫存目錄內的 AF_UNIX socket（AF_UNIX 路徑上限約 108 bytes，不放 `base`）；
    Windows：endpoint 為 `tcp://127.0.0.1:<空閒 port>`，`socket` 為不會存在的佔位路徑（Windows 路徑未在本環境實測）。
    """

    def __init__(self, base: Path) -> None:
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.label: str = ab_label()
        self.daemon_exe: list[str] = daemon_prefix()
        self._env: dict[str, str] = isolated_env(self.base)
        self.run_dir = Path(self._env["SERIALWRAP_RUN_DIR"])
        self.state_dir = Path(self._env["SERIALWRAP_STATE_DIR"])
        self.wal_dir = Path(self._env["SERIALWRAP_WAL_DIR"])
        self.by_id_dir = Path(self._env["SERIALWRAP_BY_ID_DIR"])
        self.by_path_dir = Path(self._env["SERIALWRAP_BY_PATH_DIR"])
        self.profile_dir = self.base / "profiles"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._env["SERIALWRAP_PROFILE_DIR"] = str(self.profile_dir)
        self.lock = self.base / "daemon" / "serialwrapd.lock"
        self.wal_path = self.wal_dir / "raw.wal.ndjson"
        if os.name == "nt":
            self._sock_root: Path | None = None
            self.endpoint = f"tcp://127.0.0.1:{_free_tcp_port()}"
            self.socket = self.run_dir / "serialwrapd.endpoint-unused"
        else:
            self._sock_root = Path(tempfile.mkdtemp(prefix="swp8-"))
            self.socket = self._sock_root / "d.sock"
            self.endpoint = str(self.socket)
        self._daemon: PyDaemon | None = None
        self._links: dict[str, str] = {}        # by-id 名稱 → slave 實體路徑（unplug／replug 用）
        self._prepared = False
        self._closed = False
        self._handoff_only_stop = False
        _LIVE_ENVS.add(self)

    # ---------------------------------------------------------------- 環境與設定
    def isolated_env(self) -> dict[str, str]:
        return dict(self._env)

    def add_by_id(self, name: str, slave_path: str) -> Path:
        """在隔離 by-id 目錄建立 `name` → `slave_path` 的 symlink（已存在則取代）。"""
        link = self.by_id_dir / name
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(slave_path, link)
        self._links[name] = slave_path
        return link

    def _link_target(self, com: str, target: object) -> Path:
        if isinstance(target, (str, os.PathLike)):
            return self.add_by_id(f"dut-{com.lower()}", os.fspath(target))
        slave = getattr(target, "slave_path", "")
        if not slave:
            raise ValueError(f"{com} 的 target 尚未啟動（slave_path 為空）")
        name = getattr(target, "by_id_name", None) or f"dut-{com.lower()}"
        return self.add_by_id(name, slave)

    def prepare(self, *, targets: Mapping[str, object], profile: str = "shell", uart_baud: int = 115200) -> None:
        """寫 profile YAML 與 by-id symlink，不啟動 daemon。`targets` 的值為已啟動的 ShellTarget／FakeMcu 或 slave 路徑；
        空 dict＝不寫 `targets` 區段（全走動態偵測，供 `throwaway_daemon(by_id=…)`）。"""
        rows = [(com, str(self._link_target(com, t))) for com, t in targets.items()]
        (self.profile_dir / "p8.yaml").write_text(_profile_yaml(profile, uart_baud, rows), encoding="utf-8")
        self._prepared = True

    # ---------------------------------------------------------------- daemon 生命週期
    def start(self, *, targets: Mapping[str, object], profile: str = "shell", uart_baud: int = 115200,
              timeout_s: float = 15.0) -> None:
        self.prepare(targets=targets, profile=profile, uart_baud=uart_baud)
        self._start_daemon(timeout_s)

    def _start_daemon(self, timeout_s: float = 15.0) -> None:
        if self._daemon is not None:
            raise RuntimeError("daemon 已在執行")
        if not self._prepared:
            raise RuntimeError("尚未 prepare（profile／by-id 未寫入）")
        d = PyDaemon(
            self._env,
            profile_dir=self.profile_dir,
            socket=self.endpoint,
            lock=self.lock,
            daemon_command=self.daemon_exe,
            handoff_only_stop=self._handoff_only_stop,
        )
        d.start(timeout_s=timeout_s)
        self._daemon = d

    def stop(self, timeout_s: float = 5.0) -> None:
        """依環境停止模式收尾；一般使用 daemon.stop，跨階段案例可強制只用 handoff_stop。"""
        d = self._daemon
        if d is not None:
            d.stop(timeout_s=timeout_s, handoff_only=self._handoff_only_stop)
            self._daemon = None

    def require_handoff_stop(self) -> None:
        """此環境的 daemon 只能透過 daemon.handoff_stop 停止；清理不得降級成 signal。"""
        self._handoff_only_stop = True

    def stop_via_handoff(self, timeout_s: float = 15.0) -> None:
        """透過 daemon.handoff_stop 停止目前 endpoint 的 daemon，包含 rollback 後替換的行程。"""
        self.require_handoff_stop()
        self.stop(timeout_s=timeout_s)

    def restart(self) -> None:
        self.stop()
        self._start_daemon()

    @property
    def daemon_pid(self) -> int | None:
        return None if self._daemon is None or self._daemon.proc is None else self._daemon.proc.pid

    def stderr_tail(self, n: int = 2000) -> str:
        try:
            return (self.lock.parent / "daemon.stderr").read_text(encoding="utf-8", errors="replace")[-n:]
        except OSError:
            return ""

    def close(self) -> None:
        """fixture teardown：stop daemon（殘留即 raise）並清除短路徑 socket 目錄。冪等。"""
        if self._closed:
            return
        self._closed = True
        try:
            self.stop()
        finally:
            if self._sock_root is not None and (not self._handoff_only_stop or self._daemon is None):
                shutil.rmtree(self._sock_root, ignore_errors=True)

    # ---------------------------------------------------------------- CLI／RPC
    def cli(self, *args: str, timeout_s: float = 120.0) -> CliRun:
        r = run_cli(["--socket", self.endpoint, *args], env=self._env, timeout_s=timeout_s)
        obj: dict | None = None
        text = r.stdout.decode("utf-8", errors="replace").strip()
        if text:
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                obj = parsed
        return CliRun(exit=r.rc, json=obj, stdout=r.stdout, stderr=r.stderr.decode("utf-8", errors="replace"))

    def rpc(self, method: str, params: dict, timeout_s: float = 30.0) -> dict:
        return _rpc_call(self.endpoint, method, dict(params), timeout_s)

    def rpc_async(self, method: str, params: dict, timeout_s: float = 600.0) -> concurrent.futures.Future:
        """背景 thread 送 RPC；回傳 Future（結果或例外）。thread 為 daemon thread，daemon 停止時連線 EOF 即結束。"""
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def _run() -> None:
            if not fut.set_running_or_notify_cancel():
                return
            try:
                fut.set_result(self.rpc(method, params, timeout_s))
            except BaseException as exc:  # noqa: BLE001 — 例外交給呼叫端
                fut.set_exception(exc)

        threading.Thread(target=_run, name=f"rpc-async-{method}", daemon=True).start()
        return fut

    def rpc_ok(self, method: str) -> bool:
        try:
            return bool(self.rpc(method, {}, timeout_s=2.0).get("ok"))
        except (OSError, ValueError):
            return False

    def session(self, com: str) -> dict:
        r = self.rpc("session.get_state", {"selector": com})
        if not r.get("ok") or not isinstance(r.get("session"), dict):
            raise AssertionError(f"session.get_state({com}) 失敗：{r!r}")
        return r["session"]

    def wait_until(self, pred: Callable[[], bool], timeout: float, interval: float = 0.05) -> None:
        deadline = time.monotonic() + timeout
        last_exc: BaseException | None = None
        while True:
            try:
                if pred():
                    return
                last_exc = None
            except (OSError, ValueError, AssertionError, KeyError) as exc:
                last_exc = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(interval)
        raise AssertionError(f"條件未在 {timeout}s 內成立（最後例外：{last_exc!r}）；daemon stderr 尾段：{self.stderr_tail()!r}")

    def wait_state(self, com: str, state: str, timeout: float) -> None:
        last: dict[str, Any] = {}

        def _pred() -> bool:
            last["session"] = self.session(com)
            return last["session"].get("state") == state

        try:
            self.wait_until(_pred, timeout, interval=0.1)
        except AssertionError as exc:
            raise AssertionError(f"{com} 未在 {timeout}s 內進入 {state}；最後觀察：{last.get('session')!r}；{exc}") from None

    # ---------------------------------------------------------------- 觀察（只供關聯）
    def wal_records(self) -> list[dict]:
        """讀目前的 WAL 檔（輪替歸檔不列入）；行尾容許舊格式 `\\r\\n`。"""
        try:
            raw = self.wal_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        out: list[dict] = []
        for line in raw.split("\n"):
            line = line.rstrip("\r\n")
            if line.strip():
                out.append(json.loads(line))
        return out

    def wal_tx(self, com: str) -> list[dict]:
        """`com` 的 TX 紀錄（另附解碼後的 `payload` bytes）。只供關聯，不得當「零 TX／不重送」的 oracle。"""
        out = []
        for rec in self.wal_records():
            if rec.get("dir") == "TX" and rec.get("com") == com:
                rec = dict(rec)
                rec["payload"] = base64.b64decode(rec.get("payload_b64") or "")
                out.append(rec)
        return out

    def holders(self, path: str, *, exclude_self: bool) -> list[int]:
        """開著 `path` 的 pid（掃 `/proc/*/fd`；POSIX）。`exclude_self` 排除本測試行程。"""
        return scan_holders(path, exclude=(os.getpid(),) if exclude_self else ())

    def unplug(self, name: str) -> None:
        link = self.by_id_dir / name
        if name not in self._links:
            raise KeyError(f"by-id {name!r} 不是本 SwEnv 建立的連結")
        if link.is_symlink() or link.exists():
            link.unlink()

    def replug(self, name: str) -> None:
        if name not in self._links:
            raise KeyError(f"by-id {name!r} 不是本 SwEnv 建立的連結")
        self.add_by_id(name, self._links[name])


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sw_env(tmp_path: Path):
    env = SwEnv(tmp_path / "sw")
    try:
        yield env
    finally:
        env.close()


@pytest.fixture
def shell_target(tmp_path: Path):
    st = ShellTarget(tmp_path / "dut-root").start()
    try:
        yield st
    finally:
        try:
            _stop_envs_using(st.slave_path)
        finally:
            st.stop()


@pytest.fixture
def fake_mcu():
    mcu = FakeMcu().start()
    try:
        yield mcu
    finally:
        try:
            _stop_envs_using(mcu.slave_path)
        finally:
            mcu.stop()


@pytest.fixture
def throwaway_daemon(tmp_path: Path):
    """factory：`throwaway_daemon(targets=…｜by_id={name: slave_path}, profile=…, start=True) -> SwEnv`。

    每次呼叫建立完全獨立的 RUN／STATE／WAL／BY_ID／BY_PATH／CONFIG（`isolated_env`）與 socket；teardown 依建立的反序 close。
    `by_id` 只建 by-id 連結、不寫 `targets` 區段（全走動態偵測）；`targets` 與 `SwEnv.start` 相同。
    """
    made: list[SwEnv] = []

    def _make(*, targets: Mapping[str, object] | None = None, by_id: Mapping[str, str] | None = None,
              profile: str = "shell", uart_baud: int = 115200, start: bool = True, timeout_s: float = 15.0) -> SwEnv:
        env = SwEnv(tmp_path / f"throwaway{len(made)}")
        made.append(env)
        for name, slave in (by_id or {}).items():
            env.add_by_id(name, slave)
        env.prepare(targets=targets or {}, profile=profile, uart_baud=uart_baud)
        if start:
            env._start_daemon(timeout_s)
        return env

    try:
        yield _make
    finally:
        errors: list[BaseException] = []
        for env in reversed(made):
            try:
                env.close()
            except BaseException as exc:  # noqa: BLE001 — 全部收尾後再拋
                errors.append(exc)
        if errors:
            raise errors[0]


__all__ = [
    "ANY", "CliRun", "SwEnv", "drop_version", "PROMPT",
    "ShellTarget", "FakeMcu", "FakeFlasher", "ExternalHolder", "ExternalHolderOpenError", "HolderSampler",
    "scan_holders", "sw_env", "shell_target", "fake_mcu", "throwaway_daemon",
]
