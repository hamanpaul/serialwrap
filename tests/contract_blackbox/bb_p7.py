"""P7（Slice 2.3）黑箱 helper（Task 18A）。

profile 樣板代換、CLI＋JSON 呼叫、session 狀態輪詢與 fixture ``p7`` 的 ``P7Rig``。
stdlib＋PyYAML 以外不依賴；不得 import sw_core／serialwrap／serialwrapd。

daemon 以 ``--socket`` 指定 endpoint，CLI 沒有對應的 env 覆寫，故 ``P7Rig.env`` 另帶測試工具專用鍵
``SW_P7_SOCKET``（非 ``SERIALWRAP_`` 前綴，不影響產品），``cli_json``／``wait_state`` 見到即補 ``--socket``。
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Mapping, Sequence

from bb_daemon import PyDaemon
from bb_faketarget import FakeTarget
from bb_harness import isolated_env, run_cli

PROFILE_DIR = Path(__file__).resolve().parent / "p7_profiles"
SOCKET_ENV = "SW_P7_SOCKET"
# 只代換大寫佔位（`${BY_ID}`、`${ENV_FILE}`…）；`${nonce}` 屬 ready_probe 語法，原樣保留。
_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def p7_profile_dir(tmp: Path, name: str, **subst: str) -> Path:
    """複製 ``p7_profiles/<name>.yaml`` 至 ``tmp``（建立目錄）並代換佔位；缺代換值 → ``KeyError``。"""
    text = (PROFILE_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    out = _PLACEHOLDER.sub(lambda m: str(subst[m.group(1)]), text)
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / f"{name}.yaml").write_text(out, encoding="utf-8")
    return tmp


def cli_json(args: Sequence[str], env: Mapping[str, str], timeout_s: float = 60.0) -> tuple[int, dict | None]:
    """``run_cli``＋``json.loads(stdout)``；stdout 非 JSON object 時第二項為 ``None``。"""
    argv = list(args)
    sock = env.get(SOCKET_ENV)
    if sock and "--socket" not in argv:
        argv = ["--socket", sock, *argv]
    r = run_cli(argv, env=env, timeout_s=timeout_s)
    try:
        obj = json.loads(r.stdout)
    except ValueError:
        return r.rc, None
    return r.rc, obj if isinstance(obj, dict) else None


def _session_row(env: Mapping[str, str], sel: str) -> dict | None:
    _, obj = cli_json(["session", "list"], env)
    for row in (obj or {}).get("sessions", []) or []:
        if sel in (row.get("com"), row.get("alias"), row.get("session_id")):
            return row
    return None


def wait_state(env: Mapping[str, str], sel: str, want: str, timeout_s: float = 30.0) -> dict:
    """輪詢 ``session list`` 至 ``sel``（COM／alias／session_id）進入 ``want``；逾時 ``AssertionError``。"""
    deadline = time.monotonic() + timeout_s
    last: dict | None = None
    while time.monotonic() < deadline:
        last = _session_row(env, sel)
        if last is not None and last.get("state") == want:
            return last
        time.sleep(0.2)
    raise AssertionError(f"{sel} 未於 {timeout_s}s 內進入 {want}；最後狀態 {last!r}")


class P7Rig:
    """fixture ``p7`` 的回傳：一個隔離 env、一個 daemon（``daemon_prefix()`` 決定 Python 或 Rust）與其 FakeTarget。"""

    def __init__(self, tmp: Path) -> None:
        self.tmp = Path(tmp)
        self.env: dict[str, str] = isolated_env(self.tmp / "env")
        self._sock_root = Path(tempfile.mkdtemp(prefix="swp7-"))   # AF_UNIX 路徑上限，socket 放短路徑
        self.env[SOCKET_ENV] = str(self._sock_root / "d.sock")
        self.daemon: PyDaemon | None = None
        self.targets: list[FakeTarget] = []

    def start(self, profile: str, targets: list[FakeTarget], extra_env: Mapping[str, str] = {}) -> None:
        """啟動各 target（by-id 置於隔離 env 的 ``SERIALWRAP_BY_ID_DIR``）、寫 env_file、代換 profile 並啟動 daemon。

        代換值：``BY_ID``＝第一個 target、``BY_ID_<i>``＝第 i 個、``ENV_FILE``＝帳密檔
        （``SW_P7_USER``／``SW_P7_PASS``＝第一個 target 的 user／password；有 console_lock 時另寫
        ``SW_P7_CONSOLE_UNLOCK``）。
        """
        by_id_dir = Path(self.env["SERIALWRAP_BY_ID_DIR"])
        subst: dict[str, str] = {}
        for i, ft in enumerate(targets):
            ft.release_slave = True   # daemon 相容模式（見 FakeTarget docstring）
            link = ft.start(by_id_dir, name=f"usb-FAKE_TARGET_{i}-if00")
            self.targets.append(ft)
            subst[f"BY_ID_{i}"] = str(link)
        if targets:
            subst["BY_ID"] = subst["BY_ID_0"]
        lines: list[str] = []
        if targets:
            first = targets[0]
            lines += [f"SW_P7_USER={first.user}", f"SW_P7_PASS={first.password}"]
            if first.console_lock:
                lines.append(f"SW_P7_CONSOLE_UNLOCK={first.console_lock.get('secret', '')}")
        env_file = self.tmp / "p7.env"
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        subst["ENV_FILE"] = str(env_file)
        pdir = p7_profile_dir(self.tmp / "profiles", profile, **subst)
        daemon_env = {**self.env, **dict(extra_env)}
        self.daemon = PyDaemon(daemon_env, profile_dir=pdir, socket=self.env[SOCKET_ENV], lock=self.tmp / "p7.lock")
        self.daemon.start()

    def cli(self, *args: str, timeout_s: float = 60.0) -> tuple[int, dict | None]:
        return cli_json(args, self.env, timeout_s=timeout_s)

    def wal_records(self, sel: str, limit: int = 500) -> list[dict]:
        """經 CLI ``log tail-raw``（latest 模式）取最新 ``limit`` 筆 WAL 記錄。"""
        rc, obj = self.cli("log", "tail-raw", "--selector", sel, "--limit", str(limit))
        if rc != 0 or not obj or not obj.get("ok"):
            raise AssertionError(f"log tail-raw 失敗：rc={rc} obj={obj!r}")
        return list(obj.get("records") or [])

    def stop(self) -> None:
        if self.daemon is not None:
            try:
                self.daemon.stop()
            finally:
                self.daemon = None
        for ft in self.targets:
            ft.stop()
        self.targets.clear()
        shutil.rmtree(self._sock_root, ignore_errors=True)


__all__ = ["P7Rig", "cli_json", "p7_profile_dir", "wait_state", "PROFILE_DIR", "SOCKET_ENV"]
