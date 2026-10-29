"""OD-33 R2(b)：容器內升級驗證 driver（以 root 執行，產品命令一律以 swtest 身分經 runuser 執行）。

流程（每種監管模式一個全新容器）：pipx 安裝 0.3.2 正式版並 setup → 建立 profile／alias／binding →
舊 daemon 執行中直接以 0.4.0 bundle setup（不得 two-reader、拒絕時不得動到現場）→ 停舊 daemon 後 setup →
驗證入口取代、設定保留、監管 reconcile、狀態相容與 doctor → `setup --rollback` → 手動降級回 0.3.2 →
重新升級。全程以 /proc 取樣監看 serialwrapd 實例數。判定見 upgrade_checks.py。

systemd-system（OD-48 SPEC-GAP #10）只跑「0.3.2 daemon 執行中直接 `setup --system --with-sudo`」成功路徑：
0.3.2 以 `setup --system --with-sudo` 安裝 system unit（swtest 免密碼 sudo）→ 建立 profile／alias／binding →
0.4.0 直接 setup → 入口、設定、監管與 doctor 驗證；必要檢查見 `REQUIRED_CHECKS_SYSTEMD_SYSTEM`。

只依賴標準函式庫與同目錄的 upgrade_checks（容器內以系統 python3 執行）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import traceback
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import upgrade_checks as uc  # noqa: E402

USER = "swtest"
HOME = pathlib.Path("/home/swtest")
BIN = HOME / ".local" / "bin"
CONFIG = HOME / ".config" / "serialwrap"
VERSIONS = HOME / ".local" / "share" / "serialwrap" / "versions"
BACKUPS = HOME / ".local" / "share" / "serialwrap" / "backups"
UNIT_DIR = HOME / ".config" / "systemd" / "user"
SYSTEM_UNIT_DIR = pathlib.Path("/etc/systemd/system")
SYSTEM_PROFILE_DIR = pathlib.Path("/etc/serialwrap/profiles")
OLD_VERSION = "0.3.2"
NEW_VERSION = "0.4.0"
ENTRY_NAMES = ("serialwrap", "serialwrapd")
RUNUSER = shutil.which("runuser") or "/usr/sbin/runuser"

BENCH_PROFILE = """\
profiles:
  shellboard:
    platform: shell
    prompt_regex: "# $"
    timeout_s: 15
targets:
  - act_no: 7
    com: COM7
    profile: shellboard
    device_by_id: /dev/serial/by-id/usb-UPGRADE_TEST-if00-port0
