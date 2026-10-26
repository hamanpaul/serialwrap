#!/usr/bin/env python3
"""P7（Slice 2.3）前置符號檢查（08-slice23-login-events.md Task 0 Step 1／Step 3.1：§0.3 上游 Consumes 逐項核對）。

對 §0.3 表與「其餘沿用」清單的每個上游 producer 符號，以 `(檔案, 正規式)` 檢查是否存在；函式另檢查本體
是否仍為 `s21_todo!`／`todo!` 骨架（P5／P6 尚未實作的行為）。每列標「最早消費的 P7 Task」：

  python3 scripts/rust/check_p7_prereqs.py              # 全表；缺任何一項 exit 1
  python3 scripts/rust/check_p7_prereqs.py --max-task 1 # 只把 Task ≤1 會用到的缺漏視為阻擋

輸出三種狀態：
  MISSING   符號不存在（上游缺陷或 §0.3 與實際 producer 不符；依計畫先改 §0.3／producer 計畫，不在業務碼起別名）
  SKELETON  符號存在但本體仍為 `s21_todo!`／`todo!`（上游行為尚未實作；消費它行為的 P7 Task 不得開工）
  DIFFERS   符號存在但形狀與 §0.3 宣告不同（列出實際形狀）

缺漏（MISSING）才影響 exit code；SKELETON／DIFFERS 為資訊列，供判定哪些 P7 Task 被上游擋住。stdlib only。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
CORE = "crates/sw-core/src"
PROTO = "crates/sw-proto/src"
PAL = "crates/sw-pal/src"
BB = "tests/contract_blackbox"

# (檔案, 正規式, 上游來源, 最早消費的 P7 Task, 若為函式則填函式名以檢查骨架本體)
CHECKS: list[tuple[str, str, str, int, str | None]] = [
    # §0.3 表
    (f"{CORE}/lib.rs", r"pub type Mono = u64;", "06 T0", 0, None),
    (f"{CORE}/session_core.rs", r"\bSessionAttach,", "06 T11", 7, None),
    (f"{CORE}/session_core.rs", r"SelfTest \{ timeout_s: f64, strict_human_lock: bool \}", "06 T11", 8, None),
    (f"{CORE}/session_core.rs", r"Recover \{ timeout_s: f64, force: bool \}", "06 T11", 9, None),
    (f"{CORE}/session_core.rs", r"\bLogStart,\s*LogStop,\s*LogStatus,", "06 T11", 10, None),
    (f"{CORE}/mailbox.rs", r"LoginFinished \{ epoch: u64, fsm_gen: u64, result: LoginOutcome, after_rx_seq: Option<u64> \}", "06 T11", 7, None),
    (f"{CORE}/session_core.rs", r"StartLogin \{ epoch: u64, fsm_gen: u64, plan: LoginPlan \}", "06 T11", 7, None),
    (f"{CORE}/session_core.rs", r"\bWarn\(String\)", "06 T11", 6, None),
    (f"{CORE}/session_core.rs", r"\bWalMeta \{", "06 T11", 6, None),
    (f"{CORE}/session_core.rs", r"RequestAttach", "06 T11（若缺由 P7 Task 0 補）", 5, None),
    (f"{CORE}/session_core.rs", r"fn begin_stop\b", "06 T12", 7, "begin_stop"),
    (f"{CORE}/arbiter.rs", r"pub fn flush_session\b", "06 T8", 7, "flush_session"),
    (f"{CORE}/session_core.rs", r"pub fn fg_begin\b", "07 T6", 8, "fg_begin"),
    (f"{CORE}/session_core.rs", r"pub fn fg_finish\b", "07 T6", 8, "fg_finish"),
    (f"{CORE}/generations.rs", r"pub conn_epoch: Option<u64>", "06 T11", 7, None),
    (f"{CORE}/generations.rs", r"pub bridge_generation: u64", "06 T11", 7, None),
    (f"{CORE}/generations.rs", r"pub input_epoch: u64", "07 T0 D-02", 4, None),
    (f"{CORE}/tx_writer.rs", r"pub enum FenceReject\b", "06 T9", 7, None),
    (f"{CORE}/tx_writer.rs", r"pub struct TxJob\b", "06 T9", 7, None),
    (f"{CORE}/tx_writer.rs", r"pub struct TxReport\b", "06 T9", 3, None),
    (f"{CORE}/tx_writer.rs", r"pub struct TxWriterHandle\b", "06 T9", 7, None),
    (f"{CORE}/tx_writer.rs", r"pub fn admit\b", "06 T9", 7, "admit"),
    (f"{CORE}/uart_bridge.rs", r"pub fn wait_for_regex\b", "06 T10", 3, "wait_for_regex"),
    (f"{CORE}/uart_bridge.rs", r"pub fn wait_for_regex_from\b", "06 T10", 3, "wait_for_regex_from"),
    (f"{CORE}/uart_bridge.rs", r"pub fn tail\b", "06 T10", 3, "tail"),
    (f"{CORE}/uart_bridge.rs", r"pub fn text_from\b", "06 T10", 3, "text_from"),
    (f"{CORE}/uart_bridge.rs", r"pub fn snapshot_len\b", "06 T10", 3, "snapshot_len"),
    (f"{CORE}/uart_bridge.rs", r"pub fn clear\b", "06 T10", 3, "clear"),
    (f"{CORE}/uart_bridge.rs", r"pub fn rx_stats\b", "06 T10", 3, "rx_stats"),
    (f"{CORE}/uart_bridge.rs", r"pub fn total_bytes\b", "06 T10", 6, "total_bytes"),
    (f"{CORE}/uart_bridge.rs", r"pub fn idle_s\b", "06 T10", 3, "idle_s"),
    (f"{CORE}/uart_bridge.rs", r"pub fn last_rx_mono\b", "06 T10", 6, "last_rx_mono"),
    (f"{CORE}/wal.rs", r"pub struct WalAppendReq\b", "06 T7", 6, None),
    (f"{CORE}/wal.rs", r"pub fn append\b", "06 T7", 6, "append"),
    (f"{CORE}/lease.rs", r"pub fn lease_context\b", "07 T7", 8, "lease_context"),
    (f"{CORE}/session_core.rs", r"pub fn invalidate_human_input\b", "07 T6 D-02", 4, "invalidate_human_input"),
    (f"{PROTO}/session_last_errors.rs", r"pub fn as_string\b", "P3 生成檔", 3, None),
    (f"{PROTO}/session_last_errors.rs", r"pub fn parse\b", "P3 生成檔", 3, None),
    (f"{PROTO}/pycompat.rs", r"pub fn py_int_parse\b", "P3 T4", 2, None),
    (f"{PROTO}/pycompat.rs", r"pub fn py_strip\b", "P3 T4", 2, None),
    (f"{PROTO}/pycompat.rs", r"pub fn splitlines\b", "P3 T4", 2, None),
    (f"{PROTO}/pycompat.rs", r"pub fn universal_newlines\b", "P3 T4", 2, None),
    (f"{PROTO}/rpc_types.rs", r"pub fn int_or\b", "06 T0c", 17, None),
    (f"{PROTO}/pyfmt.rs", r"pub fn py_str\b", "P3 T4", 8, None),
] + [
    (f"{PROTO}/pycompat.rs", rf"pub fn {name}\b", src, 0, None)
    for name, src in [
        ("clean_text", "06 T0c"), ("strip_ansi", "06 T0c"), ("py_round", "06 T0c"), ("strftime_local", "06 T0c"),
        ("iso_utc_now", "06 T0c"), ("uuid4_hex", "06 T0c"), ("uuid4_dashed", "06 T0c"),
        ("decode_utf8_replace", "P3 04 T4"), ("py_len_chars", "P3 04 T4"),
    ]
] + [
    (f"{BB}/bb_harness.py", rf"def {name}\b", "P3 T2", 18, None)
    for name in ("isolated_env", "run_cli", "is_rust", "ab_label")
] + [
    (f"{BB}/bb_daemon.py", r"class PyDaemon\b", "P3 T2", 18, None),
    (f"{BB}/bb_faketarget.py", r"class FakeTarget\b", "P3 T2", 18, None),
    (f"{BB}/bb_faketarget.py", r"def rx_log\b", "P3 T2", 18, None),
    (f"{BB}/conftest.py", r"def faketarget\b", "P3 T2", 18, None),
    (f"{BB}/conftest.py", r"def py_daemon\b", "P3 T2", 18, None),
    # 「其餘沿用」
    (f"{PROTO}/pyre/mod.rs", r"pub fn compile\b", "P3", 3, None),
    (f"{PROTO}/pyre/mod.rs", r"pub struct PyRegex\b", "P3", 3, None),
    (f"{PROTO}/pyre/mod.rs", r"pub struct PyMatch\b", "P3", 12, None),
    (f"{PROTO}/pyre/mod.rs", r"pub struct PyFlags\b", "P3", 11, None),
    (f"{PROTO}/pyre/mod.rs", r"pub struct PyReError\b", "P3", 11, None),
    (f"{PROTO}/pyre/mod.rs", r"pub fn escape\b", "P3", 3, None),
    # `pyre::parse::parse` 實為私有模組；redos.rs 放在 pyre/ 之下可經 `super::parse` 取用（形狀差異見 DIFFERS）。
    (f"{PROTO}/pyre/mod.rs", r"^mod parse;", "P3", 11, None),
    (f"{PROTO}/config_types.rs", r"pub struct SessionProfile\b", "P3", 2, None),
    (f"{PROTO}/config_types.rs", r"pub struct ProfileTemplate\b", "P3", 3, None),
    (f"{PAL}/fs_ops.rs", r"pub trait FsOps\b", "P4 T1", 6, None),
    (f"{PAL}/traits.rs", r"pub trait FsRead\b", "P3 T9", 13, None),
    (f"{CORE}/login_fsm.rs", r"pub struct LoginIo\b", "06 T13", 3, None),
    (f"{CORE}/login_fsm.rs", r"pub enum LoginOutcome\b", "06 T13", 3, None),
    (f"{CORE}/login_fsm.rs", r"pub enum LoginPlan\b", "06 T13", 3, None),
    (f"{CORE}/login_fsm.rs", r"pub fn choose_login_plan\b", "06 T13", 3, "choose_login_plan"),
    (f"{CORE}/login_fsm.rs", r"pub fn probe_ready\b", "06 T13", 3, "probe_ready"),
    (f"{CORE}/login_fsm.rs", r"pub fn ensure_ready\b", "06 T13", 3, "ensure_ready"),
    (f"{CORE}/login_fsm.rs", r"pub fn prompt_timeout_error\b", "06 T13", 3, "prompt_timeout_error"),
    (f"{CORE}/login_fsm.rs", r"pub const LOGIN_FSM_DETAIL_ERRORS\b", "06 T13", 3, None),
    (f"{CORE}/session_auth.rs", r"pub struct SessionAuth\b", "06 T13", 2, None),
    (f"{CORE}/session_auth.rs", r"pub struct AuthResolution\b", "06 T13", 2, None),
    (f"{CORE}/session_auth.rs", r"pub fn parse_env_file\b", "06 T13", 2, "parse_env_file"),
    (f"{CORE}/session_auth.rs", r"pub fn resolve_session_auth\b", "06 T13", 2, "resolve_session_auth"),
    (f"{CORE}/session_auth.rs", r"pub fn credentials_declared_but_unresolved\b", "06 T13", 2, "credentials_declared_but_unresolved"),
    (f"{CORE}/quiet_gate.rs", r"pub struct QuietState\b", "06 T13", 4, None),
    (f"{CORE}/quiet_gate.rs", r"pub fn on_rx_text\b", "06 T13", 4, "on_rx_text"),
    (f"{CORE}/quiet_gate.rs", r"pub fn detect_boot_banner\b", "06 T13", 3, "detect_boot_banner"),
    (f"{CORE}/quiet_gate.rs", r"pub fn is_reboot_command\b", "06 T13", 4, "is_reboot_command"),
    (f"{CORE}/quiet_gate.rs", r"pub fn slice21_unsupported_feature\b", "06 T13", 3, "slice21_unsupported_feature"),
    (f"{CORE}/op_runner.rs", r"pub enum OpSpec\b", "06 T14", 7, None),
    (f"{CORE}/op_runner.rs", r"pub enum OpOutcome\b", "06 T14", 7, None),
    (f"{CORE}/op_runner.rs", r"pub fn spawn\b", "06 T14", 7, "spawn"),
    (f"{CORE}/self_test.rs", r"pub fn classify_pre_probe\b", "06 T14", 8, "classify_pre_probe"),
    (f"{CORE}/capture.rs", r"pub struct AgentLogCapture\b", "06 T14", 10, None),
    (f"{CORE}/capture.rs", r"pub struct BackgroundCapture\b", "06 T14", 10, None),
    (f"{CORE}/mailbox.rs", r"pub fn recv_next\b", "06 T11", 7, "recv_next"),
    (f"{CORE}/session_core.rs", r"pub fn on_event\b", "06 T11", 4, "on_event"),
    (f"{CORE}/actor.rs", r"pub fn spawn_actor\b", "06 T11", 7, "spawn_actor"),
    (f"{CORE}/uart_bridge.rs", r"pub fn spawn_reader\b", "06 T10", 10, "spawn_reader"),
    (f"{CORE}/session_manager.rs", r"pub struct SessionManager\b", "06 T16", 16, None),
]

# §0.3 宣告形狀與實際 producer 已知不同之處（核對結果；處置見 08 §0.3「Task 0 核對註記」）。
DIFFERS: list[tuple[str, str, str]] = [
    (f"{CORE}/tx_writer.rs", r"pub fsm_gen: u64,", "§0.3／Task 7 宣告 `TxPermit.fsm_gen: Option<u64>`；06 實際為 `u64`（FenceState 同為 `fsm_gen: u64`）"),
    (f"{CORE}/tx_writer.rs", r"\bFsmMismatch,", "§0.3 宣告新增 `FenceReject::StaleFsmGen`；06 已有同用途的 `FsmMismatch`（相等比對，非 floor）"),
    (f"{CORE}/login_fsm.rs", r"-> sw_pal::TxResult,", "§0.3 宣告 `LoginIo.tx` 回 `TxReport`；06 實際回 `sw_pal::TxResult`"),
    (f"{PROTO}/pyre/mod.rs", r"pub fn compile\(pat: &str, flags: PyFlags\) -> Result<PyRegex, CompileError>", "`pyre::compile` 回 `CompileError`（非 `PyReError`）"),
    (f"{PROTO}/pyre/mod.rs", r"^mod parse;", "`pyre::parse` 為私有模組（`parse()` 為 pub(super)，回 `ParsedPattern`）；無 `pyre::sre::SubPattern`"),
]

_FN_TODO = re.compile(r"\b(s21_todo!|todo!|unimplemented!)\s*\(")


def _fn_is_skeleton(text: str, name: str) -> bool | None:
    """找 `fn name` 第一個定義，取其本體（到第一個同縮排 `}`）；本體含 todo 類巨集即骨架。"""
    m = re.search(rf"^(\s*)(?:pub(?:\([^)]*\))?\s+)?fn {re.escape(name)}\b", text, re.M)
    if m is None:
        return None
    indent = m.group(1)
    rest = text[m.end():]
    end = re.search(rf"^{indent}\}}", rest, re.M)
    body = rest[: end.start()] if end else rest[:400]
    return _FN_TODO.search(body) is not None


def check(root: pathlib.Path):
    missing, skeleton = [], []
    for rel, pat, src, task, fn in CHECKS:
        path = root / rel
        if not path.is_file():
            missing.append((rel, pat or "<檔案存在>", src, task))
            continue
        text = path.read_text(encoding="utf-8")
        if pat and re.search(pat, text, re.M) is None:
            missing.append((rel, pat, src, task))
            continue
        if fn and _fn_is_skeleton(text, fn):
            skeleton.append((rel, fn, src, task))
    differs = []
    for rel, pat, note in DIFFERS:
        path = root / rel
        if path.is_file() and re.search(pat, path.read_text(encoding="utf-8"), re.M):
            differs.append((rel, note))
    return missing, skeleton, differs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=pathlib.Path, default=ROOT)
    ap.add_argument("--max-task", type=int, default=None, help="只把最早消費 Task ≤ N 的缺漏視為阻擋")
    args = ap.parse_args(argv)
    missing, skeleton, differs = check(args.root)
    limit = args.max_task
    blocking = [m for m in missing if limit is None or m[3] <= limit]
    print(f"[check_p7_prereqs] 檢查 {len(CHECKS)} 項：缺漏 {len(missing)}、骨架 {len(skeleton)}、形狀不同 {len(differs)}")
    for rel, pat, src, task in missing:
        tag = "阻擋" if (limit is None or task <= limit) else "後續 Task 需要"
        print(f"MISSING ({tag}) {rel} :: {pat}（上游 {src}；P7 Task {task} 需要）")
    for rel, fn, src, task in sorted(skeleton, key=lambda r: (r[3], r[0], r[1])):
        print(f"SKELETON {rel} :: fn {fn}（上游 {src} 尚未實作；P7 Task {task} 起需要其行為）")
    for rel, note in differs:
        print(f"DIFFERS {rel} :: {note}")
    if blocking:
        print("[check_p7_prereqs] FAIL：上游缺陷，停止並回報（不在 P7 補做）")
        return 1
    print("[check_p7_prereqs] OK" + ("" if limit is None else f"（Task ≤{limit}）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
