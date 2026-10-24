#!/usr/bin/env python3
"""P6（Slice 2.2）前置符號檢查（07-slice22-console.md Task 1 Step 1／Step 2）。

對計畫「跨 Phase 介面」區塊的每個上游符號，以 `(檔案, 正規式)` 表逐項檢查並列出缺漏。
每列另標「最早消費的 P6 Task」，供分段開工時判斷缺漏是否擋住目前要做的 Task：

  python3 scripts/rust/check_p6_prereqs.py              # 全表；缺任何一項 exit 1（計畫原義）
  python3 scripts/rust/check_p6_prereqs.py --max-task 5 # 只把 Task ≤5 會用到的缺漏視為阻擋；其餘列為「後續 Task 需要」

缺漏屬上游 Phase（P2／P3／P5）缺陷：停止並回報，不在 P6 補做。stdlib only。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
CORE = "crates/sw-core/src"

# (檔案, 正規式, 上游來源, 最早消費的 P6 Task)
CHECKS: list[tuple[str, str, str, int]] = [
    (f"{CORE}/generations.rs", r"pub struct Generations\b", "06 T11", 1),
    (f"{CORE}/mailbox.rs", r"pub enum ControlEvent\b", "06 T11", 1),
    (f"{CORE}/mailbox.rs", r"\bTick\b", "06 T11", 7),
    (f"{CORE}/mailbox.rs", r"LeaseExpiry", "06 T14", 7),
    (f"{CORE}/session_core.rs", r"pub enum SessionRequest\b", "06 T11", 1),
    (f"{CORE}/session_core.rs", r"InteractiveOpen \{", "06 T14", 7),
    (f"{CORE}/session_core.rs", r"fn begin_stop\b", "06 T12", 6),
    (f"{CORE}/session_core.rs", r"pub struct PublicCtx\b", "06 T11", 10),
    (f"{CORE}/tx_writer.rs", r"pub enum FenceReject\b", "06 T9", 1),
    (f"{CORE}/tx_writer.rs", r"pub struct TxWriterHandle\b", "06 T9", 6),
    (f"{CORE}/arbiter.rs", r"pub struct SubmitReq\b", "06 T8", 1),
    (f"{CORE}/quiet_gate.rs", r"pub fn detect_boot_banner\b", "06 T13", 6),
    (f"{CORE}/quiet_gate.rs", r"pub fn is_reboot_command\b", "06 T13", 6),
    (f"{CORE}/op_runner.rs", r"pub fn encode_interactive_payload\b", "06 T14", 7),
    (f"{CORE}/op_runner.rs", r"SelfTestProbe", "06 T14", 6),
    (f"{CORE}/lease.rs", r"pub struct Tombstones\b", "06 T14", 1),
    (f"{CORE}/uart_bridge.rs", r"pub fn tail\(", "06 T10", 7),
    (f"{CORE}/uart_bridge.rs", r"sentinel:", "06 T10", 9),
    ("crates/sw-pal/src/console_pty/posix.rs", r"", "06 T2", 8),
    ("crates/sw-core/tests/support/mod.rs", r"pub struct CoreHarness\b", "06 T11", 6),
    ("crates/sw-core/tests/support/mod.rs", r"ActorRig", "06 T11", 6),
    ("crates/sw-daemon/tests/support/mod.rs", r"pub struct ServiceRig\b", "06 T18", 10),
    ("tests/contract_blackbox/bb_harness.py", r"def is_rust\b", "P3 04 T2", 11),
    ("tests/contract_blackbox/bb_harness.py", r"def isolated_env\b", "P3 04 T2", 0),
    # 計畫 Task 1 文字寫 `tests/contract_blackbox/faketarget.py`，但「跨 Phase 介面」註明 P2 沒有該檔、
    # FakeTarget 位於 P3 `bb_faketarget.py`（06 T15a 擴充 emit／written）；依後者檢查。
    ("tests/contract_blackbox/bb_faketarget.py", r"def emit\b", "06 T15a", 11),
    ("tests/contract_blackbox/bb_faketarget.py", r"def written\b", "06 T15a", 11),
    ("tests/contract_blackbox/slice21/conftest.py", r"def shell_daemon\b", "06 T15／T20", 11),
    ("tests/acceptance/regen_lock.py", r"", "P2 03 T12", 0),
    ("scripts/rust/goldens/_tsv.py", r"", "P3 04 T3", 2),
    # Task 3 Consumes：pycompat 的 UTF-8 replace 解碼與 06 T0c 的 shlex_split。
    ("crates/sw-proto/src/pycompat.rs", r"pub fn decode_utf8_replace\b", "P3 04 T4", 3),
    ("crates/sw-proto/src/pycompat.rs", r"pub fn shlex_split\b", "06 T0c", 3),
    ("crates/sw-proto/src/pycompat.rs", r"pub (struct|enum) ShlexError\b", "06 T0c", 3),
    # Task 0 Consumes：P0 catalog loader 與 P5 凍結工具。
    ("tools/contracts/catalog.py", r"def load_catalogs\b", "P0 01 T1", 0),
    ("scripts/rust/freeze_slice_cases.py", r"", "06 T0b", 0),
]


def check(root: pathlib.Path) -> list[tuple[str, str, str, int]]:
    missing = []
    for rel, pat, src, task in CHECKS:
        path = root / rel
        if not path.is_file():
            missing.append((rel, pat or "<檔案存在>", src, task))
            continue
        if pat and re.search(pat, path.read_text(encoding="utf-8")) is None:
            missing.append((rel, pat, src, task))
    return missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=pathlib.Path, default=ROOT)
    ap.add_argument("--max-task", type=int, default=None,
                    help="只把最早消費 Task ≤ N 的缺漏視為阻擋（預設：全部阻擋）")
    a = ap.parse_args(argv)
    missing = check(a.root)
    blocking = [m for m in missing if a.max_task is None or m[3] <= a.max_task]
    later = [m for m in missing if m not in blocking]
    print(f"[check_p6_prereqs] 檢查 {len(CHECKS)} 項，缺漏 {len(missing)} 項")
    for rel, pat, src, task in blocking:
        print(f"MISSING (阻擋) {rel} :: {pat}（上游 {src}；P6 Task {task} 需要）")
    for rel, pat, src, task in later:
        print(f"MISSING (後續) {rel} :: {pat}（上游 {src}；P6 Task {task} 需要，不擋 --max-task {a.max_task}）")
    if blocking:
        print("[check_p6_prereqs] FAIL：上游缺陷，停止並回報（不在 P6 補做）")
        return 1
    print("[check_p6_prereqs] OK" + ("" if not later else f"（另有 {len(later)} 項屬後續 Task）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
