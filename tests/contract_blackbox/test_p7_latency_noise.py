"""P7 Task 18（08-slice23-login-events.md）：延遲與背景雜訊黑箱。

`test_login_with_delay_and_noise`：每個回應延遲 800 ms＋每 50 ms 一行 kernel noise 下仍能登入到 READY。
雜訊行以 `\r\n` 起頭（printk 獨立成行）：Python 基線實測（2026-10-29）中，不換行直接接在 `login: ` 之後的雜訊
會使 prompt 行永遠比對不到、無法登入（500 ms 間隔、無延遲亦然），故該寫法不是 Python 的可通過情境（案例設計
修正、不改契約語意，記於 p7-fix-responses 第 11 輪）。
`test_control_latency_under_flood_with_events`（計畫同列）：OD-59（擁有者裁決 (a)，rulings/p5.md、p7.md）不新增
`control_latency_flood_p99_ms`，改以 `contracts/resource_limits.yaml` `event_latency` 的 flood 規則判定——
stop_request／command_cancel 取 `max_ms_flood`（100 ms）、`gate_rule: every_run_max_le_max_ms`，量測工具與口徑沿用
`scripts/rust/event_latency_measure.py`（`_Rpc.timed` 的「RPC 送出 → 回應」、`_Flood` 的 20000 B/s 負載、
`flood_problems` 的負載驗證、`judge_run` 的單輪判定與 loadavg 作廢規則）。差別只在負載期間另載一條比對每一行洪水
的規則（scope any、handler 執行中，後續比對被 `per_rule_busy` 丟棄），並斷言 `event_dropped` 有出現且筆數不超過送出
的洪水行數（有界）。daemon RSS 依 OD-09 不作 gate，本案不量。案例設計修正記於 `slice23-chg-register.md`
CHG-S23-CTRL-LATENCY-OD59-01。
stdlib＋PyYAML＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from bb_faketarget import FakeTarget
from bb_p7 import wait_state

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[2]
CANCEL_SAMPLES = 200        # 計畫 08 Task 18 該列「200 次取樣」
STOP_SAMPLES = 20           # stop_request 每樣本為一個新 daemon（event_latency 口徑），控制案例時長
VOID_RETRIES = 2            # 同 event_latency_measure.py 預設：loadavg 峰值 > CPU 數的輪次作廢重跑
FLOOD_MARK = "LATFLOOD"     # `_Flood` 每行開頭


def _measure_tool():
    spec = importlib.util.spec_from_file_location("event_latency_measure",
                                                  ROOT / "scripts" / "rust" / "event_latency_measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("event_latency_measure", mod)
    spec.loader.exec_module(mod)
    return mod


def _add_all_rule(d, tmp: Path) -> None:
    """比對每一行洪水的規則；handler 執行 0.2 s，期間的後續比對由 dispatcher 以 per_rule_busy 丟棄。"""
    rule = {"schema_version": 1, "owner": "p7", "name": "flood-all", "kind": "tool", "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": FLOOD_MARK}, "scope": "any",
            "handler": {"exec": [sys.executable, "-c", "import time; time.sleep(0.2)"]},
            "auto_enable_com_on_load": True, "timeout_ms": 3000}
    path = tmp / "flood-all.json"
    path.write_text(json.dumps(rule), encoding="utf-8")
    r = d.sw("event", "add", "--file", str(path))
    assert r["_rc"] == 0 and r.get("ok") is True, r


def _judged(tool, values, errors, load_problems, *, want, limit, before, monitor):
    return tool.judge_run(values, want=want, limit=limit, load_before=before, load_after=tool.host_load(),
                          errors=errors, load_problems=load_problems, host_samples=monitor.samples)


def _cancel_run(tool, harness, root: Path, limit: float, flood_cfg: dict) -> tuple[dict, dict]:
    """在途命令的 command.cancel（每輪 CANCEL_SAMPLES 筆）；整輪 DUT 持續洪水且 ALL 規則載入中。"""
    rate, chunk, seconds = int(flood_cfg["bytes_per_s"]), int(flood_cfg["chunk_bytes"]), float(flood_cfg["seconds"])
    values: list[float] = []
    errors: list[str] = []
    before = tool.host_load()
    monitor = tool._HostMonitor()
    with monitor:
        d = harness.start_daemon(root, login=False)
        rpc = tool._Rpc(d.endpoint)
        try:
            tool._wait_state(rpc, "READY")
            _add_all_rule(d, root)
            fl = tool._Flood(d.targets["COM0"], rate, chunk)
            with fl:
                for n in range(CANCEL_SAMPLES):
                    try:
                        values.append(tool._sample_cancel(rpc))
                    except Exception as exc:  # noqa: BLE001 — 逐筆記錄（judge_run 判證據不足），不中斷整輪
                        errors.append(f"command_cancel #{n}: {type(exc).__name__}: {exc}")
                        if len(errors) > 5:
                            break
                if fl.elapsed() < seconds:
                    fl.stop_evt.wait(seconds - fl.elapsed())       # 每輪 flood 至少持續 seconds
            stats = fl.stats()
            lines_sent = stats["sent"] // chunk
            entries = d.rpc("event.tail", {"n": 1000000}, timeout=30.0).get("entries") or []
        finally:
            rpc.close()
            d.stop()
    problems = tool.flood_problems(stats, bytes_per_s=rate, min_seconds=seconds)
    run = _judged(tool, values, errors, problems, want=CANCEL_SAMPLES, limit=limit, before=before, monitor=monitor)
    dropped = [e for e in entries if e.get("type") == "event_dropped"]
    events = {"dropped": len(dropped), "lines_sent": lines_sent,
              "dropped_reasons": sorted({str(e.get("reason")) for e in dropped}),
              "dropped_selectors": sorted({str(e.get("selector")) for e in dropped}),
              "matched": sum(e.get("type") == "match_recorded" for e in entries)}
    return run, events


def _stop_run(tool, harness, root: Path, limit: float, flood_cfg: dict) -> dict:
    """daemon.stop（每樣本一個新 daemon，READY＋ALL 規則載入後以 flood 暖機 stop_warmup_s 再送出）。"""
    rate, chunk, warmup = int(flood_cfg["bytes_per_s"]), int(flood_cfg["chunk_bytes"]), float(flood_cfg["stop_warmup_s"])
    values: list[float] = []
    errors: list[str] = []
    problems: list[str] = []
    before = tool.host_load()
    monitor = tool._HostMonitor()
    with monitor:
        for n in range(STOP_SAMPLES):
            sub = root / f"s{n % 2}"
            d = harness.start_daemon(sub, login=False)
            rpc = tool._Rpc(d.endpoint)
            try:
                tool._wait_state(rpc, "READY")
                _add_all_rule(d, sub)
                fl = tool._Flood(d.targets["COM0"], rate, chunk)
                with fl:
                    fl.stop_evt.wait(warmup)                # 暖機：daemon 已在持續 RX＋規則比對下才送 stop
                    ms, r = rpc.timed("daemon.stop", {})
                bad = tool.flood_problems(fl.stats(), bytes_per_s=rate, min_seconds=warmup)
                if bad:
                    problems.append(f"stop #{n}：{'；'.join(bad)}")
                if r.get("ok") is not True:
                    raise RuntimeError(f"daemon.stop 非 ok：{r}")
                values.append(ms)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"stop_request #{n}: {type(exc).__name__}: {exc}")
                if len(errors) > 5:
                    break
            finally:
                rpc.close()
                d.stop()
    return _judged(tool, values, errors, problems, want=STOP_SAMPLES, limit=limit, before=before, monitor=monitor)


def test_login_with_delay_and_noise(p7, tmp_path):
    tgt = FakeTarget(
        tmp_path / "t0",
        prompt="# ",
        password="pw-noise",
        delay_ms=800,
        noise={"interval_ms": 50, "pattern": "\r\n[  12.345678] kernel: usb 1-1: noise"},
        login_steps=[
            {"expect": "", "respond": "\r\nLogin: "},
            {"expect": "root", "respond": "\r\nPassword: "},
            {"expect": "pw-noise", "respond": "\r\nCM> "},
        ],
        commands={"overrides": {"sh": {"response": "\r\n# "}}},
    )
    p7.start("bcm", [tgt])                                     # bcm：timeout_s 15
    # 雜訊下單次 attach 可能失敗而進入 DETACHED↔ATTACHING 重試（backoff 上限 15 s）；全套負載下 90 s 曾不足
    # （第 11 輪 g2 全套 1 次，同範圍連跑 2 次與 4 份並行皆綠），上限取 180 s，斷言仍為最終 READY。
    s = wait_state(p7.env, "COM0", "READY", timeout_s=180.0)
    assert s["last_error"] is None, s
    sent = tgt.rx_log()
    assert b"root\n" in sent and b"pw-noise\n" in sent and b"sh\n" in sent and b"echo __READY__" in sent, sent


def test_control_latency_under_flood_with_events(tmp_path):
    """RX 洪水＋ALL 規則下的控制延遲（OD-59：event_latency flood 規則）。

    兩個外部量測事件各一輪：command_cancel（CANCEL_SAMPLES 筆、同一 daemon、整輪洪水）與 stop_request
    （STOP_SAMPLES 個新 daemon、各暖機 stop_warmup_s）。每輪由 `judge_run` 判定——有效樣本最大值 > `max_ms_flood`
    即 FAIL；錯誤、缺樣、負載無效即證據不足（不得 PASS）；loadavg 峰值 > CPU 數的輪次作廢重跑，重跑仍作廢即證據不足。
    另斷言洪水期間規則確有比對（match_recorded ≥ 1），且 `event_dropped` 出現、筆數 ≤ 送出的洪水行數、皆屬 COM0。"""
    tool = _measure_tool()
    harness = tool._import_harness()
    contract = tool.load_contract()
    assert contract.get("gate_rule") == "every_run_max_le_max_ms", contract.get("gate_rule")
    rows = {str(e.get("event")): e for e in contract.get("events") or []}
    flood_cfg = contract["flood_load"]
    verdicts: dict[str, dict] = {}
    events: dict | None = None
    for event in ("command_cancel", "stop_request"):
        assert rows[event].get("measure") == "external", rows[event]
        limit = rows[event]["max_ms_flood"]
        for attempt in range(VOID_RETRIES + 1):
            root = tmp_path / f"{event}-{attempt}"
            if event == "command_cancel":
                run, events = _cancel_run(tool, harness, root, limit, flood_cfg)
            else:
                run = _stop_run(tool, harness, root, limit, flood_cfg)
            if not (run["void"] and attempt < VOID_RETRIES):
                break
            time.sleep(5.0)
        verdicts[event] = run
    summary = {ev: {k: r.get(k) for k in ("verdict", "max_ms", "p99_ms", "count", "limit_ms", "void", "errors",
                                          "load_problems", "host_load")} for ev, r in verdicts.items()}
    print("control_latency_summary", json.dumps({"runs": summary, "events": events}, ensure_ascii=False, sort_keys=True))
    assert all(r["verdict"] == "PASS" for r in verdicts.values()), summary
    assert events is not None and events["matched"] >= 1, events
    assert 1 <= events["dropped"] <= events["lines_sent"], events
    assert events["dropped_selectors"] == ["COM0"], events
