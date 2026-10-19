"""F10 裝置交接：release 回條與在途命令中止（Stage 0 BC-03、BC-04、SC-04；規格 §6.2）。

entry: TestPilot ``serialwrap_regression``（``allow_destructive: true``；case id 見下方 ``@_case``）
revision: ``git rev-parse HEAD:regression/serialwrap_regression/cases/f10_device_handoff.py``（blob sha1，執行時記入 evidence）
expected: ``f10-release-drain-receipt`` 的 ``device release`` ok 回應只在 handle 關閉後送出（外部 open(tty) 成功時間 ≤ 回應時間）；
  ``f10-release-aborts-inflight-command`` 在途長命令於 release 後 ``COMMAND_ABORTED_IN_FLIGHT``，該 cmd_id 的 TX 只出現一次（無重送）。
owner: 擁有者實機（TestPilot bench；Linux 非 root 執行，root 會繞過 TIOCEXCL 使 oracle 失效）
evidence_dir: ``~/b-log/rust-refactor/p1/<case-id>/<YYMMDD-HHMMSS>/``（經 TestPilot report 目錄收集）
rollback: 任一步失敗時 case 的 finally 會以 prod ``device attach`` 收回裝置並等 READY；若仍卡住，手動執行
  ``serialwrap device attach --selector <COM>`` 並以 ``serialwrap session list`` 確認 state 為 READY。

全程在 ``guards.ThrowawayDaemon`` sandbox 內操作（獨立 XDG／socket／by-id），prod 端只做 device release／attach 交接，組態零接觸。
"""
from __future__ import annotations

import base64
import collections
import errno
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from realhw.harness import CaseResult

from .. import guards
from ..harness import Case, register
from .f10_login_creds import (
    _PROD_PROFILE_CANDIDATES,
    _reclaim_and_ensure_ready,
    _render_profile_yaml,
    _sandbox_symlink_for_board,
    _wait_dynamic_sessions,
    _wait_state,
    _ta_com,
)

_OPEN_POLL_S = 0.001  # 外部 reader 輪詢間隔（≤1 ms）：避免 open 成功時間被量化高估
_CONTINUOUS_TX_BYTES = 4000  # 接近 CMD_WARN_BYTES（4 KB）的長命令：UART 寫入需數百毫秒，release 落在寫入中
_TX_SETTLE_S = 0.1  # submit 後很快 release，使 release 與持續 TX 重疊
_OPEN_DEADLINE_S = 30.0
_READY_WAIT_S = 90.0
_CMD_ABORTED = "COMMAND_ABORTED_IN_FLIGHT"


def _blob_sha1(path: Path) -> str:
    """與 ``git hash-object`` 相同的 blob sha1（不依賴 git 可用性）。"""
    data = path.read_bytes()
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _case(id, title, issues, hints=(), requires=(), destructive=True, pending_issue=False):
    def deco(fn):
        register(Case(id=id, family="F10", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints),
                      pending_issue=pending_issue))
        return fn
    return deco


def evaluate_release_receipt(ctx: Any) -> CaseResult:
    """release 回條 oracle：外部 reader 第一次成功 ``open(tty)`` 的時間必須 ≤ ``device release`` 回應時間。

    ctx 欄位：``open_success_time``（reader 成功開啟 tty 的 monotonic 秒數）、``release_time``（回應送達的 monotonic 秒數）。
    open 成功早於回應 → handle 在回應前已關閉（PASS）；回應早於 open 成功 → 回應時 handle 仍被持有（FAIL, category ``test``）。
    """
    open_t = getattr(ctx, "open_success_time", None)
    rel_t = getattr(ctx, "release_time", None)
    if open_t is None or rel_t is None:
        return CaseResult("SKIP", reason="缺少 open 成功時間或 release 回應時間，無法判定回條語意",
                          category="environment", reason_code="insufficient_evidence")
    if rel_t < open_t:
        return CaseResult(
            "FAIL",
            reason=f"device release 回應（{rel_t:.6f}）早於外部 open(tty) 成功（{open_t:.6f}），"
                   "回應送出時 handle 仍未關閉（BC-04／SC-04 回歸：回條須在 handle 關閉後）",
            category="test", reason_code="release_receipt_before_handle_close",
        )
    return CaseResult(
        "PASS",
        reason=f"外部 open(tty) 成功（{open_t:.6f}）不晚於 release 回應（{rel_t:.6f}），handle 關閉先於回條",
    )


