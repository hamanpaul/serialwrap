"""F11 RX 洪水/傳輸層（#153 #150）。

實查依據：
- ``serialwrap session activity --selector <sel>``／``session list`` 的 session dict
  自 #153/#150 起新增 ``rx_bytes_last_10s``（近 10s raw RX bytes）、``rx_rate_bps``、
  ``last_rx_age_s``、``last_tx_age_s``、``last_error_detail`` 欄位。
- probe 失敗且 ``rx_bytes_last_10s >= 20000``（``RX_FLOOD_BYTES_PER_10S``）時，
  PROMPT_UNAVAILABLE／*_PROMPT_TIMEOUT 家族反分類為 ``RX_FLOOD``；self-test 於洪水中
  回 ``classification=RX_FLOOD``＋``recommended_action=wait``（而非 TARGET_UNRESPONSIVE
  ／ATTACHED_NOT_READY 誤導上層 recover/重建）。
- 排空後 daemon 於 RX 閒置（3s）自動重探升 READY——自癒本身即 #153 的回歸面。
- 真 transport stall（usbip URB -32）需 usbipd 人工操作，#155 已裁定排除於自動化；
  #150 heuristic 由 pytest（tests/test_transport_stall.py）覆蓋，實機側僅驗觀測面。
"""
from __future__ import annotations

import time

from realhw.harness import CaseResult

from ..harness import Case, register


def _case(id, title, issues, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, family="F11", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints)))
        return fn
    return deco


# 與 sw_core/constants.py 的 RX_FLOOD_BYTES_PER_10S 對齊（case 端獨立常數，
# 避免 import 受測物造成 pinned CLI 與 repo 版本混淆）。
_FLOOD_THRESHOLD = 20_000


def _rx_metric(session: dict) -> object:
    return session.get("rx_bytes_last_10s")


@_case("f11-last-rx-age-sane",
       "session 觀測面須含 last_rx_age_s/last_tx_age_s/last_error_detail 且值合理",
       issues=("#150",))
def f11_last_rx_age_sane(ctx):
    """#150 觀測面缺口不得再現：stall（RX 單邊凍結）過去在 session list 看不出來——
    idle_for_ms 取 max(rx,tx) 被 probe/human TX 拉小。新欄位 last_rx_age_s／last_tx_age_s
    ／last_error_detail 必須存在；剛完成一次 echo 往返後 last_rx_age_s 須為新鮮小值。
    """
    com0 = ctx.cfg["boards"][0]["com"]

    # 1. 鍵存在檢查（COM0）。
    r = ctx.sw.run("session", "activity", "--selector", com0)
    ctx.note(f"{com0}-activity-initial.json", str(r))
    session = r.get("session") or {}
    for key in ("last_rx_age_s", "last_tx_age_s", "last_error_detail"):
        if key not in session:
            return CaseResult(
                "FAIL",
                reason=f"{com0} session activity 缺 {key} 欄位（{sorted(session.keys())!r}，#150 回歸）",
                category="test", reason_code="missing_last_rx_age")

    # 2. 送一次 echo 往返（前置；隔拍讀 activity，line-race 慣例）。
    cmd = ctx.sw.submit_and_wait(com0, "echo ping")
    ctx.note("submit-echo-ping.json", str(cmd))
    if (cmd.get("status") or "") != "done":
        return CaseResult(
            "FAIL",
            reason=f"echo ping 未正常完成（status={cmd.get('status')!r}），無法驗證 age 新鮮度",
            category="environment", reason_code="submit_precondition_failed")
    time.sleep(1)

    # 3. 合理性 oracle：剛完成 RX 往返，age 須為非 null 的新鮮小值。
    r2 = ctx.sw.run("session", "activity", "--selector", com0)
    ctx.note(f"{com0}-activity-after-echo.json", str(r2))
    session2 = r2.get("session") or {}
    for key in ("last_rx_age_s", "last_tx_age_s"):
        val = session2.get(key)
        if not isinstance(val, (int, float)) or isinstance(val, bool) or not (0 <= val < 30):
            return CaseResult(
                "FAIL",
                reason=f"{com0} 剛完成 echo 往返但 {key}={val!r}（期望 0<=age<30，#150 回歸：RX/TX 年齡不可觀測）",
                category="test", reason_code="stale_last_rx_age")

    # 4. 第二塊板只做鍵存在檢查（唯讀，不送命令——foreground-busy 慣例）。
    if len(ctx.cfg["boards"]) > 1:
        com1 = ctx.cfg["boards"][1]["com"]
        r3 = ctx.sw.run("session", "activity", "--selector", com1)
        ctx.note(f"{com1}-activity.json", str(r3))
        session3 = r3.get("session") or {}
        for key in ("last_rx_age_s", "last_tx_age_s", "last_error_detail"):
            if key not in session3:
                return CaseResult(
                    "FAIL",
                    reason=f"{com1} session activity 缺 {key} 欄位（#150 回歸）",
                    category="test", reason_code="missing_last_rx_age")
    return CaseResult("PASS")