"""


class Driver:
    def __init__(self, mode: str, wheel: pathlib.Path, bundle: pathlib.Path, out: pathlib.Path) -> None:
        self.mode = mode
        self.flag = {"systemd-user": "--user", "systemd-system": "--system"}.get(mode, "--on-demand")
        self.setup_args = [self.flag, "--with-sudo"] if mode == "systemd-system" else [self.flag]
        self.wheel = wheel
        self.bundle = bundle
        self.new_cli = bundle / "bin" / "serialwrap"
        self.out = out
        self.rows: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.samples: list[tuple[float, list[int]]] = []
        self._stop = threading.Event()
        self._log = (out / "steps.ndjson").open("a", encoding="utf-8")
        self.env = {
            "HOME": str(HOME), "USER": USER, "LOGNAME": USER, "SHELL": "/bin/bash", "LANG": "C.UTF-8",
            "XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "PATH": f"{BIN}:/usr/local/bin:/usr/bin:/bin",
        }

    # ---------- 基礎 ----------
    def run(self, argv: list[str], *, timeout: float = 300.0, label: str = "") -> subprocess.CompletedProcess[str]:
        command = [RUNUSER, "-u", USER, "--preserve-environment", "--", *argv]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(command, cwd=str(HOME), env=self.env, capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            proc = subprocess.CompletedProcess(command, 124, exc.stdout or "", f"timeout {timeout}s")
            if isinstance(proc.stdout, bytes):
                proc.stdout = proc.stdout.decode("utf-8", "replace")
        self._log.write(json.dumps({"label": label, "argv": argv, "rc": proc.returncode,
                                    "elapsed_s": round(time.monotonic() - t0, 3),
                                    "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]},
                                   ensure_ascii=False, sort_keys=True) + "\n")
        self._log.flush()
        return proc

    @staticmethod
    def parse(proc: subprocess.CompletedProcess[str]) -> dict[str, Any] | None:
        for line in reversed(proc.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                return value if isinstance(value, dict) else None
        return None

    def sw(self, *args: str, label: str = "", timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
        """以 PATH 上的 serialwrap（目前生效的安裝）執行。"""
        return self.run(["serialwrap", *args], label=label or "serialwrap " + " ".join(args), timeout=timeout)

    def add(self, r: dict[str, Any]) -> None:
        self.rows.append(r)

    def entries(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for name in ENTRY_NAMES:
            path = BIN / name
            out[name] = os.readlink(path) if path.is_symlink() else (str(path) if path.exists() else None)
        return out

    def cli_version(self) -> str:
        proc = self.sw("--version", label="version")
        return proc.stdout.strip()

    def daemon_status(self) -> dict[str, Any] | None:
        return self.parse(self.sw("daemon", "status", label="daemon status"))

    def wait_daemon(self, *, version: str | None, timeout: float = 60.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        status = None
        while time.monotonic() < deadline:
            status = self.daemon_status()
            if status and status.get("ok") and status.get("running") is not False and status.get("pid"):
                if version is None or status.get("version") == version:
                    return status
            time.sleep(0.5)
        return status

    def wait_no_daemon(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not uc.scan_daemon_instances():
                return True
            time.sleep(0.2)
        return False

    def start_daemon(self, label: str) -> subprocess.CompletedProcess[str]:
        if self.mode == "systemd-user":
            return self.sw("service", "start", label=label)
        proc = self.sw("daemon", "start", label=label)
        if proc.returncode == 0 or not self.cli_version().endswith(OLD_VERSION):
            return proc
        # 0.3.2 既有缺陷：POSIX `daemon start` 固定以 site-packages 上一層的 serialwrapd.py 啟動，pipx wheel 不含該檔，
        # on-demand 模式必然 DAEMON_EXITED（與 0.4.0 無關）。以前景模式確認特徵後，改用 pipx 的 serialwrapd 入口背景啟動。
        probe = self.run(["timeout", "10", "serialwrap", "daemon", "start", "--foreground"], label=label + "（前景診斷）")
        if "serialwrapd.py" not in probe.stderr:
            return proc
        self.observations.append({"id": "OBS-0.3.2-ONDEMAND-START", "phase": label,
                                  "detail": probe.stderr.strip()[-300:],
                                  "workaround": "setsid -f ~/.local/bin/serialwrapd（pipx 入口）"})
        log = HOME / ".local" / "state" / "serialwrapd-0.3.2.log"
        return self.run(["setsid", "-f", "sh", "-c", f'exec "$HOME/.local/bin/serialwrapd" >>"{log}" 2>&1 </dev/null'],
                        label=label + "（pipx 入口背景啟動）")

    def state_view(self) -> dict[str, Any]:
        aliases = self.parse(self.sw("alias", "list", label="alias list")) or {}
        sessions = self.parse(self.sw("session", "list", label="session list")) or {}
        return {"aliases": aliases.get("aliases") or [], "sessions": sessions.get("sessions") or []}

    def doctor(self, label: str) -> dict[str, Any] | None:
        return self.parse(self.sw("doctor", label=label))

    def systemctl_user(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run(["systemctl", "--user", *args], label="systemctl --user " + " ".join(args), timeout=30)

    # ---------- 監看 ----------
    def _monitor(self) -> None:
        while not self._stop.is_set():
            self.samples.append((time.monotonic(), uc.scan_daemon_instances()))
            time.sleep(0.02)

    def prepare_user_manager(self) -> None:
        subprocess.run(["loginctl", "enable-linger", USER], capture_output=True, check=False, timeout=30)
        subprocess.run(["systemctl", "start", "user@1000.service"], capture_output=True, check=False, timeout=60)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not pathlib.Path("/run/user/1000/bus").exists():
            time.sleep(0.2)
        if not pathlib.Path("/run/user/1000/bus").exists():
            raise TimeoutError("systemd-user D-Bus 未於 30 秒內就緒")

    # ---------- 流程 ----------
    def phase_old_install(self) -> None:
        inst = self.run(["pipx", "install", "--pip-args", "--no-index --find-links=/opt/sw-old", str(self.wheel)],
                        label="pipx install 0.3.2", timeout=600)
        if self.mode == "systemd-system":
            # 0.3.2 既有缺陷：system unit 的 UMask=0117 使 daemon 自建的目錄缺 x 位元（0660），接著建立子目錄即
            # PermissionError、unit 反覆重啟。一般使用者先前以 on-demand 用過 serialwrap 時這些目錄已存在；此處以 swtest
            # 的一般 umask 前景啟動一次 0.3.2 daemon（隔離 socket）讓它自建全部目錄後結束，並記為觀察項（與 0.4.0 無關）。
            # system unit 沒有 XDG_RUNTIME_DIR（RUN_DIR 落在 STATE_DIR/run），暖機須同樣不帶該變數。
            warm = self.run(["env", "-u", "XDG_RUNTIME_DIR", "timeout", "-s", "INT", "8", str(BIN / "serialwrapd"),
                             "--socket", "/tmp/sw-warmup.sock"],
                            label="0.3.2 daemon 前景暖機（建立目錄）", timeout=30)
            self.wait_no_daemon()
            self.observations.append({"id": "OBS-0.3.2-SYSTEM-UMASK", "phase": "old_install_0_3_2",
                                      "detail": "0.3.2 system unit UMask=0117：daemon 自建目錄為 0660，ensure_runtime_dirs／ttyMCU 目錄 PermissionError",
                                      "workaround": "以 swtest 一般 umask 前景啟動一次 0.3.2 daemon 建立目錄",
                                      "warmup_rc": warm.returncode})
        setup = self.sw("setup", *self.setup_args, label="0.3.2 setup")
        start = self.start_daemon("0.3.2 start") if self.mode == "on-demand" else None
        status = self.wait_daemon(version=OLD_VERSION)
        version = self.cli_version()
        ok = (inst.returncode == 0 and setup.returncode == 0 and (start is None or start.returncode == 0)
              and version == f"serialwrap {OLD_VERSION}" and bool(status) and status.get("version") == OLD_VERSION)
        self.add(uc.row("old_install_0_3_2", "PASS" if ok else "FAIL", None if ok else "0.3.2 正式安裝或啟動失敗",
                        pipx_rc=inst.returncode, setup_rc=setup.returncode, cli_version=version,
                        daemon_version=(status or {}).get("version"), entries=self.entries()))

    def cfg_files(self) -> dict[str, str]:
        """設定快照：使用者 config；systemd-system 另含 system daemon 讀取的 /etc/serialwrap/profiles。"""
        files = uc.snapshot_files(CONFIG)
        if self.mode == "systemd-system":
            # 套件內 profiles 由 system setup 每次以 `sudo cp -r <staging>/. /etc/serialwrap/profiles` 覆寫（0.3.2 同語意），
            # 屬 setup 管理檔而非使用者資料；只比對使用者自加的檔案。
            managed = getattr(self, "system_managed_profiles", set())
            files.update({f"/etc/serialwrap/profiles/{k}": v for k, v in uc.snapshot_files(SYSTEM_PROFILE_DIR).items()
                          if k not in managed})
        return files

    def phase_seed(self) -> None:
        (CONFIG / "profiles").mkdir(parents=True, exist_ok=True)
        bench = CONFIG / "profiles" / "bench.yaml"
        bench.write_text(BENCH_PROFILE, encoding="utf-8")
        subprocess.run(["chown", f"{USER}:{USER}", str(bench)], check=True)
        if self.mode == "systemd-system":
            # system daemon 固定讀 /etc/serialwrap/profiles（0.3.2 setup_cmd SYSTEM_PROFILE_DIR）；使用者 profile 另放一份。
            staging = HOME / ".local" / "share" / "serialwrap" / "system-profiles"
            self.system_managed_profiles = {str(p.relative_to(staging)) for p in staging.rglob("*") if p.is_file()} \
                if staging.is_dir() else set()
            self.observations.append({"id": "OBS-SYSTEM-PROFILES-MANAGED", "phase": "seed_user_data",
                                      "managed": sorted(self.system_managed_profiles),
                                      "note": "system setup 每次以套件內 profiles 覆寫 /etc/serialwrap/profiles 的同名檔（0.3.2 同語意）；保留判定只比對使用者自加的檔案"})
            SYSTEM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            (SYSTEM_PROFILE_DIR / "bench.yaml").write_text(BENCH_PROFILE, encoding="utf-8")
        if self.mode == "systemd-system":
            restart = [self.run(["sudo", "systemctl", "restart", "serialwrap"], label="0.3.2 sudo systemctl restart")]
        elif self.mode == "systemd-user":
            restart = [self.sw("service", "restart", label="0.3.2 service restart")]
        else:
            restart = [self.sw("daemon", "stop", label="0.3.2 daemon stop"), self.start_daemon("0.3.2 daemon start")]
        self.wait_daemon(version=OLD_VERSION)
        cmds = [
            self.sw("alias", "set", "--session-id", "shellboard:COM7", "--alias", "upgbench"),
            self.sw("session", "bind", "--selector", "COM7", "--device-by-id",
                    "/dev/serial/by-id/usb-UPGRADE_REBOUND-if00-port0"),
            self.sw("alias", "assign", "--by-id", "/dev/serial/by-id/usb-UPGRADE_PIN-if00-port0", "--alias", "pinned",
                    "--profile", "shellboard"),
        ]
        self.baseline = self.state_view()
        self.cfg_before = self.cfg_files()
        self.add(uc.judge_seed("seed_user_data", rcs=[p.returncode for p in restart + cmds], baseline=self.baseline,
                               config_files=self.cfg_before))
        self.add(uc.judge_doctor("doctor_green_old", self.doctor("0.3.2 doctor"), require_pairing=False))

    def _site(self) -> dict[str, Any]:
        return {"daemon_pids": uc.scan_daemon_instances(), "entries": self.entries(),
                "files": self.cfg_files()}

    @staticmethod
    def _start_ticks(pid: int) -> str | None:
        try:
            stat = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        fields = stat[stat.rfind(")") + 2:].split()
        return fields[19] if len(fields) > 19 else None

    def phase_upgrade_running(self) -> bool:
        before = self._site()
        old_status = self.daemon_status() or {}
        legacy = str(old_status.get("version") or OLD_VERSION).startswith("0.3.")
        old_ids = {pid: self._start_ticks(pid) for pid in before["daemon_pids"]}
        t0 = time.monotonic()
        proc = self.run([str(self.new_cli), "setup", *self.setup_args], label="0.4.0 setup（舊 daemon 執行中）", timeout=600)
        body = self.parse(proc) or {}
        if proc.returncode == 0 and body.get("ok"):
            # OD-48：在 driver 任何啟動動作之前取樣，確認新 daemon 由 setup 自己啟動。
            status = self.wait_daemon(version=NEW_VERSION, timeout=20.0)
            t1 = time.monotonic()
            alive = [pid for pid, ticks in old_ids.items() if ticks is not None and self._start_ticks(pid) == ticks]
            overlaps = [{"t": round(t - t0, 3), "pids": sorted(pids)} for t, pids in list(self.samples)
                        if t0 <= t <= t1 and len(pids) > 1]
            self.add(uc.row("upgrade_with_old_daemon_running", "PASS", None, outcome="switched"))
            self.add(uc.judge_legacy_direct_setup(
                "legacy_handoff_direct_setup", setup_rc=proc.returncode, body=body, old_pids=sorted(old_ids),
                old_alive_after=alive, status_after=status, instances_after=uc.scan_daemon_instances(),
                cfg_before=self.cfg_before, cfg_after=self.cfg_files(), overlaps=overlaps,
                new_version=NEW_VERSION, old_version=OLD_VERSION))
            return True
        self.observations.append({"id": "OBS-UPGRADE-LEGACY-HANDOFF", "phase": "upgrade_with_old_daemon_running",
                                  "error_code": body.get("error_code"), "probe": body.get("probe"),
                                  "message": body.get("message"), "hint": body.get("hint")})
        after = self._site()
        r = uc.judge_upgrade_running_refusal("upgrade_with_old_daemon_running", before, after, body, legacy=legacy)
        r["detail"]["outcome"] = "refused"
        self.add(r)
        self.add(uc.judge_legacy_direct_setup(
            "legacy_handoff_direct_setup", setup_rc=proc.returncode, body=body, old_pids=sorted(old_ids),
            old_alive_after=sorted(after["daemon_pids"]), status_after=None, instances_after=after["daemon_pids"],
            cfg_before=self.cfg_before, cfg_after=self.cfg_files(), overlaps=[],
            new_version=NEW_VERSION, old_version=OLD_VERSION))
        return False

    def phase_upgrade_after_stop(self, already: bool) -> None:
        if already:
            self.add(uc.row("upgrade_after_stop", "PASS", None, outcome="not_needed_handoff_succeeded"))
        else:
            stop = self.sw("daemon", "stop", label="0.3.2 daemon stop")
            gone = self.wait_no_daemon()
            proc = self.run([str(self.new_cli), "setup", *self.setup_args], label="0.4.0 setup", timeout=600)
            body = self.parse(proc) or {}
            install = body.get("install") or {}
            ok = stop.returncode == 0 and gone and proc.returncode == 0 and body.get("ok") is True \
                and install.get("op") == "install" and install.get("to_version") == NEW_VERSION
            start_step = next((s for s in install.get("steps") or [] if s.get("step") == "start_and_verify"), {})
            if (start_step.get("detail") or {}).get("started") is False:
                self.observations.append({"id": "OBS-UPGRADE-NOT-STARTED", "phase": "upgrade_after_stop",
                                          "detail": start_step.get("detail")})
            self.add(uc.row("upgrade_after_stop", "PASS" if ok else "FAIL", None if ok else "停舊 daemon 後 0.4.0 setup 失敗",
                            stop_rc=stop.returncode, old_gone=gone, setup_rc=proc.returncode,
                            op=install.get("op"), from_version=install.get("from_version"),
                            error_code=body.get("error_code"), backup_dir=install.get("backup_dir")))
        # 入口：~/.local/bin 兩個入口改指 0.4.0 版本目錄，舊 pipx 入口記錄在備份 entries.json
        entries = self.entries()
        target_dir = str(VERSIONS / NEW_VERSION / "bin")
        switched = all((entries.get(n) or "").startswith(target_dir + "/") for n in ENTRY_NAMES)
        version = self.cli_version()
        recorded = []
        for path in sorted(BACKUPS.glob(f"*-to-{NEW_VERSION}-*/entries.json")):
            try:
                recorded.extend(e.get("target") for e in json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, AttributeError):
                pass
        pipx_recorded = any(isinstance(t, str) and "/pipx/venvs/serialwrap/" in t for t in recorded)
        ok = switched and version == f"serialwrap {NEW_VERSION}" and pipx_recorded
        self.add(uc.row("entries_replaced", "PASS" if ok else "FAIL", None if ok else "入口未切到 0.4.0 或未記錄舊 pipx 入口",
                        entries=entries, cli_version=version, backup_targets=recorded,
                        pipx_venv_left=(HOME / ".local/share/pipx/venvs/serialwrap").is_dir()))
        cfg = uc.judge_files_preserved("config_preserved_upgrade", self.cfg_before, self.cfg_files())
        self.add(cfg)

    def phase_new_running(self) -> None:
        start = self.start_daemon("0.4.0 start")
        status = self.wait_daemon(version=NEW_VERSION) or {}
        pids = uc.scan_daemon_instances()
        argv = []
        if status.get("pid"):
            try:
                argv = pathlib.Path(f"/proc/{status['pid']}/cmdline").read_bytes().decode("utf-8", "replace").split("\0")
            except OSError:
                argv = []
        from_bundle = any(str(VERSIONS / NEW_VERSION) in a for a in argv)
        ok = start.returncode == 0 and status.get("version") == NEW_VERSION and status.get("multi_open") is False \
            and pids == [status.get("pid")] and from_bundle
        self.add(uc.row("new_daemon_started", "PASS" if ok else "FAIL", None if ok else "0.4.0 daemon 未正確啟動",
                        start_rc=start.returncode, version=status.get("version"), pid=status.get("pid"),
                        multi_open=status.get("multi_open"), instances=pids, argv=argv[:6]))
        self._check_supervision("supervision_reconciled", status.get("pid"))
        self.add(uc.judge_state_compatible(self.baseline, self.state_view(), "state_compatible_upgrade"))
        self.add(uc.judge_doctor("doctor_green_new", self.doctor("0.4.0 doctor"), require_pairing=True))

    def phase_new_running_system(self) -> None:
        """systemd-system：新 daemon 已由 setup 啟動（不得由 driver 代啟），驗證來源、監管收斂、狀態與 doctor。"""
        status = self.wait_daemon(version=NEW_VERSION) or {}
        pids = uc.scan_daemon_instances()
        argv = []
        if status.get("pid"):
            try:
                argv = pathlib.Path(f"/proc/{status['pid']}/cmdline").read_bytes().decode("utf-8", "replace").split("\0")
            except OSError:
                argv = []
        from_bundle = any(str(VERSIONS / NEW_VERSION) in a for a in argv)
        ok = status.get("version") == NEW_VERSION and status.get("multi_open") is False \
            and pids == [status.get("pid")] and from_bundle
        self.add(uc.row("new_daemon_started", "PASS" if ok else "FAIL", None if ok else "0.4.0 daemon 未由 setup 正確啟動",
                        started_by="setup", version=status.get("version"), pid=status.get("pid"),
                        multi_open=status.get("multi_open"), instances=pids, argv=argv[:6]))
        config_text = (CONFIG / "config.yaml").read_text(encoding="utf-8") if (CONFIG / "config.yaml").is_file() else ""
        mode_line = "supervision_mode: systemd-system" in config_text.splitlines()
        systemctl = lambda *a: self.run(["systemctl", *a], label="systemctl " + " ".join(a), timeout=30).stdout.strip()  # noqa: E731
        active = systemctl("is-active", "serialwrap")
        enabled = systemctl("is-enabled", "serialwrap")
        main_pid = systemctl("show", "serialwrap", "-p", "MainPID", "--value")
        dropin_dir = SYSTEM_UNIT_DIR / "serialwrap.service.d"
        dropins = sorted(p.name for p in dropin_dir.glob("*")) if dropin_dir.is_dir() else []
        ok = mode_line and active == "active" and enabled == "enabled" and main_pid == str(status.get("pid")) and not dropins
        self.add(uc.row("supervision_reconciled", "PASS" if ok else "FAIL", None if ok else "systemd-system 監管未依 config 收斂",
                        mode_in_config=mode_line, active=active, enabled=enabled, main_pid=main_pid,
                        daemon_pid=status.get("pid"), dropins=dropins))
        self.add(uc.judge_state_compatible(self.baseline, self.state_view(), "state_compatible_upgrade"))
        self.add(uc.judge_doctor("doctor_green_new", self.doctor("0.4.0 doctor"), require_pairing=True))
        self.run(["sudo", "systemctl", "stop", "serialwrap"], label="收尾 sudo systemctl stop")
        self.wait_no_daemon()

    def _check_supervision(self, check_id: str, pid: Any) -> None:
        config_text = (CONFIG / "config.yaml").read_text(encoding="utf-8") if (CONFIG / "config.yaml").is_file() else ""
        mode_line = f"supervision_mode: {self.mode}" in config_text.splitlines()
        active = self.systemctl_user("is-active", "serialwrap").stdout.strip()
        enabled = self.systemctl_user("is-enabled", "serialwrap").stdout.strip()
        main_pid = self.systemctl_user("show", "serialwrap", "-p", "MainPID", "--value").stdout.strip()
        dropins = sorted(p.name for p in (UNIT_DIR / "serialwrap.service.d").glob("*")) \
            if (UNIT_DIR / "serialwrap.service.d").is_dir() else []
        if self.mode == "systemd-user":
            ok = mode_line and active == "active" and enabled == "enabled" and main_pid == str(pid) and not dropins
        else:
            ok = mode_line and active != "active" and enabled != "enabled" and bool(pid)
        self.add(uc.row(check_id, "PASS" if ok else "FAIL", None if ok else "監管模式未依 config 收斂",
                        mode_in_config=mode_line, active=active, enabled=enabled, main_pid=main_pid,
                        daemon_pid=pid, dropins=dropins))

    def phase_rollback_command(self) -> None:
        before = self._site()
        proc = self.sw("setup", "--rollback", label="0.4.0 setup --rollback", timeout=600)
        body = self.parse(proc) or {}
        if proc.returncode == 0 and body.get("ok"):
            # 回滾成功時 CLI、daemon 都必須回到 0.3.2 且設定保留；之後的手動降級照常執行並另行判定
            status = self.wait_daemon(version=OLD_VERSION) or {}
            self.add(uc.judge_rollback_success("rollback_command", cli_version=self.cli_version(),
                                               daemon_version=status.get("version"), cfg_before=self.cfg_before,
                                               cfg_after=uc.snapshot_files(CONFIG), old_version=OLD_VERSION))
            return
        self.observations.append({"id": "OBS-UPGRADE-ROLLBACK-PIPX", "phase": "rollback_command",
                                  "error_code": body.get("error_code"), "reason": body.get("reason"),
                                  "message": body.get("message")})
        r = uc.judge_refusal("rollback_command", before, self._site(), body, uc.REFUSAL_ROLLBACK_UNAVAILABLE)
        r["detail"]["outcome"] = "refused"
        self.add(r)

    def phase_downgrade(self) -> None:
        """release-0.4.0-plan.md 的手動降級程序：停 daemon → 移除指向版本目錄的入口 → pipx 重新安裝 0.3.2 → setup → 啟動。"""
        steps = [self.sw("daemon", "stop", label="0.4.0 daemon stop")]
        gone = self.wait_no_daemon()
        removed = []
        for name in ENTRY_NAMES:
            path = BIN / name
            if path.is_symlink() and os.readlink(path).startswith(str(VERSIONS) + "/"):
                path.unlink()
                removed.append(name)
        steps.append(self.run(["pipx", "uninstall", "serialwrap"], label="pipx uninstall"))
        steps.append(self.run(["pipx", "install", "--pip-args", "--no-index --find-links=/opt/sw-old", str(self.wheel)],
                              label="pipx install 0.3.2（降級）", timeout=600))
        steps.append(self.sw("setup", self.flag, label="0.3.2 setup（降級）"))
        steps.append(self.start_daemon("0.3.2 start（降級）"))
        status = self.wait_daemon(version=OLD_VERSION) or {}
        version = self.cli_version()
        ok = all(p.returncode == 0 for p in steps) and gone and version == f"serialwrap {OLD_VERSION}" \
            and status.get("version") == OLD_VERSION and uc.scan_daemon_instances() == [status.get("pid")]
        self.add(uc.row("downgrade_manual", "PASS" if ok else "FAIL", None if ok else "手動降級回 0.3.2 失敗",
                        rcs=[p.returncode for p in steps], old_gone=gone, removed_entries=removed,
                        cli_version=version, daemon_version=status.get("version"), entries=self.entries()))
        self.add(uc.judge_files_preserved("config_preserved_downgrade", self.cfg_before, uc.snapshot_files(CONFIG)))
        self.add(uc.judge_state_compatible(self.baseline, self.state_view(), "state_compatible_downgrade"))
        self.add(uc.judge_doctor("doctor_green_downgrade", self.doctor("0.3.2 doctor（降級）"), require_pairing=False))

    def phase_reupgrade(self) -> None:
        stop = self.sw("daemon", "stop", label="0.3.2 daemon stop（重新升級）")
        gone = self.wait_no_daemon()
        proc = self.run([str(self.new_cli), "setup", self.flag], label="0.4.0 setup（重新升級）", timeout=600)
        body = self.parse(proc) or {}
        op = (body.get("install") or body.get("update") or {})
        start = self.start_daemon("0.4.0 start（重新升級）")
        status = self.wait_daemon(version=NEW_VERSION) or {}
        version = self.cli_version()
        ok = stop.returncode == 0 and gone and proc.returncode == 0 and body.get("ok") is True \
            and start.returncode == 0 and status.get("version") == NEW_VERSION and version == f"serialwrap {NEW_VERSION}"
        if op.get("from_version") not in (None, OLD_VERSION):
            self.observations.append({"id": "OBS-UPGRADE-STALE-CURRENT", "phase": "reupgrade",
                                      "op": op.get("op"), "from_version": op.get("from_version"),
                                      "note": "手動降級後 current-release.json 仍記 0.4.0，重新升級被記為 0.4.0→0.4.0 update"})
        self.add(uc.row("reupgrade", "PASS" if ok else "FAIL", None if ok else "降級後重新升級失敗",
                        setup_rc=proc.returncode, op=op.get("op"), from_version=op.get("from_version"),
                        daemon_version=status.get("version"), cli_version=version))
        self.sw("daemon", "stop", label="收尾 daemon stop")
        self.wait_no_daemon()

    def execute(self) -> None:
        self.prepare_user_manager()
        subprocess.run(["chown", "-R", f"{USER}:{USER}", str(HOME)], check=True)
        monitor = threading.Thread(target=self._monitor, daemon=True)
        monitor.start()
        try:
            self.phase_old_install()
            self.phase_seed()
            already = self.phase_upgrade_running()
            self.phase_upgrade_after_stop(already)
            if self.mode == "systemd-system":
                self.phase_new_running_system()
                return
            self.phase_new_running()
            self.phase_rollback_command()
            self.phase_downgrade()
            self.phase_reupgrade()
        finally:
            self._stop.set()
            monitor.join(timeout=5)
            self.add(uc.judge_two_reader(self.samples, min_samples=50))


def _sha256(path: pathlib.Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("systemd-user", "on-demand", "systemd-system"))
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wheel = pathlib.Path(args.wheel)
    bundle = pathlib.Path(args.bundle)
    manifest_path = bundle / "versions" / NEW_VERSION / "release-manifest.json"
    manifest: dict[str, Any] = {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    driver = Driver(args.mode, wheel, bundle, out)
    error = None
    try:
        driver.execute()
    except Exception:  # noqa: BLE001 — harness 例外記為證據不足，不得吞成 PASS
        error = traceback.format_exc()
        driver.add(uc.row("driver_exception", "INSUFFICIENT", "容器內 driver 例外", traceback=error[-3000:]))
    report = {
        "schema": "serialwrap.upgrade-report.v1",
        "mode": args.mode,
        "evidence_layer": "L1.5-docker",
        "producer": "scripts/docker/run-upgrade.sh",
        "old_version": OLD_VERSION,
        "new_version": NEW_VERSION,
        "wheel": wheel.name,
        "wheel_sha256": _sha256(wheel),
        "bundle_manifest_sha256": _sha256(manifest_path),
        "bundle_version": manifest.get("bundle_version"),
        "bundle_git_revision": ((manifest.get("components") or {}).get("cli") or {}).get("git_revision"),
        "image": os.environ.get("SW_UPGRADE_IMAGE"),
        "image_id": os.environ.get("SW_UPGRADE_IMAGE_ID"),
        "started": started,
        "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": driver.rows,
        "observations": driver.observations,
    }
    required = uc.REQUIRED_CHECKS_SYSTEMD_SYSTEM if args.mode == "systemd-system" else uc.REQUIRED_CHECKS
    report["scope"] = "legacy_direct_setup_success_path" if args.mode == "systemd-system" else "full"
    report["verdict"] = uc.overall_verdict(driver.rows, required=required)
    (out / "upgrade-report.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                                             encoding="utf-8")
    uid, gid = os.environ.get("SW_HOST_UID"), os.environ.get("SW_HOST_GID")
    if uid and gid:
        for path in [out, *out.rglob("*")]:
            try:
                os.chown(path, int(uid), int(gid))
            except OSError:
                pass
    print(json.dumps({"mode": args.mode, "verdict": report["verdict"],
                      "failed": [r["id"] for r in driver.rows if r["verdict"] != "PASS"]}, ensure_ascii=False))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
