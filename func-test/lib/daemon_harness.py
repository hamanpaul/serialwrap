"""以隔離目錄管理一次 func-test daemon、DUT 與外部 bridge。"""
from __future__ import annotations

import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import yaml

from .target_config import TargetConfig

_PATH_CLASS = type(pathlib.Path("."))
ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
SERIALWRAP = str(ROOT_DIR / "serialwrap")
SERIALWRAPD = str(ROOT_DIR / "serialwrapd.py")
_REAL_POPEN = subprocess.Popen

# 關閉階段的上限（秒）。daemon 與 WAL 占用釋放在 Windows 上可能滯後於行程結束，故有界重試。
_DAEMON_EXIT_TIMEOUT_S = 3.0
_HANDLE_HOLDER_JOIN_TIMEOUT_S = 15.0
_TMPDIR_REMOVE_ATTEMPTS = 20
_TMPDIR_REMOVE_DELAY_S = 0.5

ISOLATED_DIR_ENV: dict[str, str] = {
    "SERIALWRAP_STATE_DIR": "state",
    "SERIALWRAP_RUN_DIR": "run",
    "SERIALWRAP_WAL_DIR": "wal",
    "SERIALWRAP_CONFIG_DIR": "config",
    "SERIALWRAP_LOG_DIR": "blog",
    "SERIALWRAP_EVENTS_DIR": "events.d",
    "SERIALWRAP_EVENTS_RUNTIME_DIR": "events-rt",
    "SERIALWRAP_BY_ID_DIR": "by-id",
    "SERIALWRAP_BY_PATH_DIR": "by-path",
}
ARTIFACT_ENV: tuple[str, ...] = ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE")


class StepError(RuntimeError):
    """func-test 動作失敗。"""


def _path(value: os.PathLike[str] | str) -> pathlib.Path:
    if isinstance(value, pathlib.PurePath):
        return value  # type: ignore[return-value]
    return _PATH_CLASS(os.fspath(value))  # type: ignore[call-arg,return-value]


def build_isolated_env(
    base: Mapping[str, str], root: pathlib.Path, *, extra: Mapping[str, str]
) -> dict[str, str]:
    """刪除外層所有產品環境變數，再只注入本次 harness 的私有目錄。"""
    env = {key: value for key, value in base.items() if not key.startswith("SERIALWRAP_")}
    for key, subdir in ISOLATED_DIR_ENV.items():
        path = _path(root) / subdir
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    xdg_config = _path(root) / "xdg-config"
    xdg_state = _path(root) / "xdg-state"
    xdg_config.mkdir(parents=True, exist_ok=True)
    xdg_state.mkdir(parents=True, exist_ok=True)
    env["XDG_CONFIG_HOME"] = str(xdg_config)
    env["XDG_STATE_HOME"] = str(xdg_state)
    env.pop("XDG_RUNTIME_DIR", None)
    env.update(extra)
    return env


def resolve_cli_exe() -> list[str]:
    """解析 CLI 身分對應的可執行檔路徑，不含 Windows Python interpreter。"""
    executable = os.environ.get("SERIALWRAP_CLI_EXE")
    if executable:
        return [executable]
    return [str(ROOT_DIR / "serialwrap")]


def resolve_cli_command() -> list[str]:
    """解析可直接交給 subprocess 執行的 CLI argv。"""
    argv = resolve_cli_exe()
    if os.environ.get("SERIALWRAP_CLI_EXE"):
        return argv
    if os.name == "nt":
        interpreter = os.environ.get("PYTHON") or sys.executable
        return [interpreter, *argv]
    return argv


def resolve_daemon_argv() -> list[str]:
    """解析 Python 或獨立 daemon launcher argv。"""
    executable = os.environ.get("SERIALWRAP_DAEMON_EXE")
    if executable:
        return [executable]
    interpreter = os.environ.get("PYTHON") or sys.executable
    return [interpreter, str(ROOT_DIR / "serialwrapd.py")]


