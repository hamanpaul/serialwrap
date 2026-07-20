from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any

from .arbiter import CMD_REJECT_BYTES, CMD_WARN_BYTES
from .client import _af_unix_available, _parse_endpoint, rpc_call
from .constants import (
    CONFIG_DIR,
    DEFAULT_ENDPOINT,
    DEFAULT_TCP_PORT,
    LOCK_PATH,
    LOOPBACK_TCP_HOSTS,
    PROFILE_DIR,
    SOCKET_PATH,
)
from .doctor_cmd import run_doctor
from .platform_backends import select_rpc_backend
from .runtime_config import RuntimeConfig
from .service_ctl import service_action
from .sysenv import force_utf8_stdio
from .setup_cmd import (
    SYSTEM_SOCKET,
    FlashingBusy,
    detect_legacy_install,
    ensure_wsl_systemd,
    materialize_assets,
    reconcile,
)

_USE_DEFAULT_ENV = object()
LEGACY_DAEMON_ENV_FILE = "~/OPI.env"
PROFILE_DAEMON_ENV_FILE = "OPI.env"

# --- #123：長操作 RPC 的 socket timeout floor -------------------------------
# 問題：session recover／attach／self-test／console-attach（recover 升級分支）
# 的 daemon 端 handler 為同步長操作（對照 sw_core/daemon.py 的
# BLOCKING_RPC_METHODS），host 過載或走 force recover 路徑時，daemon 端執行
# 時間會結構性超過 CLI 舊預設 5s socket timeout，CLI 因而回「假性 TIMEOUT」
# ——daemon 其實健康、操作稍後成功（實測 recover CLI 5.08s 報 TIMEOUT、daemon
# ~7-8s 後 READY）。
#
# floor 推導（code review MINOR-2 修正版，誠實版）：真正會拉長 daemon 端執行
# 時間的變數是 profile 的 ``timeout_s``（sw_core/config.py；bcm 類平台常設
# 15s+、且可能多階段 login/ready probe），這個值只存在 daemon 端載入的
# profile 裡，CLI 完全無從得知。初版曾誤以為「CLI 傳給 daemon 的
# recover_timeout_s／probe_timeout_s 越大、daemon 端就等越久」，因而讓 floor
# 隨這兩個參數 + 常數裕度「縮放」；但 sw_core/session_manager.py 的
# ``_recover_after_failure`` 對 CTRL_C／CTRL_D 兩段等待皆套用
# ``min(timeout_s, 2.0)`` 硬 cap，超過 2.0 的 recover_timeout_s 對 daemon 端
# 實際等待時間毫無影響——前提是錯的，floor 不該假裝能靠這個參數精算。
#
# 因此改採固定寬鬆常數：CTRL_C 等 prompt 2s + CTRL_D 等 prompt 2s
# （_recover_after_failure）+ force fallback 硬輪詢 10s（_force_recover 的
# range(10)×sleep(1.0)）+ bcm 類慢板多階段 login/ready probe 與 attach
# reprobe 裕度，取 45s 為 recover／attach／self-test／console-attach 統一
# floor；顯式 --timeout 一律照舊覆蓋。若操作仍逾時，應以 TIMEOUT 錯誤附帶的
# daemon_reachable／daemon_busy（見 client.py 的 _probe_daemon_after_timeout）
# 與 ``session list`` 確認 daemon 是否仍在執行，而非把這個 floor 當成精確上界。
LONG_RPC_TIMEOUT_FLOOR_S = 45.0
# 一般（非長操作）方法未顯式指定 --timeout 時的預設 RPC timeout，維持既有 5s。
DEFAULT_RPC_TIMEOUT_S = 5.0

# 落在 daemon 端 BLOCKING_RPC_METHODS、且 CLI 無從得知其真實變動成本
# （profile timeout_s）的長操作方法：一律採上方固定 floor，不依任何 CLI 側
# 參數縮放（MINOR-2）。
_LONG_RPC_METHODS = frozenset({
    "session.recover",
    "session.attach",
    "session.self_test",
    "session.console_attach",  # recover 升級分支可同步跑數十秒，MAJOR-1 補上
})

# file.push／file.pull 已知缺口（#123 defer，MINOR-5）：兩者的 chunk 傳輸走
# UART 逐段 base64 編碼往返，大檔可達分鐘級，遠超一般方法的 5s 預設、幾乎必
# 假性逾時；但目前 CLI 端沒有任何可靠信號能推得「這次要傳多久」（檔案大小、
# baud、對端忙碌程度皆未知），貿然給個固定常數一樣是用猜的。留待 follow-up
# （例如讓 daemon 端回報預期分段數，或改走非阻塞輪詢）另行處理，此處不假裝
# 已經解決。


class EnvFileSourceError(RuntimeError):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(message)
        self.path = path


def _print(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")


def _daemon_script_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "serialwrapd.py"))


def _repo_version_path() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "VERSION"))


def _resolve_version() -> str:
    """解析 serialwrap 版本字串（#131 補強：CLI 原本沒有 --version）。

    順序：repo checkout 的 VERSION（原始碼執行最真實）→ 已安裝套件 metadata
    （pip/pipx）→ PyInstaller 內嵌 assets/VERSION（release exe，serialwrap.spec
    datas 於打包時帶入）→ "unknown"。
    """
    try:
        with open(_repo_version_path(), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        pass
    try:
        import importlib.metadata  # noqa: PLC0415

        return importlib.metadata.version("serialwrap")
    except Exception:  # noqa: BLE001
        pass
    try:
        from .assets import read_text  # noqa: PLC0415

        return read_text("VERSION").strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _is_loopback_tcp(ep: str) -> bool:
    """endpoint 是否為 loopback tcp://（#131：Windows `daemon start` 的本機 bind 白名單）。"""
    try:
        transport, address = _parse_endpoint(ep)
    except ValueError:
        return False
    return transport == "tcp" and address[0] in LOOPBACK_TCP_HOSTS


def _daemon_spawn_argv() -> list[str] | None:
    """解析 spawn serialwrapd 的 argv（#131，Windows 路徑）。

    凍結（PyInstaller onefile）→ ``sys.executable`` 同層 serialwrapd.exe →
    PATH 上的 serialwrapd，全落空回 None；原始碼 checkout → serialwrapd.py；
    安裝套件（無 repo script）→ ``-m sw_core.daemon``。
    """
    if getattr(sys, "frozen", False):
        sibling = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "serialwrapd.exe")
        if os.path.isfile(sibling):
            return [sibling]
        found = shutil.which("serialwrapd")
        return [found] if found else None
    script = _daemon_script_path()
    if os.path.isfile(script):
        return [sys.executable, script]
    return [sys.executable, "-m", "sw_core.daemon"]


def _decode_env_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def _configured_daemon_env_file() -> str | None:
    raw = os.environ.get("SERIALWRAP_DAEMON_ENV_FILE")
    if raw is None:
        return LEGACY_DAEMON_ENV_FILE
    value = raw.strip()
    return value or None


def _parse_env_file_simple(path: str) -> dict[str, str]:
    """最小 env 檔解析（#131，Windows 用）。

    支援 ``KEY=VALUE``、``export KEY=VALUE``、``#`` 註解與空行、單/雙引號包覆；
    不做變數展開與 shell 語意。Windows 上不得經 bash source——Git Bash 的
    ``env -0`` 會把整個環境 MSYS 化（PATH 轉成 ``/c/...`` 冒號分隔），
    spawn 出的 serialwrapd 會拿到 Windows 無法使用的環境（#131 review）。
    """
    out: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not key:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            out[key] = value
    return out