@_case("f11-flood-probe-classified",
       "console 洪水下 probe 失敗須分類為 RX_FLOOD（等排空），排空後自癒回 READY",
       issues=("#153",), destructive=True,
       hints=(
           "板端 echo loop 若被 shell buffer 整批吐出，probe 可能擠過縫隙判 OK——"
           "3 次皆 OK 記 SKIP（flood_probe_survived，不可判定、非回歸）。",
           "跑本 case 前 testpilot venv 的 serialwrap client 須 >=0.2.4"
           "（#154 preflight 版本 gate 已強制 pinned CLI＝daemon 版本）。",
       ))
def f11_flood_probe_classified(ctx):
    """#153：console 被灌爆時 probe 失敗曾被誤分類為 PROMPT_UNAVAILABLE／
    TARGET_UNRESPONSIVE（「灌爆」與「死了」擠同一碼），上層被誤導去重建 session
    而非等排空。oracle：洪水中 self-test/attach 不得再回誤導碼、RX 指標非零、
    排空後自癒回 READY。只動 COM0（prpl），COM1 不碰。
    """
    com = ctx.cfg["boards"][0]["com"]
    result: CaseResult | None = None
    try:
        # 1. 前置：session 須 READY。
        sess = ctx.sw.session(com)
        ctx.note("precondition-session.json", str(sess))
        if sess.get("state") != "READY":
            result = CaseResult(
                "SKIP",
                reason=f"{com} 非 READY（state={sess.get('state')!r}），無法造洪水",
                category="environment", reason_code="board_not_ready")
            return result

        # 2. 基線：session dict 須含 rx_bytes_last_10s 鍵。
        if "rx_bytes_last_10s" not in sess:
            result = CaseResult(
                "FAIL",
                reason=f"{com} session dict 缺 rx_bytes_last_10s（{sorted(sess.keys())!r}，#153 回歸：RX 指標未露出）",
                category="test", reason_code="rx_metric_missing")
            return result

        # 3. 造有界洪水：30000 行×~14B≈420KB，115200 線速需 ~35-40s 排空；
        #    loop 背景化（&）後 prompt 立返。submit done/error 皆接受（洪水已啟動）。
        flood = ctx.sw.run(
            "cmd", "submit", "--selector", com,
            "--cmd", "for i in $(seq 1 30000); do echo FLOOD_$i; done &",
            "--cmd-timeout", "8", timeout=30.0)
        ctx.note("flood-submit.json", str(flood))

        # 4. 確認洪水建立：<=10s 內 rx_bytes_last_10s 達閾值。
        established = False
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            metric = _rx_metric(ctx.sw.session(com))
            if isinstance(metric, (int, float)) and metric >= _FLOOD_THRESHOLD:
                established = True
                break
            time.sleep(1.0)
        ctx.note("flood-established-session.json", str(ctx.sw.session(com)))
        if not established:
            result = CaseResult(
                "SKIP",
                reason=f"{com} 10s 內 rx_bytes_last_10s 未達 {_FLOOD_THRESHOLD}（板端 echo 過慢屬能力缺失非回歸）",
                category="environment", reason_code="flood_not_established")
            return result

        # 5. 洪水中 self-test（<=3 次、間隔 2s）。oracle O1：誤導碼不得再現。
        ok_count = 0
        for attempt in range(3):
            st = ctx.sw.run("session", "self-test", "--selector", com, timeout=60.0)
            ctx.note(f"selftest-during-flood-{attempt}.json", str(st))
            cls = st.get("classification")
            metric = _rx_metric(ctx.sw.session(com))
            over = isinstance(metric, (int, float)) and metric >= _FLOOD_THRESHOLD
            if cls in ("TARGET_UNRESPONSIVE", "ATTACHED_NOT_READY") and over:
                result = CaseResult(
                    "FAIL",
                    reason=f"洪水中（rx_bytes_last_10s={metric!r}）self-test 仍判 {cls}（#153 回歸：洪水與死線擠同一碼）",
                    category="test", reason_code="flood_misclassified")
                return result
            if cls == "RX_FLOOD":
                break
            if cls == "OK":
                ok_count += 1
            time.sleep(2.0)
        else:
            if ok_count == 3:
                result = CaseResult(
                    "SKIP",
                    reason="3 次 self-test 皆 OK（probe 從洪水縫隙擠過），本輪不可判定",
                    category="environment", reason_code="flood_probe_survived")
                return result

        # 6. 洪水中 attach。oracle O1'：PROMPT_UNAVAILABLE＋超閾＝誤分類；O3：指標非零。
        att = ctx.sw.run("session", "attach", "--selector", com, timeout=60.0)
        ctx.note("attach-during-flood.json", str(att))
        att_session = att.get("session") or {}
        metric = _rx_metric(att_session)
        if metric is None or metric == 0:
            metric = _rx_metric(ctx.sw.session(com))
        over = isinstance(metric, (int, float)) and metric >= _FLOOD_THRESHOLD
        code = str(att.get("error_code") or "")
        if code == "PROMPT_UNAVAILABLE" and over:
            result = CaseResult(
                "FAIL",
                reason=f"洪水中（rx_bytes_last_10s={metric!r}）attach 仍回 PROMPT_UNAVAILABLE（#153 回歸）",
                category="test", reason_code="flood_misclassified")
            return result
        if not (isinstance(metric, (int, float)) and metric > 0):
            result = CaseResult(
                "FAIL",
                reason=f"洪水進行中 rx_bytes_last_10s={metric!r}（期望非零，#153 回歸：RX 指標不誠實）",
                category="test", reason_code="rx_metric_zero_during_flood")
            return result

        # 7. 排空：輪詢指標退回閾值下（洪水有界 ~40s，deadline 120s）。
        drain_deadline = time.monotonic() + 120.0
        while time.monotonic() < drain_deadline:
            metric = _rx_metric(ctx.sw.session(com))
            if isinstance(metric, (int, float)) and metric < _FLOOD_THRESHOLD:
                break
            time.sleep(2.0)
        ctx.note("post-drain-session.json", str(ctx.sw.session(com)))
        result = CaseResult("PASS")
        return result
    finally:
        # 8. 收尾還原（oracle O4）：排空後自癒回 READY 本身就是 #153 的回歸面。
        ready_timeout_s = float(ctx.cfg["timeouts"]["ready_wait_s"])
        ready = ctx.sw.wait_state(com, "READY", timeout_s=ready_timeout_s)
        if not ready:
            rec = ctx.sw.run("session", "recover", "--selector", com, timeout=90.0)
            ctx.note("recover-after-drain.json", str(rec))
            ready = ctx.sw.wait_state(com, "READY", timeout_s=ready_timeout_s)
        # belt-and-braces：清可能殘留的背景 loop。
        if ready:
            kill = ctx.sw.submit_and_wait(com, "kill %% 2>/dev/null; :")
            ctx.note("kill-leftover-loop.json", str(kill))
        ctx.note("final-session.json", str(ctx.sw.session(com)))
        # 僅覆蓋 PASS（仿 f4-interactive 收尾慣例）：既有 FAIL 保留較具體的原因。
        if not ready and result is not None and result.verdict == "PASS":
            result.verdict = "FAIL"
            result.category = "test"
            result.reason_code = "not_ready_after_drain"
            result.reason = f"{com} 排空後未自癒回 READY（含 recover 補救仍未回，#153 回歸：洪水後不自癒）"