def free_port() -> int:
    """取得目前可用的 loopback TCP port。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class HarnessConfig:
    """一次 func-test case 的 daemon 與 bridge 設定。"""

    target_config: Any = None
    profile_overrides: dict[str, Any] = field(default_factory=dict)
    com: str = "COM0"
    alias: str = "func-test"
    env_files: dict[str, str] = field(default_factory=dict)
    mode: str = "pty"
    dut_mode: str = "local-process"
    dut_plan: dict[str, Any] | None = None
    evidence_dir: pathlib.Path | None = None
    run_id: str = ""
    case_id: str = ""
    transport_mode: str = "virtual_pty"
    daemon_com: str | None = None
    bridge_com: str | None = None
    rpc_endpoint: str | None = None
    dut_image: str | None = None
    bridge_run_args: tuple[str, ...] = ()


class DaemonHarness:
    """建置隔離的 target/profile/daemon 環境並負責完整回收。"""

    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config or HarnessConfig()
        if self.config.target_config is None:
            self.config.target_config = TargetConfig()
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._root: pathlib.Path | None = None
        self._fake_target: Any | None = None
        self._daemon: subprocess.Popen[str] | Any | None = None
        self._daemon_log: Any | None = None
        self._env: dict[str, str] = {}
        self._endpoint = ""
        self._socket_path = ""
        self._evidence_dir: pathlib.Path | None = None
        self.bridge: Any | None = None
        self.bridge_argv: list[str] = []
        self.bridge_stop_result: dict[str, Any] | None = None
        # 測試案例自建、於 stop() 前必須結束的持有者執行緒（例如持有 WAL 讀取 handle 的情境）。
        self._handle_holders: list[threading.Thread] = []
        # 關閉階段無法完全回收、但不影響證據完整性的診斷（例如臨時目錄殘留）；由 runner 寫入該案報告。
        self.cleanup_diagnostics: list[dict[str, Any]] = []
        self._started = False

    @property
    def socket_path(self) -> str:
        """相容既有呼叫端的 RPC endpoint 名稱。"""
        return self._socket_path

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @endpoint.setter
    def endpoint(self, value: str) -> None:
        self._endpoint = value
        self._socket_path = value

    @property
    def env(self) -> dict[str, str]:
        return dict(self._env)

    @property
    def fake_target(self) -> Any | None:
        return self._fake_target

    @property
    def root(self) -> pathlib.Path:
        if self._root is None:
            raise RuntimeError("harness 尚未啟動")
        return self._root

    @property
    def evidence_dir(self) -> pathlib.Path:
        if self._evidence_dir is None:
            raise RuntimeError("harness 尚未啟動")
        return self._evidence_dir

    def get_cli_argv(self) -> list[str]:
        argv = resolve_cli_command()
        endpoint_flag = "--endpoint" if self.endpoint.startswith("tcp://") else "--socket"
        return [*argv, endpoint_flag, self.endpoint]

    def _query_daemon_status(self) -> dict[str, Any]:
        from .cli_client import cli_run

        return cli_run(["daemon", "status"], socket_path=self.endpoint, env=self.env, timeout=5.0)

    def _spawn_bridge(self, *_args: Any, **_kwargs: Any) -> None:
        if self.bridge is None:
            raise RuntimeError("bridge session 尚未建立")
        self.bridge.start()

    def start(self, before_daemon: Callable[[], None] | None = None) -> None:
        """建立 case 根目錄，啟動 target/bridge，再啟動隔離 daemon。

        ``before_daemon``（OD-39）：DUT／bridge 就緒、profile 寫好之後、daemon 行程啟動之前呼叫
        （情境 ``pre_daemon_steps``，例如 daemon 啟動前外部占用端點、自 daemon 啟動前起算零 TX 窗口）。
        """
        if self._started:
            return
        self.bridge_stop_result = None
        self.cleanup_diagnostics = []
        if self.config.mode not in {"pty", "bridge"}:
            raise ValueError(f"不支援的 harness mode：{self.config.mode}")
        if self.config.dut_mode not in {"local-process", "docker"}:
            raise ValueError(f"不支援的 DUT mode：{self.config.dut_mode}")
        if self.config.mode == "bridge" and self.config.dut_mode == "docker" and not (
            self.config.dut_image or (self.config.dut_plan or {}).get("image")
        ):
            raise ValueError("docker DUT 模式必須提供 --dut-image 或 dut_plan.image")

        self._tmpdir = tempfile.TemporaryDirectory(prefix="sw-func-")
        self._root = _path(self._tmpdir.name)
        self._evidence_dir = _path(self.config.evidence_dir) if self.config.evidence_dir else self.root / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._env = build_isolated_env(
            os.environ,
            self.root,
            extra={
                "PYTHONPATH": self._pythonpath(),
                **({"SERIALWRAP_TEST_DEVICE_ALLOWLIST": self.config.daemon_com or ""}
                   if os.name == "nt" and self.config.mode == "bridge" else {}),
            },
        )
        if not self._env.get("SERIALWRAP_TEST_DEVICE_ALLOWLIST"):
            self._env.pop("SERIALWRAP_TEST_DEVICE_ALLOWLIST", None)

        try:
            if os.name == "nt":
                if self.config.mode == "bridge" and (not self.config.daemon_com or not self.config.bridge_com):
                    raise ValueError("Windows bridge mode 必須提供 --daemon-com 與 --bridge-com")
                self.endpoint = self.config.rpc_endpoint or f"tcp://127.0.0.1:{free_port()}"
            else:
                self.endpoint = self.config.rpc_endpoint or str(self.root / "run" / "serialwrapd.sock")

            profile_dir = self.root / "config" / "profiles"
            profile_dir.mkdir(parents=True, exist_ok=True)
            if self.config.mode == "pty":
                self._start_fake_target()
                device_path = str(self.root / "by-id" / "fake-uart0")
            elif os.name == "nt":
                device_path = self.config.daemon_com or self.config.com
            else:
                device_path = str(self.root / "by-id" / "fake-uart0")
            profile_yaml = self._build_profile_yaml(device_path)
            (profile_dir / "func-test.yaml").write_text(profile_yaml, encoding="utf-8")
            for filename, content in self.config.env_files.items():
                target = profile_dir / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            if self.config.mode == "bridge":
                self._start_bridge()
            if before_daemon is not None:
                if self.bridge is not None:
                    self.bridge.daemon_started = False
                try:
                    before_daemon()
                finally:
                    if self.bridge is not None:
                        self.bridge.daemon_started = True

            daemon_argv = resolve_daemon_argv()
            argv = [
                *daemon_argv,
                "--profile-dir", str(profile_dir),
                "--socket", self.endpoint,
                "--lock", str(self.root / "run" / "serialwrapd.lock"),
            ]
            log_path = self.evidence_dir / "daemon.log"
            self._daemon_log = log_path.open("wb")
            self._daemon = subprocess.Popen(
                argv,
                env=self._env,
                stdout=self._daemon_log,
                stderr=self._daemon_log,
                close_fds=True,
            )
            self.wait_endpoint(timeout_s=5.0)
            if os.name == "nt" and self.config.mode == "bridge":
                status = self._query_daemon_status()
                allowlist = status.get("test_device_allowlist") or {}
                if allowlist.get("enabled") is not True or allowlist.get("coms") != [self.config.daemon_com]:
                    raise StepError("OD-11 隔離 daemon 未確認 test_device_allowlist.enabled/coms")
            self._started = True
        except Exception:
            self.stop()
            raise

    @staticmethod
    def _pythonpath() -> str:
        bridge_src = str(ROOT_DIR / "tools" / "uart_stdio_bridge" / "src")
        current = os.environ.get("PYTHONPATH", "")
        parts = [bridge_src, *[part for part in current.split(os.pathsep) if part]]
        return os.pathsep.join(dict.fromkeys(parts))

    def _start_fake_target(self) -> None:
        # PTY 專屬依賴只在此載入，runner 與 harness 可於 Windows import。
        from .fake_target import FakeTarget

        target = FakeTarget(self.config.target_config)
        target.start()
        link_path = self.root / "by-id" / "fake-uart0"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target.slave_path, link_path)
        # 父程序持有 slave 會觸發 BC-04 的外部 holder 防線；DUT 只需保留 master。
        try:
            os.close(target.slave_fd)
        except OSError:
            pass
        target.slave_fd = -1
        self._fake_target = target

    def _start_bridge(self) -> None:
        from .bridge_session import BridgeSession

        if os.name == "nt":
            endpoint_spec = {
                "kind": "win-serial",
                "com": self.config.bridge_com,
                "transport_mode": self.config.transport_mode,
            }
        else:
            endpoint_spec = {
                "kind": "posix-pty",
                "by_id_link": str(self.root / "by-id" / "fake-uart0"),
            }
        self.bridge = BridgeSession(
            run_id=self.config.run_id or uuid.uuid4().hex,
            case_id=self.config.case_id or "func-test",
            port_id="uart0",
            endpoint_spec=endpoint_spec,
            evidence_dir=self.evidence_dir,
            target_json=self.config.target_config.to_json(),
            plan_json=self.config.dut_plan,
            dut_mode=self.config.dut_mode,
            env=self.env,
            quiesce=bool(getattr(self.config.target_config, "noise_enabled", False)),
            extra_args=self.config.bridge_run_args,
            dut_image=self.config.dut_image,
        )
        self.bridge.rpc_endpoint = self.endpoint
        self.bridge.rpc_env = self.env
        self.bridge_argv = self.bridge.command()
        self._spawn_bridge(self.bridge_argv, env=self.env)

    def register_handle_holder(self, thread: threading.Thread) -> None:
        """登記持有 WAL 等檔案 handle 的案例執行緒；stop() 會等待其結束後才回收臨時目錄。"""
        self._handle_holders.append(thread)

    def wait_endpoint(self, timeout_s: float = 5.0) -> None:
        """等待 POSIX socket 出現或以 TCP connect 確認 Windows RPC listener。"""
        deadline = time.monotonic() + timeout_s
        if self.endpoint.startswith("tcp://"):
            address = self.endpoint.removeprefix("tcp://")
            host, _, port_text = address.rpartition(":")
            if not host or not port_text.isdigit():
                raise ValueError(f"無效 TCP endpoint：{self.endpoint}")
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection((host, int(port_text)), timeout=0.2):
                        return
                except OSError:
                    time.sleep(0.05)
            raise TimeoutError(f"Daemon endpoint 未在 {timeout_s}s 內就緒：{self.endpoint}")
        while time.monotonic() < deadline:
            if os.path.exists(self.endpoint):
                return
            if self._daemon is not None and self._daemon.poll() is not None:
                break
            time.sleep(0.05)
        raise TimeoutError(f"Daemon socket 未在 {timeout_s}s 內出現：{self.endpoint}")

    def target_emit(self, payload: bytes) -> None:
        if self.config.mode == "bridge":
            import base64

            if self.bridge is None:
                raise StepError("bridge session 尚未啟動")
            self.bridge.call("dut_control", {"msg": {"action": "emit", "payload_b64": base64.b64encode(payload).decode("ascii")}})
            return
        if self.fake_target is None:
            raise StepError("pty mode 沒有 fake target")
        os.write(self.fake_target.master_fd, payload)

    def pause_target(self, duration_s: float) -> None:
        if self.config.mode == "bridge":
            if self.bridge is None:
                raise StepError("bridge session 尚未啟動")
            self.bridge.call("dut_control", {"msg": {"action": "pause_responding", "duration_ms": int(duration_s * 1000)}})
        elif self.fake_target is not None:
            self.fake_target.pause_responding(duration_s)

    def device_event(self, event: str) -> None:
        if event not in {"remove", "add"}:
            raise StepError(f"未知 device event：{event}")
        if self.config.mode == "bridge":
            if os.name == "nt":
                raise StepError("cap_missing:by_id_link")
            if self.bridge is None:
                raise StepError("bridge session 尚未啟動")
            action = "by_id_link_remove" if event == "remove" else "by_id_link_add"
            self.bridge.call(action)
            return
        link_path = self.root / "by-id" / "fake-uart0"
        if event == "remove":
            try:
                link_path.unlink()
            except FileNotFoundError:
                pass
        elif self.fake_target is not None and not link_path.exists():
            os.symlink(self.fake_target.slave_path, link_path)

    def wal_dir(self) -> pathlib.Path:
        return self.root / "wal"

    def _build_profile_yaml(self, device_path: str) -> str:
        overrides = dict(self.config.profile_overrides)
        target = self.config.target_config
        profile: dict[str, Any] = {
            "platform": target.platform,
            "prompt_regex": overrides.get("prompt_regex", '(?m)^root@prplOS:.*# '),
            "ready_probe": overrides.get("ready_probe", "echo __READY__${nonce}"),
            "timeout_s": overrides.get("timeout_s", 10),
            "uart": {
                "baud": 115200,
                "data_bits": 8,
                "parity": "N",
                "stop_bits": 1,
                "flow_control": "rtscts",
                "xonxoff": False,
            },
        }
        for key in ("login_regex", "password_regex", "user_env", "pass_env", "env_file", "post_login_cmd"):
            if overrides.get(key):
                profile[key] = overrides[key]
        if overrides.get("login_regex"):
            profile.setdefault("password_regex", "(?i)password")
            profile.setdefault("user_env", "USER")
            profile.setdefault("pass_env", "PASS")
        for key, value in overrides.items():
            if key.startswith("uart."):
                profile["uart"][key.partition(".")[2]] = value
        target_row: dict[str, Any] = {
            "act_no": 1,
            "com": self.config.com,
            "alias": self.config.alias,
            "profile": "func-test-template",
            "device_by_id": device_path,
        }
        document = {
            "profiles": {"func-test-template": profile},
            "targets": [target_row],
        }
        return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)

    def stop(self) -> None:
        """先停 daemon/bridge、保存 WAL 複本，再回收所有 throwaway 資源。

        失敗分類：daemon 行程未確認結束、bridge 關閉或 WAL 複本失敗會以例外拋出（證據完整性不明，由 runner 判為
        INSUFFICIENT_EVIDENCE）。等待案例持有者執行緒與臨時目錄回收為有界重試；回收仍失敗者只寫入
        cleanup_diagnostics，不改變案例判定。
        """
        try:
            try:
                self._stop_daemon()
            finally:
                self._close_daemon_log()
                self._stop_bridge_and_target()
            self._preserve_wal_and_unlink_link()
        finally:
            self._join_handle_holders()
            self._remove_tmpdir()
            self._root = None
            self._started = False

    def _stop_daemon(self) -> None:
        """停止隔離 daemon；以 Popen 的 wait 確認行程結束（不以 pid 數字判斷，避免 pid 重用）。"""
        daemon = self._daemon
        if daemon is None:
            return
        try:
            if isinstance(daemon, _REAL_POPEN) and daemon.poll() is None and self.endpoint:
                try:
                    from .cli_client import cli_run

                    cli_run(
                        ["daemon", "stop"],
                        socket_path=self.endpoint,
                        env=self.env,
                        timeout=3.0,
                        log_path=(self.evidence_dir / "cli.ndjson") if self._evidence_dir else None,
                    )
                except Exception:
                    pass
            if daemon.poll() is None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=_DAEMON_EXIT_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    try:
                        daemon.wait(timeout=_DAEMON_EXIT_TIMEOUT_S)
                    except subprocess.TimeoutExpired as exc:
                        raise StepError(
                            f"daemon pid={daemon.pid} 於 kill 後仍未確認結束（WAL 可能仍被占用，證據不完整）"
                        ) from exc
        finally:
            self._daemon = None

    def _close_daemon_log(self) -> None:
        if self._daemon_log is not None:
            try:
                self._daemon_log.flush()
                self._daemon_log.close()
            except OSError:
                pass
            self._daemon_log = None

    def _stop_bridge_and_target(self) -> None:
        if self.bridge is not None:
            try:
                result = self.bridge.stop()
                if isinstance(result, dict):
                    self.bridge_stop_result = dict(result)
            finally:
                self.bridge = None
        if self.fake_target is not None:
            self.fake_target.stop()
            self._fake_target = None

    def _preserve_wal_and_unlink_link(self) -> None:
        if self._root is not None and self._evidence_dir is not None:
            source = self.root / "wal"
            if source.is_dir():
                destination = self.evidence_dir / "wal"
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                (self.evidence_dir / "wal").mkdir(parents=True, exist_ok=True)
            link_path = self.root / "by-id" / "fake-uart0"
            try:
                link_path.unlink()
            except FileNotFoundError:
                pass

    def _join_handle_holders(self) -> None:
        """等待案例自建的 handle 持有者結束（其 sleep 結束後即釋放檔案），有上限。"""
        holders, self._handle_holders = self._handle_holders, []
        for thread in holders:
            thread.join(timeout=_HANDLE_HOLDER_JOIN_TIMEOUT_S)
            if thread.is_alive():
                self.cleanup_diagnostics.append({
                    "kind": "handle_holder_alive",
                    "thread": thread.name,
                    "join_timeout_s": _HANDLE_HOLDER_JOIN_TIMEOUT_S,
                })

    def _remove_tmpdir(self) -> None:
        """回收臨時目錄；TemporaryDirectory.cleanup 失敗後可再次呼叫（finalizer 已分離、路徑仍存在時會重試）。"""
        tmpdir = self._tmpdir
        if tmpdir is None:
            return
        root = tmpdir.name
        last_error: OSError | None = None
        for _attempt in range(_TMPDIR_REMOVE_ATTEMPTS):
            try:
                tmpdir.cleanup()
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                time.sleep(_TMPDIR_REMOVE_DELAY_S)
        if last_error is not None:
            self.cleanup_diagnostics.append({
                "kind": "tmpdir_remove_failed",
                "path": root,
                "attempts": _TMPDIR_REMOVE_ATTEMPTS,
                "error": f"{type(last_error).__name__}: {last_error}",
            })
        self._tmpdir = None

__all__ = [
    "ARTIFACT_ENV", "ISOLATED_DIR_ENV", "DaemonHarness", "HarnessConfig", "StepError",
    "build_isolated_env", "free_port", "resolve_cli_exe", "resolve_cli_command", "resolve_daemon_argv",
]