def evaluate_inflight_abort(ctx: Any) -> CaseResult:
    """在途命令 oracle：同一 cmd_id 的 TX 不得重送（同 payload 出現 ≥2 次 → FAIL ``resend_detected``）。

    ctx 欄位：``cmd_id``、``wal_records``（WAL 記錄 list，取 ``dir == "TX"`` 且 ``cmd_id`` 相符者）、
    可選 ``cmd_status``（``cmd status`` 回應 JSON 文字；含 ``COMMAND_ABORTED_IN_FLIGHT`` 才算中止符合）。
    """
    cmd_id = getattr(ctx, "cmd_id", None)
    records = list(getattr(ctx, "wal_records", None) or [])
    tx = [r for r in records if r.get("dir") == "TX" and r.get("cmd_id") == cmd_id and r.get("payload_b64")]
    counts = collections.Counter(str(r["payload_b64"]) for r in tx)
    repeated = {p: n for p, n in counts.items() if n >= 2}
    if repeated:
        decoded = [base64.b64decode(p).decode("utf-8", errors="replace") for p in repeated]
        return CaseResult(
            "FAIL",
            reason=f"cmd_id={cmd_id} 的 TX 相同 payload 出現多次（{decoded}），疑似 release 後重送（BC-03 回歸）",
            category="test", reason_code="resend_detected",
        )
    status_text = getattr(ctx, "cmd_status", None)
    if status_text is not None and _CMD_ABORTED not in str(status_text):
        return CaseResult(
            "FAIL",
            reason=f"release 後 cmd status 未見 {_CMD_ABORTED}（實際：{str(status_text)[:200]}）",
            category="test", reason_code="abort_code_mismatch",
        )
    if not tx:
        return CaseResult("SKIP", reason=f"WAL 未見 cmd_id={cmd_id} 的 TX 記錄，無法判定重送",
                          category="environment", reason_code="insufficient_evidence")
    return CaseResult("PASS", reason=f"cmd_id={cmd_id} 的 TX 僅出現一次，未重送")


def _prod_template_for(platform: str) -> dict[str, Any] | None:
    """唯讀掃描 prod profile 候選，取指定 platform 的 template dict（只讀結構，不讀帳密值）。"""
    for cand in _PROD_PROFILE_CANDIDATES:
        path = Path(cand).expanduser()
        if not path.is_file():
            continue
        try:
            obj = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        profiles = obj.get("profiles") if isinstance(obj, dict) else None
        if not isinstance(profiles, dict):
            continue
        for tpl in profiles.values():
            if isinstance(tpl, dict) and str(tpl.get("platform", "")).strip().lower() == platform.lower():
                return tpl
    return None


def _board_by_id_real_path(board: dict[str, Any]) -> str:
    """prod by-id 連結解析成實體 tty 路徑（外部 reader 直接 open 這個路徑）。"""
    serial = str(board.get("serial") or "").strip()
    match = next((p for p in sorted(Path("/dev/serial/by-id").glob("*")) if serial and serial in p.name), None)
    if match is None:
        raise LookupError(f"/dev/serial/by-id 找不到含 serial={serial} 的裝置")
    return os.path.realpath(match)


