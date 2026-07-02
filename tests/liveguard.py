"""#120 live guard：live 路徑公式、快照、純函式判定。

設計約束：
- 頂層 import 不得碰 sw_core（conftest 在覆寫 env 前 import 本模組；提前 import sw_core
  會把 constants 凍結在 live 路徑）。需要 RPC 時於函式內 lazy import。
- live 路徑一律 XDG 公式並「刻意忽略 SERIALWRAP_*」隔離變數——daemon（systemd）env
  沒有它們；勿沿用 setup_cmd._state_path_for（systemd-system 回 /var/lib 與實況不符）。
- 判定函式為純函式（吃快照、回 (verdict, reason)），失敗模式由 tests/test_liveguard.py 逐一釘住。
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess

POLLUTION_MARKERS = ("/tmp/sw-", "test-tpl", '"test:', "fake-uart")
STRUCTURAL_KEYS = ("aliases", "bindings", "released")
VERDICT_FAIL = "FAIL"
VERDICT_WARN = "WARN"
VERDICT_PASS = "PASS"
VERDICT_SKIP = "SKIP"


@dataclasses.dataclass(frozen=True)
class FileSnap:
    exists: bool
    content: bytes = b""
    size: int = 0


@dataclasses.dataclass(frozen=True)
class DaemonSnap:
    reachable: bool
    active: bool = False
    main_pid: int | None = None
    # session_id → (last_tx_at, bridge_generation)
    sessions: dict[str, tuple[str | None, int | None]] = dataclasses.field(default_factory=dict)


# ---- live 路徑公式 ----

def live_state_path() -> str:
    home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(home, "serialwrap", "state.json")


def live_wal_path() -> str:
    home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(home, "serialwrap", "wal", "raw.wal.ndjson")


def live_config_path() -> str:
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(home, "serialwrap", "config.yaml")


# ---- 快照 ----

def snap_file(path: str) -> FileSnap:
    try:
        with open(path, "rb") as fp:
            content = fp.read()
    except OSError:
        return FileSnap(exists=False)
    return FileSnap(exists=True, content=content, size=len(content))


def snap_daemon() -> DaemonSnap:
    """唯讀查 systemd unit 與 live daemon session 快照；任何一步失敗 → unreachable（SKIP）。"""
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "ActiveState,MainPID", "serialwrap"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        props = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
        active = props.get("ActiveState") == "active"
        main_pid = int(props.get("MainPID") or 0) or None
    except Exception:
        return DaemonSnap(reachable=False)
    if not active or not main_pid:
        return DaemonSnap(reachable=False)
    try:
        from sw_core.client import rpc_call  # lazy：此時 env 已隔離，僅用 client、endpoint 顯式傳入

        endpoint = _live_socket_path()
        resp = rpc_call(endpoint, "session.list", {}, timeout_s=3.0)
        sessions: dict[str, tuple[str | None, int | None]] = {}
        for s in resp.get("sessions") or []:
            sessions[s.get("session_id", "?")] = (s.get("last_tx_at"), s.get("bridge_generation"))
    except Exception:
        return DaemonSnap(reachable=False)
    return DaemonSnap(reachable=True, active=active, main_pid=main_pid, sessions=sessions)


def _live_socket_path() -> str:
    """live CLI 同款解析：config.yaml socket_path → systemd-system canonical。唯讀。"""
    try:
        import yaml

        with open(live_config_path(), "r", encoding="utf-8") as fp:
            cfg = yaml.safe_load(fp) or {}
        sock = cfg.get("socket_path")
        if isinstance(sock, str) and sock.strip():
            return sock.strip()
    except Exception:
        pass
    return "/run/serialwrap/serialwrapd.sock"


# ---- 純函式判定 ----

def _structural_damage(pre: FileSnap, post: FileSnap) -> str | None:
    """回傳結構性破壞原因；無則 None。pre 必須 exists。"""
    if not post.exists:
        return "live state.json 被刪除"
    try:
        pre_obj = json.loads(pre.content)
        post_obj = json.loads(post.content)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return "live state.json 內容非 JSON（截斷/損毀）"
    for key in STRUCTURAL_KEYS:
        pre_entries = set((pre_obj.get(key) or {}).keys()) if isinstance(pre_obj, dict) else set()
        post_entries = set((post_obj.get(key) or {}).keys()) if isinstance(post_obj, dict) else set()
        missing = pre_entries - post_entries
        if missing:
            return f"live state.json 的 {key} 少了 {sorted(missing)}"
    text = post.content.decode("utf-8", errors="replace")
    for marker in POLLUTION_MARKERS:
        if marker in text:
            return f"live state.json 含污染特徵 {marker!r}"
    return None


def classify_state(pre: FileSnap, post: FileSnap, mode: str) -> tuple[str, str]:
    if not pre.exists and not post.exists:
        return VERDICT_PASS, "live state.json 不存在（前後皆然）"
    if not pre.exists and post.exists:
        return VERDICT_FAIL, "live state.json 於測試期間被建立（隔離失效）"
    if pre.exists and not post.exists:
        return VERDICT_FAIL, "live state.json 於測試期間被刪除"
    if pre.content == post.content:
        return VERDICT_PASS, "live state.json byte-identical"
    damage = _structural_damage(pre, post)
    if damage:
        return VERDICT_FAIL, damage
    if mode == "warn":
        return VERDICT_WARN, "live state.json 有非結構性變更（warn 模式放行，請人工確認）"
    return VERDICT_FAIL, "live state.json 內容變更（strict 模式；確為 live daemon 合法活動時可 SERIALWRAP_LIVE_GATE=warn）"


def classify_wal(pre: FileSnap, post: FileSnap) -> tuple[str, str]:
    if pre.exists and not post.exists:
        return VERDICT_FAIL, "live WAL 於測試期間消失（wal reset/清除特徵）"
    if pre.exists and post.exists and post.size < pre.size:
        return VERDICT_FAIL, f"live WAL 縮小（{pre.size}→{post.size}；wal reset 特徵，罕見誤報源：64MB rotation）"
    return VERDICT_PASS, "live WAL 未縮小（append 為 live daemon 常態）"


def classify_shell_wal(pre: FileSnap, post: FileSnap) -> tuple[str, str]:
    if pre.exists != post.exists or pre.size != post.size:
        return VERDICT_FAIL, "外層 shell SERIALWRAP_WAL_DIR 的 WAL 有變更（env 繼承類回歸）"
    return VERDICT_PASS, "shell WAL 維度未變"


def classify_config(pre: FileSnap, post: FileSnap) -> tuple[str, str]:
    if pre.exists != post.exists or pre.content != post.content:
        return VERDICT_FAIL, "live config.yaml 於測試期間被變更（CLI 應唯讀）"
    return VERDICT_PASS, "live config.yaml 未變"


def classify_daemon(pre: DaemonSnap, post: DaemonSnap) -> tuple[str, str]:
    if not pre.reachable:
        return VERDICT_SKIP, "live daemon 不存在或不可達（CI/無 daemon 機器）"
    if not post.reachable or not post.active:
        return VERDICT_FAIL, "live daemon 於測試期間停止或不可達（測試動到 live service）"
    if post.main_pid != pre.main_pid:
        return VERDICT_FAIL, f"live daemon MainPID 變更（{pre.main_pid}→{post.main_pid}；被 restart）"
    for sid, (pre_tx, pre_gen) in pre.sessions.items():
        post_tx, post_gen = post.sessions.get(sid, (None, None))
        if post_tx != pre_tx:
            return VERDICT_FAIL, f"live session {sid} 的 last_tx_at 前進（{pre_tx}→{post_tx}；有東西對真板 TX）"
        if post_gen != pre_gen:
            return VERDICT_FAIL, f"live session {sid} 的 bridge_generation 變更（{pre_gen}→{post_gen}；被 rebind/reattach）"
    return VERDICT_PASS, "live daemon 未被觸碰"
