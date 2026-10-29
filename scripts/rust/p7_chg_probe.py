#!/usr/bin/env python3
"""P7 第 5 輪「案例設計修正」的 Python 基線實測探針（rulings/p7.md 2026-10-29：逐列登記 CHG、附實測證據）。

每個子命令以 Python 基線 daemon（`tests/contract_blackbox/bin/py-serialwrapd`，經 `bb_p7.P7Rig`）＋PTY
`FakeTarget` 重現 `docs/rust-refactor/slice23-chg-register.md` 一列 CHG 的「Python 實測」欄，輸出一行 JSON
（`json.dumps(sort_keys=True)`）。只觀測外部 CLI／session list，不 import sw_core。

  python3 scripts/rust/p7_chg_probe.py attached-history     # 初次 attach 失敗只見 DETACHED／ATTACHING
  python3 scripts/rust/p7_chg_probe.py sc34-initial-nonce   # 初次 attach 的 nonce 逾時：attach ok:true、落 DETACHED
  python3 scripts/rust/p7_chg_probe.py ctrl-c               # 逾時救援 CTRL_C 的命令紀錄欄位
  python3 scripts/rust/p7_chg_probe.py stall-order          # READY recover 先回 PROMPT_TIMEOUT，stall 出現在之後的列
  python3 scripts/rust/p7_chg_probe.py noise-inline         # 雜訊直接接在 `login: ` 後（不換行）→ 永不 READY
  python3 scripts/rust/p7_chg_probe.py noise-own-line       # 對照：雜訊以 `\\r\\n` 起頭 → READY
"""
from __future__ import annotations

import collections
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/contract_blackbox"))

from bb_faketarget import FakeTarget  # noqa: E402
from bb_p7 import P7Rig, wait_state  # noqa: E402


def _row(rig: P7Rig) -> dict:
    _, j = rig.cli("session", "list")
    return [x for x in (j or {}).get("sessions", []) if x["com"] == "COM0"][0]


def _observe(rig: P7Rig, seconds: float) -> dict:
    seen: collections.Counter = collections.Counter()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        s = _row(rig)
        seen[f"{s['state']}/{s['last_error']}"] += 1
        time.sleep(0.25)
    return dict(seen)


def attached_history(tmp: Path) -> dict:
    tgt = FakeTarget(tmp / "t0", prompt="root@op3:~# ",
                     login_steps=[{"expect": "", "respond": "\r\nop3 login: "}, {"expect": "root", "respond": ""},
                                  {"expect": "__never__", "respond": ""}])   # 帳號之後永不出現 Password prompt
    rig = P7Rig(tmp)
    try:
        rig.start("shell", [tgt])
        return {"probe": "attached-history", "profile": "shell", "observed_state_last_error": _observe(rig, 45.0)}
    finally:
        rig.stop()


def sc34_initial_nonce(tmp: Path) -> dict:
    tgt = FakeTarget(tmp / "t0", prompt="root@op3:~# ", login_steps=[
        {"expect": "", "respond": "\r\nop3 login: "}, {"expect": "root", "respond": "\r\nPassword: "},
        {"expect": "pw", "respond": "\r\nroot@op3:~# "}, {"expect": "__never__", "respond": ""}])
    rig = P7Rig(tmp)
    try:
        rig.start("shell", [tgt])
        seen = _observe(rig, 30.0)
        rc, a = rig.cli("session", "attach", "--selector", "COM0", timeout_s=90.0)
        return {"probe": "sc34-initial-nonce", "observed_state_last_error": seen,
                "attach": {"rc": rc, "ok": (a or {}).get("ok"), "session_state": ((a or {}).get("session") or {}).get("state"),
                           "last_error": ((a or {}).get("session") or {}).get("last_error")}}
    finally:
        rig.stop()


def ctrl_c(tmp: Path) -> dict:
    tgt = FakeTarget(tmp / "t0")
    rig = P7Rig(tmp)
    try:
        rig.start("shell", [tgt])
        wait_state(rig.env, "COM0", "READY", timeout_s=60.0)
        _, sub = rig.cli("cmd", "submit", "--selector", "COM0", "--cmd", "sleep 60", "--cmd-timeout", "2")
        deadline = time.monotonic() + 20.0
        cmd: dict = {}
        while time.monotonic() < deadline and not cmd.get("done_at"):
            _, st = rig.cli("cmd", "status", "--cmd-id", sub["cmd_id"])
            cmd = (st or {}).get("command") or {}
            time.sleep(0.3)
        keys = ("status", "error_code", "partial", "recovery_action", "stdout")
        return {"probe": "ctrl-c", "command": {k: cmd.get(k) for k in keys}, "state": _row(rig)["state"]}
    finally:
        rig.stop()


def stall_order(tmp: Path) -> dict:
    tgt = FakeTarget(tmp / "t0")
    rig = P7Rig(tmp)
    try:
        rig.start("shell", [tgt])
        wait_state(rig.env, "COM0", "READY", timeout_s=60.0)
        tgt.freeze_rx()
        time.sleep(35.0)
        rc, j = rig.cli("session", "recover", "--selector", "COM0", timeout_s=90.0)
        recover = {k: (j or {}).get(k) for k in ("ok", "error_code", "partial", "recovery_action")}
        first_stall = None
        t0 = time.monotonic()
        rows = []
        while time.monotonic() - t0 < 40.0:
            s = _row(rig)
            rows.append(f"{s['state']}/{s['last_error']}")
            if s["last_error"] == "TRANSPORT_STALL" and first_stall is None:
                first_stall = {"state": s["state"], "after_s": round(time.monotonic() - t0, 1),
                               "detail_prefix": (s.get("last_error_detail") or "")[:5]}
                break
            time.sleep(0.5)
        return {"probe": "stall-order", "recover_rc": rc, "recover": recover, "first_stall_row": first_stall,
                "rows_before_stall": sorted(set(rows))}
    finally:
        rig.stop()


def _noise(tmp: Path, pattern: str, seconds: float) -> dict:
    tgt = FakeTarget(tmp / "t0", prompt="# ", password="pw-noise", delay_ms=0,
                     noise={"interval_ms": 500, "pattern": pattern},
                     login_steps=[{"expect": "", "respond": "\r\nLogin: "}, {"expect": "root", "respond": "\r\nPassword: "},
                                  {"expect": "pw-noise", "respond": "\r\nCM> "}],
                     commands={"overrides": {"sh": {"response": "\r\n# "}}})
    rig = P7Rig(tmp)
    try:
        rig.start("bcm", [tgt])
        return {"pattern": pattern, "interval_ms": 500, "observed_state_last_error": _observe(rig, seconds)}
    finally:
        rig.stop()


def main(argv: list[str]) -> int:
    probes = {
        "attached-history": attached_history,
        "sc34-initial-nonce": sc34_initial_nonce,
        "ctrl-c": ctrl_c,
        "stall-order": stall_order,
        "noise-inline": lambda t: {"probe": "noise-inline", **_noise(t, "[  12.345678] kernel: usb 1-1: noise", 60.0)},
        "noise-own-line": lambda t: {"probe": "noise-own-line", **_noise(t, "\r\n[  12.345678] kernel: usb 1-1: noise", 60.0)},
    }
    if len(argv) != 1 or argv[0] not in probes:
        print(f"用法：p7_chg_probe.py {{{'|'.join(probes)}}}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="p7chg-") as tmp:
        out = probes[argv[0]](Path(tmp))
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