class _OpenProbe:
    """背景輪詢 ``os.open(tty)``；第一次成功即記錄 monotonic 時間並關閉 fd。

    ``first_attempt_failed`` 為真僅代表第一次開啟因 ``EBUSY`` 失敗（TIOCEXCL 生效，daemon 確實持有 tty）；
    其他錯誤（如 ``ENOENT``）不得被當成獨占證據，會記入 ``other_errors`` 且不計入 ``first_attempt_failed``。
    """

    def __init__(self, tty: str) -> None:
        self._tty = tty
        self.open_success_time: float | None = None
        self.first_attempt_failed = False
        self.other_errors: list[str] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        deadline = time.monotonic() + _OPEN_DEADLINE_S
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            try:
                fd = os.open(self._tty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno == errno.EBUSY:
                    if attempts == 1:
                        self.first_attempt_failed = True
                elif len(self.other_errors) < 5:
                    self.other_errors.append(f"errno={exc.errno}: {exc.strerror}")
                time.sleep(_OPEN_POLL_S)
                continue
            self.open_success_time = time.monotonic()
            os.close(fd)
            return


def _load_wal_records(ta: guards.ThrowawayDaemon) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in ta.wal_text().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _start_throwaway(ctx: Any, board: dict[str, Any], by_id_dir: Path, template: dict[str, Any],
                     workdir: Path) -> guards.ThrowawayDaemon:
    profile_yaml = _render_profile_yaml(template, profile_name="swreg-handoff",
                                        env_file=str(template.get("env_file") or ""))
    return guards.ThrowawayDaemon(exe=str(ctx.cfg["serialwrap_exe"]), workdir=workdir,
                                  by_id_dir=by_id_dir, profile_yaml=profile_yaml)


def _bring_up_ready(ctx: Any, ta: guards.ThrowawayDaemon, board: dict[str, Any]) -> tuple[str | None, CaseResult | None]:
    listing = _wait_dynamic_sessions(ta, timeout_s=20.0)
    ctx.note("ta-session-list-initial.json", str(listing))
    if not (listing.get("sessions") or []):
        return None, CaseResult("SKIP", reason="throwaway 動態偵測未見 session（sandbox by-id 未見裝置）",
                                category="environment", reason_code="no_dynamic_session")
    ta_com = _ta_com(listing, str(board.get("serial") or ""))
    attach = ta.run("session", "attach", "--selector", ta_com, timeout=60)
    ctx.note("ta-attach.json", str(attach))
    ready = _wait_state(ta, ta_com, "READY", timeout_s=_READY_WAIT_S)
    if ready.get("state") != "READY":
        return None, CaseResult("SKIP", reason=f"throwaway session 未達 READY（state={ready.get('state')!r}）",
                                category="environment", reason_code="throwaway_not_ready")
    return ta_com, None


def _run_with_handoff(ctx: Any, case_id: str, body) -> CaseResult:
    """共用骨架：prod release → throwaway 上線 READY → 執行 body(ta, ta_com) → finally prod attach 收回。"""
    ctx.note(f"{case_id}-case-revision.txt", f"blob={_blob_sha1(Path(__file__))}\n")
    boards = ctx.cfg.get("boards") or []
    if not boards:
        return CaseResult("SKIP", reason="testbed 未設定 boards", category="configuration",
                          reason_code="boards_not_configured")
    board = boards[0]
    com = str(board["com"])
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return CaseResult("SKIP", reason="以 root 執行會繞過 TIOCEXCL，無法驗證 BC-04 獨占語意",
                          category="environment", reason_code="root_bypasses_exclusive")
    template = _prod_template_for(str(board.get("platform") or "shell"))
    if template is None:
        return CaseResult("SKIP", reason=f"prod profile 找不到 platform={board.get('platform')!r} 的 template",
                          category="environment", reason_code="template_not_found")
    try:
        _sandbox_symlink_for_board(board, ctx.case_dir / "byid")  # 先驗證 sandbox 能見到板子，再交出 prod 裝置
    except LookupError as exc:
        return CaseResult("SKIP", reason=str(exc), category="environment", reason_code="board_by_id_not_found")

    release = ctx.sw.run("device", "release", "--selector", com,
                         "--source", "agent:swreg", "--reason", f"{case_id} regression")
    ctx.note(f"{case_id}-prod-release.json", str(release))
    if not release.get("ok"):
        return CaseResult("FAIL", reason=f"prod device release 失敗（selector={com}）：{release!r}",
                          category="environment", reason_code="release_failed")

    result: CaseResult | None = None
    try:
        try:
            with _start_throwaway(ctx, board, ctx.case_dir / "byid", template, ctx.case_dir / "ta") as ta:
                ta_com, skip = _bring_up_ready(ctx, ta, board)
                result = skip if skip is not None else body(ctx, ta, ta_com, board)
        except RuntimeError as exc:
            result = CaseResult("SKIP", reason=f"throwaway daemon 未在時限內就緒：{exc}",
                                category="environment", reason_code="throwaway_start_failed")
    finally:
        reclaim_fail = _reclaim_and_ensure_ready(ctx, com)
        if reclaim_fail is not None:
            result = reclaim_fail
    return result if result is not None else CaseResult(
        "FAIL", reason="case 邏輯未產生結果（不應發生）", category="test", reason_code="case_logic_error")


def _body_release_receipt(ctx: Any, ta: guards.ThrowawayDaemon, ta_com: str, board: dict[str, Any]) -> CaseResult:
    tty = _board_by_id_real_path(board)
    # 持續 TX：長命令（接近 4 KB 軟上限）在 release 時仍在寫入／在途，release 不得在寫入未完成前回條。
    payload = "sleep 20; echo X; " + "#" * (_CONTINUOUS_TX_BYTES - len("sleep 20; echo X; "))
    sub = ta.run("cmd", "submit", "--selector", ta_com, "--cmd", payload, "--cmd-timeout", "30")
    ctx.note("ta-submit.json", f"cmd_id={sub.get('cmd_id')} bytes={_CONTINUOUS_TX_BYTES} rc={sub.get('_rc')}")
    if not sub.get("cmd_id"):
        return CaseResult("FAIL", reason=f"submit 未回 cmd_id：{sub!r}", category="environment",
                          reason_code="submit_no_cmd_id")
    time.sleep(_TX_SETTLE_S)  # 讓寫入進行中
    inflight = _cmd_status_of(ta, str(sub.get("cmd_id")))
    if inflight in ("done", "error", "timeout"):
        return CaseResult("SKIP", reason=f"release 前命令已終態（{inflight}），非在途 release，不判定",
                          category="environment", reason_code="in_flight_not_confirmed")
    probe = _OpenProbe(tty)
    probe.start()  # 於 release 前即開始輪詢：首次 open 應因 daemon 持有 tty 而 EBUSY
    rel_resp, release_time = ta.run_timed("device", "release", "--selector", ta_com, "--source", "agent:swreg",
                                          "--reason", "f10 release receipt", timeout=60)
    probe.join(_OPEN_DEADLINE_S)
    ctx.note("ta-release.json", str(rel_resp))
    ctx.note("ta-release-inflight.json", str({"status_before_release": inflight}))
    if not rel_resp.get("ok"):
        return CaseResult("FAIL", reason=f"throwaway device release 失敗：{rel_resp!r}",
                          category="environment", reason_code="release_failed")
    if release_time is None:
        return CaseResult("SKIP", reason="release 回應未見 stdout，無法定位回條時刻", category="environment",
                          reason_code="insufficient_evidence")
    if probe.open_success_time is None:
        return CaseResult("SKIP", reason="外部 reader 於時限內未能 open(tty) 成功，回條無法判定",
                          category="environment", reason_code="insufficient_evidence")
    if not probe.first_attempt_failed:
        return CaseResult("SKIP", reason="release 前 tty 未被 EBUSY 持有（首次 open 未失敗於獨占），證據不足",
                          category="environment", reason_code="insufficient_evidence")
    verdict = evaluate_release_receipt(SimpleNamespace(open_success_time=probe.open_success_time,
                                                       release_time=release_time))
    ctx.note("release-receipt-oracle.json", json.dumps({
        "verdict": verdict.verdict, "reason_code": verdict.reason_code, "reason": verdict.reason,
        "other_open_errors": probe.other_errors}, ensure_ascii=False))
    if verdict.verdict != "PASS":
        return verdict
    # SC-05：release 後 device attach 須恢復 ATTACHING→READY（不得只靠收尾時 prod reclaim）。
    attach = ta.run("device", "attach", "--selector", ta_com, timeout=60)
    ctx.note("ta-attach-after-release.json", str(attach))
    after = _wait_state(ta, ta_com, "READY", timeout_s=_READY_WAIT_S)
    if after.get("state") != "READY":
        return CaseResult("FAIL", reason=f"release 後 device attach 未恢復 READY（state={after.get('state')!r}）",
                          category="test", reason_code="attach_after_release_failed")
    return verdict


def _cmd_status_of(ta: guards.ThrowawayDaemon, cmd_id: str) -> str | None:
    """唯讀取得命令狀態（僅證據用；讀取失敗回 None）。"""
    st = ta.run("cmd", "status", "--cmd-id", cmd_id)
    return (st.get("command") or {}).get("status")


def _body_inflight_abort(ctx: Any, ta: guards.ThrowawayDaemon, ta_com: str, board: dict[str, Any]) -> CaseResult:
    sub = ta.run("cmd", "submit", "--selector", ta_com, "--cmd", "sleep 20; echo X", "--cmd-timeout", "30")
    ctx.note("ta-submit.json", str(sub))
    cmd_id = sub.get("cmd_id")
    if not cmd_id:
        return CaseResult("FAIL", reason=f"submit 未回 cmd_id：{sub!r}", category="environment",
                          reason_code="submit_no_cmd_id")
    time.sleep(1.0)  # 確保命令已在途、TX 已寫出
    rel = ta.run("device", "release", "--selector", ta_com, "--source", "agent:swreg",
                 "--reason", "f10 inflight abort", timeout=60)
    ctx.note("ta-release.json", str(rel))
    if not rel.get("ok"):
        return CaseResult("FAIL", reason=f"throwaway device release 失敗：{rel!r}", category="environment",
                          reason_code="release_failed")
    time.sleep(2.0)
    status = ta.run("cmd", "status", "--cmd-id", str(cmd_id))
    status_text = json.dumps(status, ensure_ascii=False)
    ctx.note("ta-cmd-status.json", status_text)
    verdict = evaluate_inflight_abort(SimpleNamespace(
        cmd_id=str(cmd_id), wal_records=_load_wal_records(ta), cmd_status=status_text))
    return verdict


@_case(
    "f10-release-drain-receipt",
    "實板 console 持續 TX 時 device release 的 ok 回條只在 handle 關閉後送出",
    issues=(),
    pending_issue=True,
    hints=(
        "外部 reader 在 release 前即開始輪詢 open(tty)（TIOCEXCL 下非 root 會 EBUSY），第一次成功的時刻即 handle 關閉時刻。",
        "回條時間 ≤ 第一次成功 open 的時刻 → FAIL（回條早於 handle 關閉）。root 執行會繞過 TIOCEXCL，case 直接 SKIP。",
        "issues 欄待擁有者為 Stage 0 開立 issue 號後補上（不得填假號）。",
    ),
)
def f10_release_drain_receipt(ctx: Any) -> CaseResult:
    return _run_with_handoff(ctx, "f10-release-drain-receipt", _body_release_receipt)


@_case(
    "f10-release-aborts-inflight-command",
    "長命令在途時 device release 後 cmd status 為 COMMAND_ABORTED_IN_FLIGHT 且 TX 不重送",
    issues=(),
    pending_issue=True,
    hints=(
        "submit `sleep 20; echo X`（在途 TX 已寫出）後 release；oracle 以 WAL 同 cmd_id 的 TX 計數判定重送。",
        "issues 欄待擁有者為 Stage 0 開立 issue 號後補上（不得填假號）。",
    ),
)
def f10_release_aborts_inflight_command(ctx: Any) -> CaseResult:
    return _run_with_handoff(ctx, "f10-release-aborts-inflight-command", _body_inflight_abort)


__all__ = [
    "evaluate_release_receipt",
    "evaluate_inflight_abort",
    "f10_release_drain_receipt",
    "f10_release_aborts_inflight_command",
]