def _load_daemon_start_env_files(env_files: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    env = dict(os.environ)
    loaded_paths: list[str] = []
    seen_paths: set[str] = set()
    use_simple_parser = _rpc_backend_is_win()  # #131：Windows 不經 bash（見 _parse_env_file_simple）
    for env_file in env_files:
        path = os.path.expanduser(str(env_file).strip())
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        if not os.path.isfile(path):
            continue

        if use_simple_parser:
            try:
                env.update(_parse_env_file_simple(path))
            except OSError as exc:
                raise EnvFileSourceError(path, f"無法讀取 env 檔：{exc}") from exc
            loaded_paths.append(path)
            continue

        proc = subprocess.run(
            ["bash", "-lc", 'set -a && source "$1" >/dev/null && env -0', "serialwrap", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            stderr = _decode_env_text(proc.stderr).strip()
            raise EnvFileSourceError(path, stderr or f"failed to source {path}")

        loaded_env: dict[str, str] = {}
        for row in proc.stdout.split(b"\0"):
            if not row:
                continue
            key, sep, value = row.partition(b"=")
            if not sep:
                continue
            loaded_env[_decode_env_text(key)] = _decode_env_text(value)
        env = loaded_env
        loaded_paths.append(path)
    return env, loaded_paths


def _load_daemon_start_env(env_file: str | None | object = _USE_DEFAULT_ENV) -> tuple[dict[str, str], str | None]:
    if env_file is _USE_DEFAULT_ENV:
        env_file = _configured_daemon_env_file()
    if env_file is None:
        return dict(os.environ), None
    env, loaded = _load_daemon_start_env_files([str(env_file)])
    return env, loaded[0] if loaded else None


def _default_runtime_config() -> RuntimeConfig:
    return RuntimeConfig(os.path.join(CONFIG_DIR, "config.yaml"))


def should_auto_spawn(rc: RuntimeConfig | None = None) -> bool:
    """systemd 監管模式下不得自動 spawn（避免與 unit 互搶）；on-demand/未設→可。"""
    rc = rc if rc is not None else _default_runtime_config()
    return (rc.mode() or "on-demand") == "on-demand"


def _safe_runtime_config() -> RuntimeConfig | None:
    """讀取 config.yaml；損壞/不可讀（如壞 YAML）時回 None，避免 CLI traceback（#108）。"""
    try:
        return _default_runtime_config()
    except Exception:  # noqa: BLE001
        return None


def _resolve_daemon_start_env_files(profile_dir: str) -> list[str]:
    """解析 daemon 啟動時要載入的 runtime env 檔。

    帳密不再在此階段載入（改為 per-session 解析），
    這裡只處理 runtime 設定（如 SERIALWRAP_WAL_DIR）。
    """
    explicit_env_file = os.environ.get("SERIALWRAP_DAEMON_ENV_FILE")
    if explicit_env_file is not None:
        value = explicit_env_file.strip()
        return [value] if value else []

    env_files: list[str] = []
    fallback = _configured_daemon_env_file()
    if fallback is not None:
        env_files.append(fallback)
    profile_env = os.path.join(profile_dir, PROFILE_DAEMON_ENV_FILE)
    if profile_env not in env_files:
        env_files.append(profile_env)
    return env_files


def _probe_healthy_daemon(endpoint: str) -> bool:
    """對 endpoint 做 health.ping 存活探測；連得上且 ok 視為已有健康 daemon。"""
    try:
        resp = rpc_call(endpoint, "health.ping", {}, timeout_s=0.5)
    except Exception:  # noqa: BLE001
        return False
    return bool(resp.get("ok"))


def _run_daemon_start(args: argparse.Namespace) -> int:
    is_win = _rpc_backend_is_win()  # backend 不會於呼叫中途改變，一次判定全函式共用
    ep_arg = getattr(args, "endpoint", None)
    if ep_arg and not (is_win and _is_loopback_tcp(ep_arg)):
        # Windows（win backend）開放 loopback tcp:// 作為本機 bind 位址（#131）；
        # 其餘（POSIX 一律、win 非 loopback）照舊拒絕，POSIX 訊息逐字保留。
        if is_win:
            message = "--endpoint 於 daemon start 僅接受 loopback tcp://（127.0.0.1/localhost/::1）作為本機 bind 位址"
        else:
            message = "--endpoint 不支援 daemon start（daemon 只能在本機啟動）"
        _print({"ok": False, "error_code": "REMOTE_NOT_SUPPORTED", "message": message})
        return 2
    if ep_arg and args.socket is not None and args.socket != ep_arg:
        # 同給 --endpoint 與 --socket 且不一致：冪等探測（走 --endpoint）與 spawn bind
        # （走 --socket）會指向不同位址，寧可顯式拒絕也不留下歧義（#131 review）。
        _print({
            "ok": False,
            "error_code": "INVALID_ARGS",
            "message": "--endpoint 與 --socket 不一致；daemon start 請擇一指定 bind 位址",
        })
        return 2
    # 監管模式 gate（#108 #1）：systemd 模式下重導到 `service start`，避免顯式 daemon
    # start 繞過 unit 管理另起非託管 daemon（對稱於 `daemon stop` → `service stop`）。
    # config 不可讀（rc is None）時退化為 on-demand spawn 路徑，不 traceback。
    rc = _safe_runtime_config()
    if rc is not None and not should_auto_spawn(rc):  # systemd-user / systemd-system
        mode = rc.mode() or "on-demand"
        resp = service_action("start", mode=mode, with_sudo=getattr(args, "with_sudo", False))
        resp["_routed_to"] = "service start"
        _print(resp)
        return 0 if resp.get("ok") else 2
    # on-demand：spawn 前先對「使用者實際會連到的 endpoint」冪等探測，已有健康 daemon 則
    # no-op（#108 #1）。用 _resolve_endpoint 而非裸 args.socket，避免 config 記錄的 daemon
    # 在非預設 socket 時 probe miss 又 spawn 出第二個（two-reader）。
    endpoint = _resolve_endpoint(args)
    if _probe_healthy_daemon(endpoint):
        _print({"ok": True, "already_running": True, "socket": endpoint})
        return 0
    # --socket 為 None sentinel（#120 向量 2）：spawn 路徑落到平台預設（#131：
    # win backend → tcp DEFAULT_ENDPOINT，POSIX → SOCKET_PATH 不變）；
    # Windows 放行的 --endpoint 視為本機 bind 位址。
    if args.socket is not None:
        sock = args.socket
    elif ep_arg:
        sock = ep_arg
    else:
        sock = _local_default_endpoint()
    if is_win:
        daemon_argv = _daemon_spawn_argv()
        if daemon_argv is None:
            _print({
                "ok": False,
                "error_code": "DAEMON_BINARY_NOT_FOUND",
                "message": "找不到 serialwrapd（serialwrap.exe 同層或 PATH 上皆無）；請確認發行包完整",
            })
            return 2
    else:
        # POSIX：argv 組法逐字保留既有行為。
        daemon_argv = [sys.executable, _daemon_script_path()]
    cmd = [
        *daemon_argv,
        "--profile-dir",
        args.profile_dir,
        "--socket",
        sock,
        "--lock",
        args.lock,
    ]
    env_files = _resolve_daemon_start_env_files(args.profile_dir)
    try:
        daemon_env, loaded_env_files = _load_daemon_start_env_files(env_files)
    except EnvFileSourceError as exc:
        payload: dict[str, Any] = {
            "ok": False,
            "error_code": "ENV_FILE_SOURCE_FAILED",
            "env_file": exc.path,
            "message": str(exc),
        }
        if env_files:
            payload["env_files"] = [os.path.expanduser(path) for path in env_files]
        _print(payload)
        return 2

    if args.foreground:
        return subprocess.call(cmd, env=daemon_env)

    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": daemon_env,
    }
    if os.name == "nt":
        # DETACHED_PROCESS：不掛父 console（關閉視窗不殺 daemon）；
        # CREATE_NEW_PROCESS_GROUP：隔離父 shell 的 Ctrl+C（#131）。
        # 以 os.name 而非 rpc backend 判斷：Linux 上模擬 win backend 時
        # creationflags 會使 Popen ValueError。
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)

    # 等待 daemon 就緒（POSIX 最多 3 秒；win backend 放寬至 10 秒——
    # PyInstaller onefile 冷啟需先解壓，#131）
    attempts = 50 if is_win else 15
    for attempt in range(attempts):
        time.sleep(0.2)
        if proc.poll() is not None:
            _print({"ok": False, "error_code": "DAEMON_EXITED", "pid": proc.pid, "returncode": proc.returncode})
            return 2
        resp = rpc_call(sock, "health.ping", {}, timeout_s=0.5)
        if resp.get("ok"):
            result: dict[str, Any] = {"ok": True, "pid": proc.pid, "socket": sock}
            if loaded_env_files:
                result["env_files"] = loaded_env_files
            health = rpc_call(sock, "health.status", {}, timeout_s=1.0)
            warnings = health.get("warnings")
            if warnings:
                result["warnings"] = warnings
            _print(result)
            return 0

    _print({"ok": False, "error_code": "DAEMON_NOT_READY", "pid": proc.pid})
    return 2


def _run_daemon_stop(args: argparse.Namespace) -> int:
    # 用 _safe_runtime_config 避免 config.yaml 壞 YAML 時 traceback；讀不到退化 on-demand
    # 路徑（與 daemon start / _resolve_endpoint 的容錯一致，#108 PR #112 review）。
    rc = _safe_runtime_config()
    mode = (rc.mode() if rc is not None else None) or "on-demand"
    if mode.startswith("systemd"):
        # systemd 模式：將 daemon stop 重導到 service stop，避免繞開 unit 管理
        with_sudo = getattr(args, "with_sudo", False)
        resp = service_action("stop", mode=mode, with_sudo=with_sudo)
        resp["_routed_to"] = "service stop"
        _print(resp)
        return 0 if resp.get("ok") else 2
    # on-demand 模式：維持原有 RPC daemon.stop 路徑
    resp = rpc_call(_resolve_endpoint(args), "daemon.stop", {}, timeout_s=2.0)
    if not resp.get("ok"):
        _print(resp)
        return 2
    _print(resp)
    return 0


def _rpc_backend_is_win() -> bool:
    """CLI 端的 rpc backend 判準（#131）。

    包一層 ``select_rpc_backend()``：``SERIALWRAP_RPC_BACKEND`` 為無法識別的值時
    不讓 ValueError 穿出 CLI（維持機器可解析 JSON 契約），退回實際平台判斷；
    daemon 端仍以 select_rpc_backend 嚴格驗證。
    """
    try:
        return select_rpc_backend() == "win"
    except ValueError:
        return os.name == "nt" or sys.platform.startswith("win")


def _local_default_endpoint() -> str:
    """本機預設 endpoint（#131）。

    win backend（native Windows，或 ``SERIALWRAP_RPC_BACKEND=win``）→ tcp
    loopback（與 daemon 的 ``--socket`` 預設一致）：native Windows 直用平台感知
    ``DEFAULT_ENDPOINT``；POSIX 上以 env 模擬 win backend 時 ``DEFAULT_ENDPOINT``
    仍是 unix 路徑，改組 tcp 預設（尊重 ``SERIALWRAP_TCP_PORT``），避免把 unix
    路徑餵給 win 後端的 TcpRpcServer。其餘 → ``SOCKET_PATH``（POSIX 既有行為
    逐位元組不變）。
    """
    if _rpc_backend_is_win():
        if DEFAULT_ENDPOINT.startswith("tcp://"):
            return DEFAULT_ENDPOINT
        return f"tcp://127.0.0.1:{DEFAULT_TCP_PORT}"
    return SOCKET_PATH


def _endpoint_alive(ep: str) -> bool:
    """對 unix socket endpoint 做 0.2s connect 探測（#108 #2）。

    以 ``client._parse_endpoint`` 判斷 transport（涵蓋裸路徑與 ``unix:///path``）：
    非 unix endpoint（``tcp://...``）或無法解析者一律視為可連、跳過探測，
    使 dangling fallback 僅作用於 POSIX unix socket。
    """
    if not ep:
        return True
    try:
        transport, address = _parse_endpoint(ep)
    except ValueError:
        return True
    if transport != "unix":
        # POSIX：tcp endpoint 一律視為可連、跳過探測（#108 原語意，可能是 ssh tunnel）。
        # win backend（#131）：tcp 是本機 canonical 的常態，殘留死 port 的 config 若不
        # 實測就永遠不會觸發 dangling fallback，改用 lock_win 的 0.2s connect 探測。
        if transport == "tcp" and _rpc_backend_is_win():
            from .lock_win import _endpoint_alive as _tcp_alive  # 純 socket，msvcrt 為 lazy

            return _tcp_alive(ep)
        return True
    if not _af_unix_available():
        # native Windows：無 AF_UNIX，unix endpoint 必然不可連（#131），
        # 回 False 讓 dangling fallback（#108）得以改連 tcp canonical。
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        sock.connect(address)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _resolve_endpoint(args: argparse.Namespace) -> str:
    """回傳實際連接 endpoint。

    優先序：``--endpoint`` > 明確傳入的 ``--socket`` > config.yaml 記錄的有效 socket
    > 預設 ``SOCKET_PATH``。讀 config 是為了讓 systemd-system 裝完後 CLI 連到系統 daemon 的
    socket（``/run/serialwrap/...``）而非使用者 XDG socket（Codex #1a）。

    dangling fallback（#108 #2）：當選用的 config socket 為不可連的 unix socket 時，
    依 ``supervision_mode`` 推 canonical endpoint（``systemd-system`` → ``SYSTEM_SOCKET``、
    其餘 → ``SOCKET_PATH``）並改連之；canonical 不可連或同值則回原值讓既有錯誤照常浮現。
    CLI 對 config.yaml 維持唯讀，不 self-heal 改寫。明確 ``--endpoint``/``--socket`` 不受影響。
    """
    ep = getattr(args, "endpoint", None)
    if ep:
        return ep
    if args.socket is not None:
        # 有傳即明確（#120 向量 2）：不得與 import-time 預設值比對——測試以 env 覆寫 RUN_DIR 時
        # 傳入值恰等於預設 SOCKET_PATH，等值比對會誤判為「未指定」而 fallback 到 live config。
        return args.socket
    rc = _safe_runtime_config()
    cfg_sock = None
    if rc is not None:
        try:
            cfg_sock = rc.socket_path()
        except Exception:
            cfg_sock = None
    chosen = cfg_sock or _local_default_endpoint()
    if cfg_sock and chosen == cfg_sock and not _endpoint_alive(cfg_sock):
        try:
            mode = (rc.mode() if rc is not None else None) or "on-demand"
        except Exception:
            mode = "on-demand"
        canonical = SYSTEM_SOCKET if mode == "systemd-system" else _local_default_endpoint()
        if canonical != cfg_sock and _endpoint_alive(canonical):
            sys.stderr.write(
                f"serialwrap: config.yaml socket_path '{cfg_sock}' 不可連，"
                f"依 supervision_mode={mode} 改用 '{canonical}'；"
                f"如需修正請更新 config.yaml 或重跑 serialwrap setup。\n"
            )
            return canonical
    return chosen


def _effective_timeout_s(args: argparse.Namespace, method: str) -> float:
    """解析本次 RPC 實際使用的 socket timeout（#123）。

    使用者顯式指定全域 ``--timeout`` → 一律照用；未指定（None）→ 一般方法用
    ``DEFAULT_RPC_TIMEOUT_S``（5s），落在 ``_LONG_RPC_METHODS``（對照 daemon
    端 ``BLOCKING_RPC_METHODS`` 的 session.recover／session.attach／
    session.self_test／session.console_attach）改用固定
    ``LONG_RPC_TIMEOUT_FLOOR_S``，消除「daemon 還在跑、CLI 先報 TIMEOUT」的
    假性逾時。floor 為固定常數、不隨 recover_timeout_s／probe_timeout_s 縮放
    （MINOR-2：daemon 端對這兩個 CLI 參數皆有 cap，且真正影響執行時間的是
    CLI 無從得知的 profile timeout_s）。file.push／file.pull 為已知缺口，
    暫不納入（見上方常數註解，#123 defer）。
    """
    explicit = getattr(args, "timeout_s", None)
    if explicit is not None:
        return float(explicit)
    if method in _LONG_RPC_METHODS:
        return LONG_RPC_TIMEOUT_FLOOR_S
    return DEFAULT_RPC_TIMEOUT_S


def _run_rpc(args: argparse.Namespace, method: str, params: dict[str, Any]) -> int:
    resp = rpc_call(
        _resolve_endpoint(args),
        method,
        params,
        timeout_s=_effective_timeout_s(args, method),
        retries=int(getattr(args, "retries", 0) or 0),
    )
    _print(resp)
    if not resp.get("ok"):
        # #94：失敗時除了 stdout 的機器可解析 JSON，另在 stderr 印一行具體 error，
        # 讓依 Unix 慣例讀 stderr 解釋非零 exit 的 consumer 不再拿到空字串。
        err = resp.get("error_code") or resp.get("message") or "UNKNOWN_ERROR"
        sys.stderr.write(f"serialwrap: {method} failed: {err}\n")
    return 0 if resp.get("ok") else 2


# event 子命令 → 實際送出的 RPC method 名對映（NIT-8：先前用佔位字串
# f"event.{event_cmd}" 查 floor，與 rpc_call 實際送出的 method 名不一致，
# 例如 "add" 實際送 "event.rule_set" 而非 "event.add"）。目前結果不變
# （event.* 全不在 _LONG_RPC_METHODS／RETRYABLE_READONLY_METHODS 白名單，
# 一律維持 DEFAULT_RPC_TIMEOUT_S、不重試），但用真實 method 名查詢才不會在
# 白名單將來納入 event.* 某個方法時悄悄查錯 key。
_EVENT_CMD_METHOD = {
    "add": "event.rule_set",
    "rm": "event.rule_delete",
    "list": "event.rule_list",
    "show": "event.rule_get",
    "enable": "event.com_enable",
    "disable": "event.com_disable",
    "status": "event.com_status",
    "reset": "event.reset",
    "reload": "event.reload",
    "tail": "event.tail",
}


def _dispatch_event(args: argparse.Namespace) -> int:
    # event.* 皆非長操作、也都不在 RETRYABLE_READONLY_METHODS 白名單（即使是
    # event.rule_list／event.tail 這類查詢，目前也未列入，見 client.py）：
    # 未顯式指定 --timeout 時維持一般預設 5s；--retries 於此不轉發——轉發了
    # 也不會生效，維持現狀（#123）。
    timeout_s = _effective_timeout_s(args, _EVENT_CMD_METHOD.get(args.event_cmd, f"event.{args.event_cmd}"))
    if args.event_cmd == "add":
        with open(args.file, "r", encoding="utf-8") as f:
            params = json.load(f)
        result = rpc_call(_resolve_endpoint(args), "event.rule_set", params, timeout_s=timeout_s)
    elif args.event_cmd == "rm":
        result = rpc_call(_resolve_endpoint(args), "event.rule_delete", {"rule_id": args.rule_id}, timeout_s=timeout_s)
    elif args.event_cmd == "list":
        result = rpc_call(
            _resolve_endpoint(args),
            "event.rule_list",
            {"selector": getattr(args, "selector", None), "owner": getattr(args, "owner", None)},
            timeout_s=timeout_s,
        )
    elif args.event_cmd == "show":
        result = rpc_call(_resolve_endpoint(args), "event.rule_get", {"rule_id": args.rule_id}, timeout_s=timeout_s)
    elif args.event_cmd == "enable":
        result = rpc_call(_resolve_endpoint(args), "event.com_enable", {"selector": args.selector}, timeout_s=timeout_s)
    elif args.event_cmd == "disable":
        result = rpc_call(_resolve_endpoint(args), "event.com_disable", {"selector": args.selector}, timeout_s=timeout_s)
    elif args.event_cmd == "status":
        result = rpc_call(_resolve_endpoint(args), "event.com_status", {"selector": getattr(args, "selector", None)}, timeout_s=timeout_s)
    elif args.event_cmd == "reset":
        result = rpc_call(
            _resolve_endpoint(args),
            "event.reset",
            {"rule_id": getattr(args, "rule_id", None), "selector": getattr(args, "selector", None)},
            timeout_s=timeout_s,
        )
    elif args.event_cmd == "reload":
        result = rpc_call(_resolve_endpoint(args), "event.reload", {}, timeout_s=timeout_s)
    elif args.event_cmd == "tail":
        result = rpc_call(
            _resolve_endpoint(args),
            "event.tail",
            {
                "rule_id": getattr(args, "rule_id", None),
                "selector": getattr(args, "selector", None),
                "n": args.n,
                "since_ts": getattr(args, "since", None),
            },
            timeout_s=timeout_s,
        )
    else:
        _print({"ok": False, "error_code": "UNKNOWN_EVENT_CMD", "cmd": args.event_cmd})
        return 2
    _print(result)
    return 0 if result.get("ok") else 2


# 與 doctor 報告中「advisory（缺少不致命）」的檢查項對應；這些項 ok=False 不
# 拉低整體 ok（無 systemd 可走 on-demand；無裝置可能只是還沒插線）。
_DOCTOR_ADVISORY_CHECKS = {"systemd", "wsl_systemd", "devices"}
# Windows（#131）：PATH／daemon endpoint／裝置皆 advisory（未起 daemon、exe 未入 PATH
# 不致命）；pyserial 為 Windows 序列埠後端硬依賴 → 非 advisory。
_DOCTOR_ADVISORY_CHECKS_WIN = {
    "serialwrap_on_path",
    "serialwrapd_on_path",
    "daemon_endpoint",
    "devices",
}


def _run_skill(args: argparse.Namespace) -> int:
    """輸出操作指南（skill）原文到 stdout（#131 點 4；唯讀、不需 daemon）。

    ``--platform windows`` → assets/skill/SKILL_WINDOWS.md；``linux`` → SKILL.md；
    ``auto``（預設）依實際平台選擇。stdout 已由 force_utf8_stdio 保證 UTF-8
    （cp950/cp1252 console 印 zh-tw 不炸，#118），使用者可直接 ``> SKILL.md`` 重導存檔。
    """
    platform = args.platform
    if platform == "auto":
        platform = "windows" if sys.platform.startswith("win") else "linux"
    name = "SKILL_WINDOWS.md" if platform == "windows" else "SKILL.md"
    from .assets import read_text  # 延遲匯入：僅此子命令需要

    sys.stdout.write(read_text(f"skill/{name}"))
    return 0


def _run_remote(args: argparse.Namespace) -> int:
    """serialwrap remote 分派：words → status / close / open。

    ``sw_core.remote_tunnel`` 於此延遲匯入（而非 cli.py 模組層級）：該模組硬依賴
    ``fcntl``（POSIX-only），cli.py 頂層需在 Windows 也能正常 import，故不得在
    模組層級引入；`guard_platform()` 在 open 分支對 native Windows fail-closed
    拒絕（``REMOTE_NOT_SUPPORTED``），status／close 分支則本期不受限（純讀寫
    ``<run_dir>/remote/`` 下的 state 檔）。全程例外皆經 ``except rt.TunnelError``
    轉為 JSON，不讓例外穿越 CLI 邊界。
    """
    from . import remote_tunnel as rt  # noqa: PLC0415

    run_dir = _remote_run_dir()
    words = list(getattr(args, "words", []) or [])
    try:
        if not words or words[0] == "status":
            _print(rt.status(run_dir))
            return 0
        if words[0] == "close":
            selector = words[1] if len(words) > 1 else "all"
            _print(rt.close(run_dir, selector))
            return 0

        rt.guard_platform()
        if args.reverse and args.forward:
            raise rt.TunnelError("INVALID_ARGS", "-R 與 -L 不可同時指定")
        role = "connect" if args.forward else "expose"  # -R 預設
        ssh_target, port = rt.parse_target(words[0])
        rt.resolve_ssh_bin("autossh" if args.autossh else "ssh")

        forward_src = None
        if role == "expose":
            forward_src = _forward_src_from_endpoint(_resolve_endpoint(args))

        spec = rt.TunnelSpec(
            role=role,
            ssh_target=ssh_target,
            port=port,
            local=args.local,
            forward_src=forward_src,
            remote_socket=args.remote_socket,
            via="autossh" if args.autossh else "ssh",
            ssh_opts=tuple(args.ssh_opt),
            ready_timeout=args.ready_timeout,
        )
        res = rt.open_tunnel(
            spec,
            run_dir,
            spawner=rt.real_spawner,
            runner=rt.make_runner(),
            ping=rt.real_ping,
        )
        _print(res)
        return 0
    except rt.TunnelError as exc:
        _print({"ok": False, "error_code": exc.code, "message": exc.message or exc.code})
        return 1


def _forward_src_from_endpoint(endpoint: str) -> str:
    """把 ``_resolve_endpoint`` 解出的 endpoint 轉成 ssh ``-R`` 的本機轉發源。

    unix endpoint → 原樣回傳 AF_UNIX 路徑；tcp endpoint → 改指 ``127.0.0.1:<port>``
    （對端只需連到本機 loopback 上該 daemon 監聽的 port，host 部分無意義）。
    """
    transport, address = _parse_endpoint(endpoint)
    if transport == "unix":
        return address  # AF_UNIX 路徑
    host, tcp_port = address
    return f"127.0.0.1:{tcp_port}"


def _remote_run_dir() -> str:
    """remote state 落在 ``<run_dir>/remote/``。

    **於呼叫時讀 env**（不可用 import-time ``constants.SOCKET_PATH`` 之類的凍結值）：
    測試以 ``monkeypatch.setenv("SERIALWRAP_RUN_DIR", ...)`` 於 import 之後覆寫，
    需要每次呼叫都即時生效，才能達到 per-test 隔離。
    """
    run = os.environ.get("SERIALWRAP_RUN_DIR")
    if run and run.strip():
        return os.path.expanduser(run)
    from . import constants  # noqa: PLC0415

    return constants.RUN_DIR


def _run_doctor(args: argparse.Namespace) -> int:
    """執行環境診斷並印出 JSON 報告；advisory 項不影響整體 ok。"""
    report = run_doctor()
    advisory = (
        _DOCTOR_ADVISORY_CHECKS_WIN
        if sys.platform.startswith("win")
        else _DOCTOR_ADVISORY_CHECKS
    )
    overall_ok = all(item["ok"] or item["check"] in advisory for item in report)
    _print({"ok": overall_ok, "checks": report})
    return 0


def _resolve_target_mode(args: argparse.Namespace, fx) -> str:
    """依旗標解析目標監管模式；未指定時 auto（有 systemd → user，否則 on-demand）。"""
    if getattr(args, "user", False):
        return "systemd-user"
    if getattr(args, "system", False):
        return "systemd-system"
    if getattr(args, "on_demand", False):
        return "on-demand"
    return "systemd-user" if fx.has_systemd() else "on-demand"


def _run_setup(args: argparse.Namespace) -> int:
    """物化資產並 reconcile 監管模式，輸出 legacy 偵測與 setup 結果。"""
    from .sysenv import SystemEffects

    fx = SystemEffects()

    # 1. legacy 偵測（僅指引、不刪除）。
    legacy = detect_legacy_install()

    # 2. 解析目標模式與目前（舊）模式。
    target = _resolve_target_mode(args, fx)
    old = _default_runtime_config().mode() or "on-demand"

    # 3. daemon/flash 偵測：best-effort，連不到一律 False，不阻擋 setup。
    #    flash 偵測須在物化「之前」——否則燒錄中仍會先覆寫 profiles/wrappers/skill 才報錯（Codex #1c）。
    daemon_running = False
    any_flashing = False
    try:
        daemon_running = bool(rpc_call(_resolve_endpoint(args), "health.ping", {}, timeout_s=0.5).get("ok"))
    except Exception:
        daemon_running = False
    try:
        any_flashing = bool(rpc_call(_resolve_endpoint(args), "mcu.status", {}, timeout_s=0.5).get("flashing"))
    except Exception:
        any_flashing = False

    # 4. flash 護欄前置：進行中且未 force → 立即中止，不物化、不動模式（Codex #1c）。
    if any_flashing and not args.force:
        _print({"ok": False, "error_code": "FLASHING_BUSY",
                "message": "flash 進行中，拒絕 setup（可用 --force 覆寫）"})
        return 2

    # 5. 物化套件資產到使用者可寫位置。
    materialize_assets(force=args.force)

    # 5b. WSL 偵測：若在 WSL 但 systemd 尚未啟用，主動寫 /etc/wsl.conf [boot] systemd=true，
    #     並早退提示使用者 `wsl --shutdown` 重啟後再跑 setup（systemd 須重進 WSL 才生效，
    #     當次無法直接起 systemd 服務）。已啟用或非 WSL → no-op，照常往下走。
    wsl_info = ensure_wsl_systemd(fx, os.path.expanduser("~"))
    if wsl_info.get("needs_restart"):
        _print({
            "ok": True,
            "legacy": legacy,
            "wsl_systemd_enabled": True,
            "needs_restart": True,
            "requested_mode": target,
            "hint": wsl_info.get("hint"),
        })
        return 0

    # 6. 有效 socket：systemd-system 走系統固定 socket，其餘走使用者 XDG 預設（Codex #1a/#1b）。
    effective_socket = SYSTEM_SOCKET if target == "systemd-system" else SOCKET_PATH

    # 7. reconcile（先停舊、再起新）；flash 進行中除非 force 否則拒絕。
    try:
        result = reconcile(
            old_mode=old,
            target_mode=target,
            fx=fx,
            home=os.path.expanduser("~"),
            daemon_running=daemon_running,
            any_flashing=any_flashing,
            with_sudo=args.with_sudo,
            force=args.force,
            socket_path=effective_socket,
            # config 寫入路徑須與所有讀取端（_default_runtime_config→CONFIG_DIR）一致，
            # 否則自訂 XDG_CONFIG_HOME/SERIALWRAP_CONFIG_DIR 下 writer≠reader 會分歧（I-1）。
            config_path=os.path.join(CONFIG_DIR, "config.yaml"),
        )
    except FlashingBusy as exc:
        _print({
            "ok": False,
            "error_code": "FLASHING_BUSY",
            "message": str(exc),
            "legacy": legacy,
        })
        return 2

    payload: dict[str, Any] = {
        "ok": True,
        "legacy": legacy,
        "setup": result,
        "doctor_hint": "serialwrap doctor 可驗證環境",
    }
    pending_sudo = result.get("pending_sudo")
    if pending_sudo:
        payload["pending_sudo"] = pending_sudo
    _print(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="serialwrap",
        description="serialwrap client（支援本機 Unix socket 與遠端 endpoint）",
        epilog=(
            "examples:\n"
            "  serialwrap session list\n"
            "  serialwrap --endpoint tcp://127.0.0.1:7777 session list\n"
            "  serialwrap --endpoint tcp://127.0.0.1:7777 cmd submit --selector COM0 --cmd 'uname -a'"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"serialwrap {_resolve_version()}", help="顯示版本後離開")
    p.add_argument("--socket", default=None, help="本機 daemon 的 Unix socket 路徑（未指定時依 config.yaml 與 XDG 執行期目錄解析，可用 SERIALWRAP_RUN_DIR 覆寫）")
    p.add_argument("--endpoint", default=None, metavar="ENDPOINT", help="遠端 daemon endpoint，例如 tcp://127.0.0.1:7777（優先於 --socket）")
    p.add_argument(
        "--timeout",
        dest="timeout_s",
        type=float,
        default=None,
        help="RPC timeout 秒數（未指定：一般方法 5.0；長操作 session attach/recover/self-test/console-attach 自動採固定 45.0 的 floor，#123）",
    )
    p.add_argument(
        "--retries",
        dest="retries",
        type=int,
        default=0,
        help="TIMEOUT／連線失敗時的重試次數，僅作用於冪等唯讀方法白名單（指數退避 0.5s 起、單次上限 5s；預設: %(default)s）",
    )

    sub = p.add_subparsers(
        dest="cmd",
        required=True,
        title="command groups",
        metavar="<group>",
    )

    p_daemon = sub.add_parser(
        "daemon",
        help="管理 serialwrap daemon（啟動／停止／狀態）",
        description="管理 serialwrap daemon 行程：啟動、停止與查詢執行狀態。",
    )
    daemon_sub = p_daemon.add_subparsers(dest="daemon_cmd", required=True, metavar="<command>")

    p_ds = daemon_sub.add_parser("start", help="啟動 daemon（--foreground 可前景執行；systemd 模式重導 service start）")
    p_ds.add_argument("--profile-dir", default=PROFILE_DIR, help="profile YAML 目錄（預設: %(default)s）")
    p_ds.add_argument("--lock", default=LOCK_PATH)
    p_ds.add_argument("--foreground", action="store_true")
    p_ds.add_argument(
        "--with-sudo",
        dest="with_sudo",
        action="store_true",
        default=False,
        help="systemd-system 模式下，daemon start 重導至 service start 時以 sudo 執行",
    )

    p_dstop = daemon_sub.add_parser("stop", help="停止執行中的 daemon")
    p_dstop.add_argument(
        "--with-sudo",
        dest="with_sudo",
        action="store_true",
        default=False,
        help="systemd-system 模式下，daemon stop 重導至 service stop 時以 sudo 執行",
    )
    daemon_sub.add_parser("status", help="顯示 daemon 狀態（pid／sessions／devices／log 路徑／多開偵測 multi_open）")

    p_device = sub.add_parser(
        "device",
        help="實體 UART 裝置列舉與 handoff（release／attach）",
        description="管理實體 UART 裝置：列舉裝置，以及把 raw device 暫時交給外部工具獨佔再收回。",
    )
    device_sub = p_device.add_subparsers(dest="device_cmd", required=True, metavar="<command>")
    device_sub.add_parser("list", help="列出實體 UART 裝置（real_path 與 by-id）")
    p_drel = device_sub.add_parser(
        "release",
        help="釋放 raw 裝置給外部工具獨佔（如 MCU 燒錄），進入 RELEASED 不自動搶回",
    )
    p_drel.add_argument("--selector", required=True)
    p_drel.add_argument("--source", default="cli")
    p_drel.add_argument("--reason", default=None)
    p_datt = device_sub.add_parser(
        "attach",
        help="收回先前 release 的裝置並重建 console（外部仍持有時回 DEVICE_STILL_HELD，--force 略過）",
    )
    p_datt.add_argument("--selector", required=True)
    p_datt.add_argument("--force", action="store_true")

    p_session = sub.add_parser(
        "session",
        help="session 生命週期、探測、recover、console 與 interactive 操作",
        description="管理 session：列舉與綁定、健康探測（self-test）、recover、console 與 interactive lease、capture log。",
    )
    sess_sub = p_session.add_subparsers(dest="session_cmd", required=True, metavar="<command>")
    sess_sub.add_parser("list", help="列出所有 session 及其狀態")
    p_sc = sess_sub.add_parser("clear", help="清除 session（detach 後會自動 re-attach；交接外部請改用 device release）")
    p_sc.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sb = sess_sub.add_parser("bind", help="把 session 綁定到指定裝置 by-id")
    p_sb.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sb.add_argument("--device-by-id", required=True)
    p_pin = sess_sub.add_parser("pin", help="把 device 釘到指定 profile（最高優先，繞過偵測）")
    p_pin.add_argument("--selector", required=True, help="session_id | COMx | alias | by-id | by-path")
    p_pin.add_argument("--profile", required=True, help="要釘的 profile/template 名")
    p_unpin = sess_sub.add_parser("unpin", help="解除 device 的 profile pin（保留 sticky）")
    p_unpin.add_argument("--selector", required=True, help="session_id | COMx | alias | by-id | by-path")
    p_sa = sess_sub.add_parser("attach", help="將 session attach 到裝置並建立 bridge")
    p_sa.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sst = sess_sub.add_parser("self-test", help="探測 session 健康度，回報 classification 與 recommended_action")
    p_sst.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sst.add_argument("--probe-timeout", dest="probe_timeout_s", type=float, default=2.0)
    p_sst.add_argument(
        "--strict-human-lock",
        action="store_true",
        default=False,
        help="嚴格模式：若 human interactive lease 仍在使用中則直接回報 busy；預設模式會先暫停 human interactive lease 再做 probe",
    )
    p_sact = sess_sub.add_parser("activity", help="顯示 session 的 RX／TX／state 活動")
    p_sact.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sr = sess_sub.add_parser("recover", help="重建 bridge 修復不健康的 session（TARGET_UNRESPONSIVE 時用這個，非 device attach）")
    p_sr.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sr.add_argument("--timeout", dest="recover_timeout_s", type=float, default=2.0)
    p_sr.add_argument("--force", action="store_true", help="force clear+reattach if normal recovery fails")
    p_sca = sess_sub.add_parser("console-attach", help="附加一個 console reader 到 session")
    p_sca.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sca.add_argument("--label")
    p_scd = sess_sub.add_parser("console-detach", help="卸除指定的 console reader")
    p_scd.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_scd.add_argument("--client-id", required=True)
    p_scl = sess_sub.add_parser("console-list", help="列出 session 上的 console readers")
    p_scl.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sio = sess_sub.add_parser("interactive-open", help="開啟 interactive lease（給全螢幕互動程式用）")
    p_sio.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sio.add_argument("--owner", default="agent")
    p_sio.add_argument("--timeout", dest="interactive_timeout_s", type=float, default=60.0)
    p_sio.add_argument("--command", default="")
    p_sio.add_argument(
        "--allow-attached",
        action="store_true",
        default=False,
        help="允許在 ATTACHED 狀態下開啟 bootloader recovery lease（需通過 bootloader prompt 比對）。"
             " 若 session 已有 human interactive lease 則暫停並在 close 時恢復。",
    )
    p_sis = sess_sub.add_parser("interactive-send", help="送出按鍵／資料到 interactive lease")
    p_sis.add_argument("--interactive-id", required=True)
    p_sis.add_argument("--data", required=True)
    p_sis.add_argument("--encoding", default="plain")
    p_sist = sess_sub.add_parser("interactive-status", help="讀取 interactive lease 目前畫面與狀態")
    p_sist.add_argument("--interactive-id", required=True)
    p_sist.add_argument("--screen-chars", type=int, default=2048)
    p_sic = sess_sub.add_parser("interactive-close", help="關閉 interactive lease")
    p_sic.add_argument("--interactive-id", required=True)
    p_sls = sess_sub.add_parser("log-start", help="開始該 session 的 capture log")
    p_sls.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_slst = sess_sub.add_parser("log-stop", help="停止該 session 的 capture log")
    p_slst.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_slstat = sess_sub.add_parser("log-status", help="查詢該 session 的 capture log 狀態")
    p_slstat.add_argument("--selector", required=True, help="session_id | COMx | alias")

    p_alias = sub.add_parser(
        "alias",
        help="session 別名與 by-id 綁定管理",
        description="管理 session 別名：列舉、指定到 session_id、綁定裝置 by-id 與解除綁定。",
    )
    alias_sub = p_alias.add_subparsers(dest="alias_cmd", required=True, metavar="<command>")
    alias_sub.add_parser("list", help="列出所有 alias 綁定")
    p_as = alias_sub.add_parser("set", help="把 alias 指定到既有 session_id")
    p_as.add_argument("--session-id", required=True)
    p_as.add_argument("--alias", required=True)
    p_aa = alias_sub.add_parser("assign", help="把 alias 綁到裝置 by-id（可附 profile）")
    p_aa.add_argument("--by-id", required=True)
    p_aa.add_argument("--alias", required=True)
    p_aa.add_argument("--profile")
    p_au = alias_sub.add_parser("unassign", help="移除 alias 綁定")
    p_au.add_argument("--alias", required=True)

    p_cmd = sub.add_parser(
        "cmd",
        help="提交命令並讀取結果（line／background）",
        description="向 session 提交命令並取回結果：line 模式看 status，background 模式用 result-tail。",
    )
    cmd_sub = p_cmd.add_subparsers(dest="cmd_cmd", required=True, metavar="<command>")
    p_cs = cmd_sub.add_parser(
        "submit",
        help="提交命令到 session（--mode line|background|interactive）",
        description=(
            "提交命令到 session（--mode line|background|interactive）。\n"
            "\n"
            "命令長度限制（--cmd 整條字串，以 UTF-8 位元組計；#129）：\n"
            f"  - > {CMD_WARN_BYTES} bytes：接受但附 CMD_LENGTH_WARNING（過長命令可能造成 UART 緩衝溢位或 prompt timeout）\n"
            f"  - > {CMD_REJECT_BYTES} bytes：直接拒絕（CMD_TOO_LONG）\n"
            "  - 含 \\n 換行字元：直接拒絕（CMD_CONTAINS_NEWLINE），請拆成多次獨立提交\n"
            "\n"
            "broker 對命令內容不做截斷；上述為 broker 對單一 --cmd 參數的上限，\n"
            "target 端 tty line buffer（常見 4096 bytes，target-dependent）才是物理單行限制。\n"
            "上限可由 `serialwrap daemon status` 回應的 limits 欄位執行期查詢，client 不需硬編碼。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p_cs.add_argument("--selector", required=True)
    p_cs.add_argument(
        "--cmd",
        dest="command_text",
        default="",
        help=f"要執行的單行命令（UTF-8 > {CMD_WARN_BYTES} bytes warning、> {CMD_REJECT_BYTES} bytes 拒絕，詳見上方說明）",
    )
    p_cs.add_argument("--source", default="agent")
    p_cs.add_argument("--mode", default="line")
    p_cs.add_argument("--priority", type=int, default=10)
    p_cs.add_argument("--cmd-timeout", dest="cmd_timeout_s", type=float, default=10.0)
    p_cs.add_argument("--expected-duration", dest="expected_duration_s", type=float, default=None)
    p_cg = cmd_sub.add_parser("status", help="查詢命令狀態與 stdout（line 模式讀這裡）")
    p_cg.add_argument("--cmd-id", required=True)
    p_cr = cmd_sub.add_parser("result-tail", help="增量讀取 background 命令的結果 chunk")
    p_cr.add_argument("--cmd-id", required=True)
    p_cr.add_argument("--from-chunk", type=int, default=0)
    p_cr.add_argument("--limit", type=int, default=200)
    p_cc = cmd_sub.add_parser("cancel", help="取消執行中的命令")
    p_cc.add_argument("--cmd-id", required=True)

    p_stream = sub.add_parser(
        "stream",
        help="即時 tail 解析後的文字事件串流",
        description="即時 tail session 解析後的文字串流（line 事件）。",
    )
    stream_sub = p_stream.add_subparsers(dest="stream_cmd", required=True, metavar="<command>")
    p_st = stream_sub.add_parser("tail", help="即時 tail 解析後的文字串流")
    p_st.add_argument("--selector")
    p_st.add_argument("--com")
    p_st.add_argument("--from-seq", type=int, default=0)
    p_st.add_argument("--limit", type=int, default=200)

    p_log = sub.add_parser(
        "log",
        help="raw／text 日誌 tail（含 timestamp／seq／crc）",
        description="tail raw 或純文字日誌；raw 含 timestamp／source／seq／crc，可做回放與稽核。",
    )
    log_sub = p_log.add_subparsers(dest="log_cmd", required=True, metavar="<command>")
    p_lr = log_sub.add_parser("tail-raw", help="tail raw 日誌（含 timestamp／source／seq／crc）")
    p_lr.add_argument("--selector")
    p_lr.add_argument("--com")
    p_lr.add_argument(
        "--from-seq",
        type=int,
        default=None,
        help="省略＝latest 模式（回傳最新 N 筆）；指定 N（含 0）＝range 模式（自 seq>N 起最舊 N 筆，增量讀取用）",
    )
    p_lr.add_argument("--limit", type=int, default=200)
    p_lt = log_sub.add_parser("tail-text", help="tail 純文字日誌")
    p_lt.add_argument("--selector")
    p_lt.add_argument("--com")
    p_lt.add_argument(
        "--from-seq",
        type=int,
        default=None,
        help="省略＝latest 模式（回傳最新 N 筆）；指定 N（含 0）＝range 模式（自 seq>N 起最舊 N 筆，增量讀取用）",
    )
    p_lt.add_argument("--limit", type=int, default=200)

    p_file = sub.add_parser(
        "file",
        help="透過 UART 推送／拉取檔案",
        description="透過 UART 在本機與 target 之間推送（push）或拉取（pull）檔案。",
    )
    file_sub = p_file.add_subparsers(dest="file_cmd", required=True, metavar="<command>")
    p_fp = file_sub.add_parser("push", help="透過 UART 推送本機檔案到 target")
    p_fp.add_argument("--selector", required=True)
    p_fp.add_argument("--local", required=True)
    p_fp.add_argument("--remote", required=True)
    p_fp.add_argument("--chunk-size", dest="chunk_size", type=int, default=2048)
    p_fp.add_argument("--source", default="agent")
    p_fl = file_sub.add_parser("pull", help="透過 UART 從 target 拉取檔案到本機")
    p_fl.add_argument("--selector", required=True)
    p_fl.add_argument("--remote", required=True)
    p_fl.add_argument("--local", default=None)
    p_fl.add_argument("--source", default="agent")

    p_wal = sub.add_parser(
        "wal",
        help="write-ahead log 匯出／重設／seq 查詢",
        description="操作 write-ahead log（WAL）：匯出區段、重設與查詢目前 seq。",
    )
    wal_sub = p_wal.add_subparsers(dest="wal_cmd", required=True, metavar="<command>")
    p_we = wal_sub.add_parser("export", help="匯出 WAL 區段（--from-seq／--to-seq／--limit）")
    p_we.add_argument("--from-seq", type=int, default=0)
    p_we.add_argument("--to-seq", type=int, default=0)
    p_we.add_argument("--limit", type=int, default=1000)
    wal_sub.add_parser("reset", help="重設 WAL")
    wal_sub.add_parser("current-seq", help="顯示目前 WAL seq")

    p_mcu = sub.add_parser(
        "mcu",
        help="MCU flash pattern 查詢與 flash 端點狀態",
        description="查詢 MCU flash pattern 清單與目前 flash 端點狀態（候選 COM port 與 is_flashing）。",
    )
    mcu_sub = p_mcu.add_subparsers(dest="mcu_cmd", required=True, metavar="<command>")
    mcu_sub.add_parser("patterns", help="列出所有已知 MCU 家族 flash pattern（family／probe／expect／baud）")
    mcu_sub.add_parser("status", help="顯示 flash 端點狀態：候選 COM port 清單與目前是否 flashing")

    p_remote = sub.add_parser(
        "remote",
        help="按需開關 ssh 反向隧道，讓遠端 agent 連本機 daemon（-R 預設 expose）",
        description=(
            "serialwrap remote：外包系統 ssh 建立 -R（expose，把本機 daemon 推到對端）／"
            "-L（connect，relay 情境把對端 port 拉回本機 loopback）隧道，background 常駐。\n"
            "  serialwrap remote user@host:7777        # -R 預設：expose 本機 daemon\n"
            "  serialwrap remote -L user@relay:7777    # connect（relay/雙 NAT）\n"
            "  serialwrap remote                        # 列目前隧道（status）\n"
            "  serialwrap remote close 7777|all         # 拆除\n"
            "安全：只透過 ssh-tunnel、單租戶/可信 relay 或 --remote-socket；不可對網路直接開放。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_remote.add_argument("words", nargs="*", help="[user@]host:port ｜ status ｜ close <port|all>")
    # 註：-R/-L 刻意不用 add_mutually_exclusive_group()——argparse 對同群組 store_true
    # 旗標的互斥是在 parse_args 當場 parser.error() → SystemExit(2)（usage 訊息印到
    # stderr），會讓「同時給 -R -L」整條命令繞過 _run_remote() 的 try/except
    # TunnelError，無法回傳本 CLI 一貫的機器可解析 JSON
    # {"ok": false, "error_code": ...} 邊界契約（「例外不得穿越 CLI 邊界」慣例）。
    # 改成兩個獨立旗標，互斥檢查留給 _run_remote() 內顯式判斷並 raise
    # TunnelError("INVALID_ARGS")。
    p_remote.add_argument("-R", dest="reverse", action="store_true", help="reverse/expose（預設）")
    p_remote.add_argument("-L", dest="forward", action="store_true", help="forward/connect（relay）")
    p_remote.add_argument("--autossh", action="store_true", help="以 autossh 斷線自動重連")
    p_remote.add_argument("--local", type=int, default=None, help="-L 本機 loopback port（預設=對端 port）")
    p_remote.add_argument(
        "--remote-socket",
        dest="remote_socket",
        default=None,
        help="硬化：-R 建遠端 unix socket／-L 連該 socket（共享 relay 建議）",
    )
    p_remote.add_argument(
        "--ready-timeout",
        dest="ready_timeout",
        type=float,
        default=10.0,
        help="readiness 確認上限秒數（逾時回 starting）",
    )
    p_remote.add_argument(
        "--ssh-opt",
        dest="ssh_opt",
        action="append",
        default=[],
        help="透傳額外 ssh 參數（可重複），如 --ssh-opt=-p --ssh-opt=2222",
    )
    # 註：--socket / --endpoint / --timeout 為既有全域參數，_resolve_endpoint 會取用。

    p_event = sub.add_parser(
        "event",
        help="event-trigger 規則註冊與 matcher 控制",
        description="event-trigger 規則註冊表與 matcher 控制：新增／刪除／列舉／啟用停用規則與檢視觸發紀錄。",
    )
    e_sub = p_event.add_subparsers(dest="event_cmd", required=True, metavar="<command>")

    e_add = e_sub.add_parser("add", help="從 JSON 檔註冊或更新一條規則")
    e_add.add_argument("--file", required=True)

    e_rm = e_sub.add_parser("rm", help="依 id 刪除一條規則")
    e_rm.add_argument("rule_id")

    e_list = e_sub.add_parser("list", help="列出規則（可依 selector／owner 過濾）")
    e_list.add_argument("--selector")
    e_list.add_argument("--owner")

    e_show = e_sub.add_parser("show", help="依 id 顯示單一規則內容")
    e_show.add_argument("rule_id")

    e_enable = e_sub.add_parser("enable", help="啟用指定 selector 的規則 matcher")
    e_enable.add_argument("--selector", required=True)

    e_disable = e_sub.add_parser("disable", help="停用指定 selector 的規則 matcher")
    e_disable.add_argument("--selector", required=True)

    e_status = e_sub.add_parser("status", help="顯示 matcher 狀態（可依 selector 過濾）")
    e_status.add_argument("--selector")

    e_reset = e_sub.add_parser("reset", help="重設規則計數／狀態（--rule-id 或 --selector 擇一）")
    grp = e_reset.add_mutually_exclusive_group(required=True)
    grp.add_argument("--rule-id")
    grp.add_argument("--selector")

    e_reload = e_sub.add_parser("reload", help="重新載入規則註冊表")

    e_tail = e_sub.add_parser("tail", help="檢視規則觸發紀錄（可依 rule-id／selector 過濾）")
    e_tail.add_argument("--rule-id")
    e_tail.add_argument("--selector")
    e_tail.add_argument("-n", type=int, default=50)
    e_tail.add_argument("--since", type=int)

    sub.add_parser(
        "supervision-mode",
        help="顯示有效的監管模式（on-demand、systemd-user 或 systemd-system）",
        description="印出 config.yaml 中的 supervision_mode，未設定時預設為 on-demand。供 shell 腳本（如 minicom_router.sh）查詢。",
    )

    p_svc = sub.add_parser(
        "service",
        help="透過 systemctl 管理 serialwrap systemd service（systemd 監管模式適用）",
        description=(
            "包裝 systemctl，按 config.yaml 的 supervision_mode 決定呼叫方式。\n"
            "systemd-user 模式：免 sudo。\n"
            "systemd-system 模式：start/stop/restart 需 root（加 --with-sudo 代跑）。\n"
            "on-demand 模式：不可用。"
        ),
    )
    p_svc.add_argument(
        "action",
        choices=["start", "stop", "restart", "status"],
        help="要執行的 systemctl 動作",
    )
    p_svc.add_argument(
        "--with-sudo",
        dest="with_sudo",
        action="store_true",
        default=False,
        help="systemd-system 模式的特權動作（start/stop/restart）以 sudo 執行",
    )

    p_setup = sub.add_parser(
        "setup",
        help="安裝資產並設定監管模式（systemd-user／systemd-system／on-demand）",
        description=(
            "物化套件資產（profiles／agent skill／minicom wrappers）到使用者位置，"
            "並 reconcile 監管模式（先停舊、再起新）。\n"
            "未指定模式時自動偵測（有 systemd → systemd-user，否則 on-demand）。\n"
            "system scope 的特權動作需 --with-sudo，否則只回報待執行的 sudo 指令。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    setup_mode = p_setup.add_mutually_exclusive_group()
    setup_mode.add_argument("--user", action="store_true", help="設為 systemd-user 模式（免 sudo）")
    setup_mode.add_argument("--system", action="store_true", help="設為 systemd-system 模式（特權動作需 --with-sudo）")
    setup_mode.add_argument(
        "--on-demand",
        dest="on_demand",
        action="store_true",
        help="設為 on-demand 模式（無 systemd 時的降級備援）",
    )
    p_setup.add_argument("--force", action="store_true", help="覆蓋既有 profiles，並在 flash 進行中仍強制切換")
    p_setup.add_argument(
        "--with-sudo",
        dest="with_sudo",
        action="store_true",
        default=False,
        help="systemd-system 模式下允許執行特權 sudo 指令（安裝 unit／enable／start）",
    )

    sub.add_parser(
        "doctor",
        help="診斷安裝與執行環境（平台感知：Linux 檢 dialout／systemd／by-id 裝置，Windows 檢 pyserial／daemon endpoint／COM 列舉）",
        description=(
            "對安裝與執行環境做一系列唯讀檢查並印出 JSON 報告（依平台選擇檢查清單，#131）。\n"
            "Linux：systemd／wsl_systemd／devices 為 advisory（缺少不致命，不拉低整體 ok）。\n"
            "Windows：serialwrap_on_path／serialwrapd_on_path／daemon_endpoint／devices 為 advisory。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    p_skill = sub.add_parser(
        "skill",
        help="輸出操作指南（skill）原文到 stdout（--platform windows 為 Windows 操作指南）",
        description=(
            "把內嵌的 serialwrap 操作指南（agent skill／操作手冊）原文印到 stdout（#131）。\n"
            "唯讀、不需 daemon；可重導存檔：serialwrap skill --platform windows > SKILL_WINDOWS.md"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p_skill.add_argument(
        "--platform",
        choices=("auto", "linux", "windows"),
        default="auto",
        help="指南平台；auto（預設）依目前平台選擇",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()  # Windows console cp1252 印繁中 help 會崩（#118），須在 parse_args 前
    p = build_parser()
    args = p.parse_args(argv)

    if args.cmd == "daemon":
        if args.daemon_cmd == "start":
            return _run_daemon_start(args)
        if args.daemon_cmd == "stop":
            return _run_daemon_stop(args)
        if args.daemon_cmd == "status":
            return _run_rpc(args, "health.status", {})

    if args.cmd == "device":
        if args.device_cmd == "list":
            return _run_rpc(args, "device.list", {})
        if args.device_cmd == "release":
            return _run_rpc(args, "device.release", {"selector": args.selector, "source": args.source, "reason": args.reason})
        if args.device_cmd == "attach":
            return _run_rpc(args, "device.attach", {"selector": args.selector, "force": args.force})

    if args.cmd == "session":
        if args.session_cmd == "list":
            return _run_rpc(args, "session.list", {})
        if args.session_cmd == "clear":
            return _run_rpc(args, "session.clear", {"selector": args.selector})
        if args.session_cmd == "bind":
            return _run_rpc(args, "session.bind", {"selector": args.selector, "device_by_id": args.device_by_id})
        if args.session_cmd == "pin":
            return _run_rpc(args, "session.pin", {"selector": args.selector, "profile": args.profile})
        if args.session_cmd == "unpin":
            return _run_rpc(args, "session.unpin", {"selector": args.selector})
        if args.session_cmd == "attach":
            return _run_rpc(args, "session.attach", {"selector": args.selector})
        if args.session_cmd == "self-test":
            return _run_rpc(
                args,
                "session.self_test",
                {
                    "selector": args.selector,
                    "timeout_s": args.probe_timeout_s,
                    "strict_human_lock": args.strict_human_lock,
                },
            )
        if args.session_cmd == "activity":
            return _run_rpc(args, "session.activity", {"selector": args.selector})
        if args.session_cmd == "recover":
            return _run_rpc(args, "session.recover", {"selector": args.selector, "timeout_s": args.recover_timeout_s, "force": getattr(args, "force", False)})
        if args.session_cmd == "console-attach":
            params: dict[str, Any] = {"selector": args.selector}
            if args.label:
                params["label"] = args.label
            return _run_rpc(args, "session.console_attach", params)
        if args.session_cmd == "console-detach":
            return _run_rpc(args, "session.console_detach", {"selector": args.selector, "client_id": args.client_id})
        if args.session_cmd == "console-list":
            return _run_rpc(args, "session.console_list", {"selector": args.selector})
        if args.session_cmd == "interactive-open":
            return _run_rpc(
                args,
                "session.interactive_open",
                {
                    "selector": args.selector,
                    "owner": args.owner,
                    "timeout_s": args.interactive_timeout_s,
                    "command": args.command,
                    "allow_attached": args.allow_attached,
                },
            )
        if args.session_cmd == "interactive-send":
            return _run_rpc(
                args,
                "session.interactive_send",
                {"interactive_id": args.interactive_id, "data": args.data, "encoding": args.encoding},
            )
        if args.session_cmd == "interactive-status":
            return _run_rpc(
                args,
                "session.interactive_status",
                {"interactive_id": args.interactive_id, "screen_chars": args.screen_chars},
            )
        if args.session_cmd == "interactive-close":
            return _run_rpc(args, "session.interactive_close", {"interactive_id": args.interactive_id})
        if args.session_cmd == "log-start":
            return _run_rpc(args, "session.log_start", {"selector": args.selector})
        if args.session_cmd == "log-stop":
            return _run_rpc(args, "session.log_stop", {"selector": args.selector})
        if args.session_cmd == "log-status":
            return _run_rpc(args, "session.log_status", {"selector": args.selector})

    if args.cmd == "alias":
        if args.alias_cmd == "list":
            return _run_rpc(args, "alias.list", {})
        if args.alias_cmd == "set":
            return _run_rpc(args, "alias.set", {"session_id": args.session_id, "alias": args.alias})
        if args.alias_cmd == "assign":
            params: dict[str, Any] = {"by_id": args.by_id, "alias": args.alias}
            if args.profile:
                params["profile"] = args.profile
            return _run_rpc(args, "alias.assign", params)
        if args.alias_cmd == "unassign":
            return _run_rpc(args, "alias.unassign", {"alias": args.alias})

    if args.cmd == "cmd":
        if args.cmd_cmd == "submit":
            submit_params: dict[str, Any] = {
                "selector": args.selector,
                "cmd": args.command_text,
                "source": args.source,
                "mode": args.mode,
                "priority": args.priority,
                "timeout_s": args.cmd_timeout_s,
            }
            if args.expected_duration_s is not None:
                submit_params["expected_duration_s"] = args.expected_duration_s
            return _run_rpc(args, "command.submit", submit_params)
        if args.cmd_cmd == "status":
            return _run_rpc(args, "command.get", {"cmd_id": args.cmd_id})
        if args.cmd_cmd == "result-tail":
            return _run_rpc(
                args,
                "command.result_tail",
                {"cmd_id": args.cmd_id, "from_chunk": args.from_chunk, "limit": args.limit},
            )
        if args.cmd_cmd == "cancel":
            return _run_rpc(args, "command.cancel", {"cmd_id": args.cmd_id})

    if args.cmd == "stream" and args.stream_cmd == "tail":
        selector = args.selector or args.com
        params: dict[str, Any] = {"from_seq": args.from_seq, "limit": args.limit}
        if selector:
            params["selector"] = selector
        return _run_rpc(args, "result.tail", params)

    if args.cmd == "log":
        selector = args.selector or args.com
        # --from-seq 省略時不放 key → daemon 走 latest 模式（#124）；顯式帶值（含 0）→ range 模式。
        params = {"limit": args.limit}
        if args.from_seq is not None:
            params["from_seq"] = args.from_seq
        if selector:
            params["selector"] = selector
        if args.log_cmd == "tail-raw":
            return _run_rpc(args, "log.tail_raw", params)
        if args.log_cmd == "tail-text":
            return _run_rpc(args, "log.tail_text", params)

    if args.cmd == "file":
        if args.file_cmd == "push":
            return _run_rpc(
                args,
                "file.push",
                {
                    "selector": args.selector,
                    "local_path": args.local,
                    "remote_path": args.remote,
                    "chunk_size": args.chunk_size,
                    "source": args.source,
                },
            )
        if args.file_cmd == "pull":
            p: dict[str, Any] = {
                "selector": args.selector,
                "remote_path": args.remote,
                "source": args.source,
            }
            if args.local is not None:
                p["local_path"] = args.local
            return _run_rpc(args, "file.pull", p)

    if args.cmd == "wal" and args.wal_cmd == "export":
        return _run_rpc(args, "wal.range", {"from_seq": args.from_seq, "to_seq": args.to_seq, "limit": args.limit})

    if args.cmd == "wal" and args.wal_cmd == "reset":
        return _run_rpc(args, "wal.reset", {})

    if args.cmd == "wal" and args.wal_cmd == "current-seq":
        return _run_rpc(args, "wal.current_seq", {})

    if args.cmd == "mcu":
        if args.mcu_cmd == "patterns":
            return _run_rpc(args, "mcu.patterns", {})
        if args.mcu_cmd == "status":
            return _run_rpc(args, "mcu.status", {})

    if args.cmd == "event":
        return _dispatch_event(args)

    if args.cmd == "supervision-mode":
        print(_default_runtime_config().mode() or "on-demand")
        return 0

    if args.cmd == "service":
        mode = _default_runtime_config().mode() or "on-demand"
        result = service_action(args.action, mode=mode, with_sudo=args.with_sudo)
        _print(result)
        return 0 if result.get("ok") else 2

    if args.cmd == "setup":
        return _run_setup(args)

    if args.cmd == "doctor":
        return _run_doctor(args)

    if args.cmd == "skill":
        return _run_skill(args)

    if args.cmd == "remote":
        return _run_remote(args)

    _print({"ok": False, "error_code": "INVALID_ARGS"})
    return 2


if __name__ == "__main__":
    sys.exit(main())
