"""P9-T7B 差分工具的完整性、實例根目錄與數值比對規則測試。

本檔以獨立的最小報告樹替身建立資料（不依賴 `fakes_suites.ab_env`，因此不需要 P9-T3 的
`ab_manifest.load_manifest`），補足 `tests/test_stage3_ab_diff.py` 未覆蓋的失敗模式。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.stage3 import ab_diff
from tools.stage3.reason_codes import HarnessError


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "contracts" / "ab_normalization.yaml"
CASE = "case-alpha"


def _dump(path: Path, document: Any) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _call(argv: list[str], payload: Any, *, rc: int = 0) -> dict[str, Any]:
    """與 func-test `cli_run` 寫入 cli.ndjson 的紀錄形狀一致（argv、rc、stdout、response）。"""
    return {
        "argv": argv,
        "mono_start_ns": 1,
        "rc": rc,
        "stderr": "",
        "stdout": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "response": payload,
    }


def _default_calls() -> list[dict[str, Any]]:
    return [_call(["session", "list"], {"ok": True, "sessions": []})]


def _passing_comparison(case: str = CASE) -> dict[str, Any]:
    return {
        "schema_version": "comparison.v1",
        "run_id": f"{case}-1",
        "case_id": case,
        "verdict": "PASS",
        "reasons": [],
        "invariants": [{"id": "zero_tx_on_reject", "verdict": "PASS", "detail": "ok"}],
    }


def _write_run(
    report_dir: Path,
    *,
    calls: list[dict[str, Any]] | None = None,
    comparison: Any = None,
    bridge: Any = None,
    daemon_side: str | None = None,
    write_cli: bool = True,
    case: str = CASE,
    index: str = "1",
) -> Path:
    run = report_dir / case / "bridge" / index
    run.mkdir(parents=True, exist_ok=True)
    _dump(run / "manifest.json", {
        "schema_version": "l15-manifest.v1",
        "case_id": case,
        "mode": "bridge",
        "transport": {"mode": "virtual_pty", "port_id": "uart0",
                      "daemon_side": daemon_side, "bridge_side": daemon_side},
    })
    _dump(run / "comparison.json", comparison if comparison is not None else _passing_comparison(case))
    _dump(run / "bridge-manifest.json", bridge if bridge is not None else {"closed_cleanly": True, "failure": None})
    if write_cli:
        records = calls if calls is not None else _default_calls()
        with (run / "cli.ndjson").open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return run


def _diff(a_dir: Path, b_dir: Path, *, required: set[str] | None = None, rules_path: Path = RULES_PATH,
          case_changes: dict[str, str] | None = None) -> dict[str, Any]:
    return ab_diff.diff_l15(
        a_dir,
        b_dir,
        registered={"SC-15": "absolute_only"},
        case_changes=case_changes or {},
        required_case_ids={CASE} if required is None else required,
        rules_path=rules_path,
    )


def _sides(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "reports" / "A", tmp_path / "reports" / "B"


def _pid_test_rules() -> dict[str, Any]:
    """直接以正規化器驗證 PID 佔位；foreign_holders 不在 volatile_keys 中，仍逐一參與 PID 佔位。"""
    return ab_diff.load_rules(RULES_PATH)


def test_pid_identity_survives_normalized_tty_key_collision(tmp_path):
    """兩個 PTY key 取代成同一鍵後，兩個 PID 仍須與重複同一 PID 不同。"""
    rules = _pid_test_rules()
    distinct = ab_diff.normalize_response(
        {"foreign_holders": {"/dev/pts/20": 101, "/dev/pts/24": 202}},
        rules=rules,
        instance_root=str(tmp_path / "instance"),
    )
    repeated = ab_diff.normalize_response(
        {"foreign_holders": {"/dev/pts/24": 101, "/dev/pts/20": 101}},
        rules=rules,
        instance_root=str(tmp_path / "instance"),
    )

    assert distinct != repeated


def test_pid_aliases_are_invariant_to_runtime_values_and_list_order(tmp_path):
    """PID 數值、dict 插入次序與 pids 陣列次序改變時，相同 PID 關係仍須正規化相同。"""
    rules = _pid_test_rules()
    a = {
        "pid": 101,
        "pids": [101, 202],
        "foreign_holders": {"COM0": 101, "COM1": 202},
    }
    b = {
        "foreign_holders": {"COM1": 404, "COM0": 303},
        "pids": [404, 303],
        "pid": 303,
    }

    normalized_a = ab_diff.normalize_response(a, rules=rules, instance_root=str(tmp_path / "instance"))
    normalized_b = ab_diff.normalize_response(b, rules=rules, instance_root=str(tmp_path / "instance"))

    assert normalized_a == normalized_b


def test_pid_labels_are_canonical_under_tty_key_collision(tmp_path):
    """同簽章 PID 因 PTY 鍵碰撞合併時，相同持有者關聯（兩人同群一人獨立）與 PTY 編號不同，正規化結果須相同。"""
    rules = _pid_test_rules()
    a = {"foreign_holders": {
        "/dev/pts/1": {"device": "/dev/pts/1", "holder_unknown": True, "pids": [101, 202]},
        "/dev/pts/2": {"device": "/dev/pts/2", "holder_unknown": True, "pids": [303]},
    }}
    b = {"foreign_holders": {
        "/dev/pts/1": {"device": "/dev/pts/1", "holder_unknown": True, "pids": [9001]},
        "/dev/pts/2": {"device": "/dev/pts/2", "holder_unknown": True, "pids": [9002, 9003]},
    }}

    normalized_a = ab_diff.normalize_response(a, rules=rules, instance_root=str(tmp_path / "instance"))
    normalized_b = ab_diff.normalize_response(b, rules=rules, instance_root=str(tmp_path / "instance"))

    assert normalized_a == normalized_b


def test_identical_fixture_is_ok(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir)
    _write_run(b_dir)

    result = _diff(a_dir, b_dir)

    assert result["ok"] is True
    assert result["integrity"] == {"a": True, "b": True}
    assert result["differences"] == []
    assert result["safety_violations"] == []


def test_report_dir_path_in_stdout_is_normalized(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, calls=[_call(["session", "list"], {"state_path": f"{a_dir}/instance/state.json"})])
    _write_run(b_dir, calls=[_call(["session", "list"], {"state_path": f"{b_dir}/instance/state.json"})])

    result = _diff(a_dir, b_dir)

    assert result["ok"] is True
    assert result["differences"] == []


def test_instance_root_outside_report_dir_is_normalized(tmp_path):
    """func-test 實例根目錄位於 `sw-func-*` 暫存目錄，不在報告目錄之下，仍須由 run 自身證據推得並正規化。"""
    a_dir, b_dir = _sides(tmp_path)
    inst_a = tmp_path / "instances" / "sw-func-AAAA"
    inst_b = tmp_path / "instances" / "sw-func-BBBB"
    _write_run(
        a_dir,
        calls=[_call(
            ["serialwrap", "--socket", f"{inst_a}/run/serialwrapd.sock", "session", "list"],
            {"ok": True, "device_by_id": f"{inst_a}/by-id/fake-uart0",
             "mirror_path": f"{inst_a}/xdg-state/serialwrap/wal/raw.mirror.log"},
        )],
        daemon_side=f"{inst_a}/by-id/fake-uart0",
    )
    _write_run(
        b_dir,
        calls=[_call(
            ["serialwrap", "--socket", f"{inst_b}/run/serialwrapd.sock", "session", "list"],
            {"ok": True, "device_by_id": f"{inst_b}/by-id/fake-uart0",
             "mirror_path": f"{inst_b}/xdg-state/serialwrap/wal/raw.mirror.log"},
        )],
        daemon_side=f"{inst_b}/by-id/fake-uart0",
    )

    result = _diff(a_dir, b_dir)

    assert result["ok"] is True
    assert result["differences"] == []


def test_unregistered_absolute_path_still_differs(tmp_path):
    """對照組：路徑不屬於任何 run 證據的根目錄，不得被視為實例根目錄而遮蔽差異。"""
    a_dir, b_dir = _sides(tmp_path)
    inst_a = tmp_path / "instances" / "sw-func-AAAA"
    _write_run(
        a_dir,
        calls=[_call(["serialwrap", "--socket", f"{inst_a}/run/serialwrapd.sock", "session", "list"],
                     {"ok": True, "device_by_id": f"{inst_a}/by-id/fake-uart0"})],
        daemon_side=f"{inst_a}/by-id/fake-uart0",
    )
    _write_run(
        b_dir,
        calls=[_call(["serialwrap", "--socket", str(tmp_path / "instances" / "sw-func-BBBB" / "run" / "serialwrapd.sock"),
                      "session", "list"],
                     {"ok": True, "device_by_id": "/tmp/elsewhere/by-id/fake-uart0"})],
        daemon_side=str(tmp_path / "instances" / "sw-func-BBBB" / "by-id" / "fake-uart0"),
    )

    result = _diff(a_dir, b_dir)

    assert result["ok"] is False
    assert {"case": CASE, "path": "call[0].device_by_id", "kind": "value", "registered_change": None} in result[
        "differences"]


@pytest.mark.parametrize(
    "comparison",
    [{}, {"verdict": None, "invariants": []}, {"verdict": "ERROR", "invariants": []},
     {"verdict": "unknown", "invariants": []}, {"verdict": "INSUFFICIENT_EVIDENCE", "invariants": []}],
    ids=["missing", "null", "error", "unknown", "insufficient"],
)
def test_unknown_or_missing_verdict_breaks_integrity(tmp_path, comparison):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, comparison=comparison)
    _write_run(b_dir)

    result = _diff(a_dir, b_dir)

    assert result["integrity"]["a"] is False
    assert result["integrity"]["b"] is True
    assert result["ok"] is False


def test_unknown_verdict_on_both_sides_is_not_ok(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, comparison={})
    _write_run(b_dir, comparison={})

    result = _diff(a_dir, b_dir)

    assert result["integrity"] == {"a": False, "b": False}
    assert result["ok"] is False


@pytest.mark.parametrize(
    "invariants",
    [
        [{"id": "zero_tx_on_reject", "verdict": "INSUFFICIENT_EVIDENCE", "detail": "x"}],
        [{"id": "zero_tx_on_reject", "detail": "x"}],
        [{"id": "zero_tx_on_reject", "verdict": "UNKNOWN", "detail": "x"}],
    ],
    ids=["insufficient", "missing", "unknown"],
)
def test_undecided_invariant_under_pass_breaks_integrity(tmp_path, invariants):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, comparison={"verdict": "PASS", "invariants": invariants})
    _write_run(b_dir)

    result = _diff(a_dir, b_dir)

    assert result["integrity"]["a"] is False
    assert result["safety_violations"] == []
    assert result["ok"] is False


def test_invariant_fail_on_both_sides_is_safety_violation(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    failed = {"verdict": "FAIL", "invariants": [{"id": "zero_tx_on_reject", "verdict": "FAIL", "detail": "x"}]}
    _write_run(a_dir, comparison=failed)
    _write_run(b_dir, comparison=failed)

    result = _diff(a_dir, b_dir)

    assert f"A:{CASE}:zero_tx_on_reject" in result["safety_violations"]
    assert f"B:{CASE}:zero_tx_on_reject" in result["safety_violations"]
    assert result["ok"] is False


def test_bridge_failure_breaks_integrity_even_when_closed(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, bridge={"closed_cleanly": True, "failure": "wire mismatch"})
    _write_run(b_dir)

    result = _diff(a_dir, b_dir)

    assert result["integrity"]["a"] is False
    assert result["ok"] is False


def test_missing_cli_record_on_both_sides_is_not_ok(tmp_path):
    """兩側 cli.ndjson 皆遺失時不得被當成零次呼叫而通過。"""
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, write_cli=False)
    _write_run(b_dir, write_cli=False)

    result = _diff(a_dir, b_dir)

    assert result["integrity"] == {"a": False, "b": False}
    assert result["ok"] is False
    assert any("cli.ndjson" in problem for problem in result["integrity_problems"]["a"])


def test_cli_record_without_rc_breaks_integrity(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    record = _call(["session", "list"], {"ok": True})
    del record["rc"]
    _write_run(a_dir, calls=[record])
    _write_run(b_dir)

    result = _diff(a_dir, b_dir)

    assert result["integrity"]["a"] is False
    assert result["ok"] is False


def test_truncated_cli_record_breaks_integrity(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    run = _write_run(a_dir)
    (run / "cli.ndjson").write_text('{"argv":["session","list"],"rc":0,"stdo', encoding="utf-8")
    _write_run(b_dir)

    result = _diff(a_dir, b_dir)

    assert result["integrity"]["a"] is False
    assert result["ok"] is False


def test_empty_required_case_set_is_not_ok(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir)
    _write_run(b_dir)

    result = _diff(a_dir, b_dir, required=set())

    assert result["integrity"] == {"a": False, "b": False}
    assert result["ok"] is False


def test_broken_rules_file_maps_to_comparator_error(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir)
    _write_run(b_dir)
    broken = tmp_path / "broken.yaml"
    broken.write_text("volatile_keys: [\n", encoding="utf-8")

    with pytest.raises(HarnessError) as info:
        _diff(a_dir, b_dir, rules_path=broken)

    assert info.value.code == "HARNESS_COMPARATOR_ERROR"


def test_non_mapping_rules_file_maps_to_comparator_error(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir)
    _write_run(b_dir)
    rules = tmp_path / "list.yaml"
    rules.write_text("- a\n- b\n", encoding="utf-8")

    with pytest.raises(HarnessError) as info:
        _diff(a_dir, b_dir, rules_path=rules)

    assert info.value.code == "HARNESS_COMPARATOR_ERROR"


def test_unicode_line_separator_inside_record_is_not_a_line_break(tmp_path):
    """cli.ndjson 以 `\\n` 分隔；JSON 字串內未跳脫的 U+2028 不得被當成換行，否則單筆紀錄會被誤判為損毀。"""
    a_dir, b_dir = _sides(tmp_path)
    payload = {"ok": True, "note": "a b"}
    _write_run(a_dir, calls=[_call(["session", "list"], payload)])
    _write_run(b_dir, calls=[_call(["session", "list"], payload)])

    result = _diff(a_dir, b_dir)

    assert result["integrity"] == {"a": True, "b": True}
    assert result["ok"] is True


def test_integer_and_float_are_different_values(tmp_path):
    """arch §9.3：不得寬鬆比對，1 與 1.0 的 JSON 文字不同即為差異。"""
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, calls=[_call(["session", "list"], {"n": 1})])
    _write_run(b_dir, calls=[_call(["session", "list"], {"n": 1.0})])

    result = _diff(a_dir, b_dir)

    assert result["ok"] is False
    assert {"case": CASE, "path": "call[0].n", "kind": "value", "registered_change": None} in result["differences"]


def test_nan_on_both_sides_is_equal(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    nan_record = {"argv": ["session", "list"], "mono_start_ns": 1, "rc": 0, "stderr": "",
                  "stdout": '{"x":NaN}', "response": {}}
    _write_run(a_dir, calls=[nan_record])
    _write_run(b_dir, calls=[dict(nan_record)])

    result = _diff(a_dir, b_dir)

    assert result["ok"] is True
    assert result["differences"] == []


# ---------------------------------------------------------------------------
# 審查修正（P9-T7B review fix）
# ---------------------------------------------------------------------------


def _real_shape_calls(
    inst: Path,
    *,
    polls: int,
    holder_path: str,
    cmd_id: str,
    seq: int,
    command_capable: bool = True,
    endpoint: list[str] | None = None,
) -> list[dict[str, Any]]:
    """依 261021-* 真實 L1.5 報告的欄位形狀合成最小呼叫序列，不複製主機路徑、PID 或真實時戳。

    對應真實報告的欄位：daemon status 的 foreign_holders／multi_open_detail／wal（host 觀測與時序快照）、
    session list 的 attached_real_path（PTY）與 idle_for_ms／last_rx_age_s（時序）、
    cmd submit／cmd status 的 cmd_id 與 accepted_at。輪詢次數 polls 模擬兩側 session list 次數不同。
    """
    link = list(endpoint) if endpoint is not None else ["--socket", str(inst / "run" / "serialwrapd.sock")]
    wal_dir = str(inst / "wal")
    device = str(inst / "by-id" / "fake-uart0")

    def argv(*tokens: str) -> list[str]:
        return ["serialwrap", *link, *tokens]

    status = {
        "ok": True,
        "pid": 4100 + seq,
        "running": True,
        "wal": {"current_seq": seq, "healthy": True, "wal_dir": wal_dir},
        "mirror_path": f"{wal_dir}/raw.mirror.log",
        "multi_open": True,
        "multi_open_detail": {"daemons": [{"pid": 900 + seq, "socket": "/run/other.sock"}]},
        "foreign_holders": {holder_path: {"device": holder_path, "holder_unknown": True, "pids": [4100 + seq]}},
    }
    session = {
        "act_no": 1,
        "activity_classification": "active",
        "alias": "func-test",
        "attached_real_path": holder_path,
        "com": "COM0",
        "command_capable": command_capable,
        "console_count": 1,
        "device_by_id": device,
        "idle_for_ms": 80 + seq,
        "last_rx_age_s": 0.1 + seq,
        "last_ready_at": f"2026-10-21T12:00:{seq:02d}+00:00",
    }
    listing = {"ok": True, "id": 1, "sessions": [session]}
    submitted = {
        "ok": True,
        "command": {
            "accepted_at": f"2026-10-21T12:00:{seq:02d}+00:00",
            "cmd_id": cmd_id,
            "done_at": None,
            "error_code": None,
            "status": "done",
        },
    }
    calls = [_call(argv("daemon", "status"), status)]
    calls += [_call(argv("session", "list"), listing) for _ in range(polls)]
    calls += [
        _call(argv("cmd", "submit", "--selector", "COM0", "--cmd", "echo L15"), submitted),
        _call(argv("device", "release", "--selector", "COM0"), {"ok": True, "released": True}),
        _call(argv("cmd", "status", "--cmd-id", cmd_id), submitted),
        _call(argv("device", "attach", "--selector", "COM0"), {"ok": True}),
    ]
    calls += [_call(argv("session", "list"), listing) for _ in range(2)]
    calls += [_call(argv("daemon", "stop"), {"ok": True, "stopping": True})]
    return calls


def test_real_shape_aa_pair_is_ok_across_timing_noise(tmp_path):
    """A/A：同一程式的兩次真實形狀報告，僅差時序雜訊、PTY、PID、cmd_id、根目錄與輪詢次數，必須 ok。"""
    a_dir, b_dir = _sides(tmp_path)
    inst_a = tmp_path / "instances" / "sw-func-AAAA"
    inst_b = tmp_path / "instances" / "sw-func-BBBB"
    _write_run(a_dir, calls=_real_shape_calls(inst_a, polls=10, holder_path="/dev/pts/20", cmd_id="a" * 32, seq=5),
               daemon_side=str(inst_a / "by-id" / "fake-uart0"))
    _write_run(b_dir, calls=_real_shape_calls(inst_b, polls=11, holder_path="/dev/pts/24", cmd_id="b" * 32, seq=4),
               daemon_side=str(inst_b / "by-id" / "fake-uart0"))

    result = _diff(a_dir, b_dir)

    assert result["integrity"] == {"a": True, "b": True}
    assert result["differences"] == []
    assert result["ok"] is True


def test_real_shape_non_volatile_change_still_differs(tmp_path):
    """對照：A/A 的正規化不得吞掉非 volatile 欄位（command_capable）的真實改變。"""
    a_dir, b_dir = _sides(tmp_path)
    inst_a = tmp_path / "instances" / "sw-func-AAAA"
    inst_b = tmp_path / "instances" / "sw-func-BBBB"
    _write_run(a_dir, calls=_real_shape_calls(inst_a, polls=10, holder_path="/dev/pts/20", cmd_id="a" * 32, seq=5),
               daemon_side=str(inst_a / "by-id" / "fake-uart0"))
    _write_run(b_dir, calls=_real_shape_calls(inst_b, polls=10, holder_path="/dev/pts/24", cmd_id="b" * 32, seq=4,
                                              command_capable=False),
               daemon_side=str(inst_b / "by-id" / "fake-uart0"))

    result = _diff(a_dir, b_dir)

    assert result["ok"] is False
    assert any(item["path"].endswith(".sessions.0.command_capable") and item["kind"] == "value"
               for item in result["differences"])


def test_windows_style_run_derives_root_from_wal_paths(tmp_path):
    """Windows：endpoint 為 tcp://、daemon_side 為 COM 裝置名，實例根目錄只能由 daemon status 的 wal 路徑推得。"""
    a_dir, b_dir = _sides(tmp_path)
    inst_a = tmp_path / "instances" / "sw-func-AAAA"
    inst_b = tmp_path / "instances" / "sw-func-BBBB"
    endpoint = ["--endpoint", "tcp://127.0.0.1:41001"]
    _write_run(a_dir, calls=_real_shape_calls(inst_a, polls=3, holder_path="/dev/pts/20", cmd_id="a" * 32, seq=5,
                                              endpoint=endpoint),
               daemon_side="COM20")
    _write_run(b_dir, calls=_real_shape_calls(inst_b, polls=3, holder_path="/dev/pts/24", cmd_id="b" * 32, seq=4,
                                              endpoint=endpoint),
               daemon_side="COM20")

    result = _diff(a_dir, b_dir)

    assert result["differences"] == []
    assert result["ok"] is True


def test_swapped_cli_order_with_identical_stdout_is_argv_difference(tmp_path):
    """對調 device release／attach 而 stdout 相同，必須以 argv 差異攔下。"""
    a_dir, b_dir = _sides(tmp_path)
    release = _call(["serialwrap", "device", "release", "--selector", "COM0"], {"ok": True})
    attach = _call(["serialwrap", "device", "attach", "--selector", "COM0"], {"ok": True})
    _write_run(a_dir, calls=[release, attach])
    _write_run(b_dir, calls=[attach, release])

    result = _diff(a_dir, b_dir)

    assert result["ok"] is False
    assert {"case": CASE, "path": "call[0].argv", "kind": "argv", "registered_change": None} in result["differences"]


def test_pty_paths_in_values_and_keys_are_normalized(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, calls=[_call(["session", "list"], {"ok": True, "ptys": {"/dev/pts/20": 1},
                                                          "vtty": "/dev/pts/3"})])
    _write_run(b_dir, calls=[_call(["session", "list"], {"ok": True, "ptys": {"/dev/pts/24": 1},
                                                          "vtty": "/dev/pts/9"})])

    result = _diff(a_dir, b_dir)

    assert result["differences"] == []
    assert result["ok"] is True


def test_pty_holder_count_difference_is_not_hidden_by_key_rename(tmp_path):
    """兩個 PTY 持有者改名後鍵會碰撞；碰撞時必須保留數量差異，不得靜默吞掉。"""
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, calls=[_call(["session", "list"], {"ok": True, "ptys": {"/dev/pts/1": 1, "/dev/pts/2": 1}})])
    _write_run(b_dir, calls=[_call(["session", "list"], {"ok": True, "ptys": {"/dev/pts/1": 1}})])

    result = _diff(a_dir, b_dir)

    assert result["ok"] is False
    assert any(item["kind"] == "value" and item["path"].endswith(".ptys.<PTY>") for item in result["differences"])


def test_instance_root_replacement_respects_path_boundary(tmp_path):
    """根目錄 `sw-func-AAAA` 不得誤中 `sw-func-AAAAx`（前後邊界）；否則兩側會被同時取代成相同字串而漏報。"""
    a_dir, b_dir = _sides(tmp_path)
    inst_a = tmp_path / "instances" / "sw-func-AAAA"
    inst_b = tmp_path / "instances" / "sw-func-BBBB"
    socket_a = str(inst_a / "run" / "serialwrapd.sock")
    socket_b = str(inst_b / "run" / "serialwrapd.sock")
    _write_run(a_dir, calls=[_call(["serialwrap", "--socket", socket_a, "session", "list"],
                                   {"ok": True, "note": f"{inst_a}x/state"})],
               daemon_side=str(inst_a / "by-id" / "fake-uart0"))
    _write_run(b_dir, calls=[_call(["serialwrap", "--socket", socket_b, "session", "list"],
                                   {"ok": True, "note": f"{inst_b}x/state"})],
               daemon_side=str(inst_b / "by-id" / "fake-uart0"))

    result = _diff(a_dir, b_dir)

    assert result["ok"] is False
    assert {"case": CASE, "path": "call[0].note", "kind": "value", "registered_change": None} in result["differences"]


def test_relative_report_dirs_resolve_before_substitution(tmp_path, monkeypatch):
    """相對目錄 a、b 不得以未解析字串取代：xa 與 xb 必須為差異，內容相同的 banana 必須相等。"""
    monkeypatch.chdir(tmp_path)
    a_dir, b_dir = Path("a"), Path("b")
    _write_run(a_dir, calls=[_call(["session", "list"], {"mode": "xa", "word": "banana"})])
    _write_run(b_dir, calls=[_call(["session", "list"], {"mode": "xb", "word": "banana"})])

    result = _diff(a_dir, b_dir)

    assert result["ok"] is False
    assert {"case": CASE, "path": "call[0].mode", "kind": "value", "registered_change": None} in result["differences"]
    assert all(item["path"] != "call[0].word" for item in result["differences"])


def test_identical_report_dirs_are_refused(tmp_path):
    a_dir, _ = _sides(tmp_path)
    _write_run(a_dir)

    with pytest.raises(HarnessError) as info:
        _diff(a_dir, a_dir)

    assert info.value.code == "HARNESS_COMPARATOR_ERROR"


def test_nested_report_dirs_are_refused(tmp_path):
    outer = tmp_path / "reports"
    inner = outer / "B"
    _write_run(outer / "A")
    _write_run(inner)

    with pytest.raises(HarnessError) as info:
        _diff(outer, inner)

    assert info.value.code == "HARNESS_COMPARATOR_ERROR"


@pytest.mark.parametrize("verdict", [["PASS"], {"v": "PASS"}], ids=["list", "dict"])
def test_non_string_verdict_is_integrity_problem_not_crash(tmp_path, verdict):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, comparison={"verdict": verdict, "invariants": []})
    _write_run(b_dir)

    result = _diff(a_dir, b_dir)

    assert result["integrity"]["a"] is False
    assert result["ok"] is False


@pytest.mark.parametrize("invariant_verdict", [["FAIL"], {"v": 1}], ids=["list", "dict"])
def test_non_string_invariant_verdict_is_integrity_problem_not_crash(tmp_path, invariant_verdict):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, comparison={"verdict": "PASS",
                                  "invariants": [{"id": "zero_tx_on_reject", "verdict": invariant_verdict}]})
    _write_run(b_dir)

    result = _diff(a_dir, b_dir)

    assert result["integrity"]["a"] is False
    assert result["ok"] is False


def test_unicode_digit_run_name_does_not_crash(tmp_path):
    """`²` 通過 str.isdigit() 但不能 int()；排序鍵不得拋出 ValueError。"""
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir)
    _write_run(a_dir, index="²")
    _write_run(b_dir)

    result = _diff(a_dir, b_dir)

    assert result["integrity"]["b"] is False


def test_cli_timeout_record_is_integrity_problem_on_both_sides(tmp_path):
    """func-test 逾時寫入 rc: null 與 CLI_TIMEOUT；兩側同時逾時不得判為 ok。"""
    timeout = {"argv": ["session", "list"], "mono_start_ns": 1, "rc": None, "stderr": "", "stdout": "",
               "response": {"ok": False, "error_code": "CLI_TIMEOUT"}}
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, calls=[dict(timeout)])
    _write_run(b_dir, calls=[dict(timeout)])

    result = _diff(a_dir, b_dir)

    assert result["integrity"] == {"a": False, "b": False}
    assert result["ok"] is False


def test_comparison_verdict_difference_is_reported_as_verdict(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, comparison={"verdict": "FAIL", "invariants": []})
    _write_run(b_dir)

    result = _diff(a_dir, b_dir)

    assert {"case": CASE, "path": "comparison.verdict", "kind": "verdict", "registered_change": None} in (
        result["differences"])
    assert result["ok"] is False


def test_rc_difference_is_reported_as_rc(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, calls=[_call(["session", "list"], {"ok": True}, rc=0)])
    _write_run(b_dir, calls=[_call(["session", "list"], {"ok": True}, rc=1)])

    result = _diff(a_dir, b_dir)

    assert {"case": CASE, "path": "call[0].rc", "kind": "rc", "registered_change": None} in result["differences"]
    assert result["ok"] is False


def test_calls_length_difference_is_reported(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    submit = _call(["cmd", "submit", "--selector", "COM0", "--cmd", "echo x"], {"ok": True})
    _write_run(a_dir, calls=[submit])
    _write_run(b_dir, calls=[dict(submit), dict(submit)])

    result = _diff(a_dir, b_dir)

    assert {"case": CASE, "path": "calls.length", "kind": "value", "registered_change": None} in result["differences"]
    assert result["ok"] is False


def test_missing_run_on_one_side_is_integrity_problem(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, index="1")
    _write_run(a_dir, index="2")
    _write_run(b_dir, index="1")

    result = _diff(a_dir, b_dir)

    assert result["integrity"]["b"] is False
    assert any("B 側缺少對應 run" in problem for problem in result["integrity_problems"]["b"])


def test_repeat_runs_are_compared_per_run(tmp_path):
    """多 run：第 2 次 run 的差異必須被偵測，第 1 次相同不得遮蔽它。"""
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, index="1", calls=[_call(["session", "list"], {"ok": True, "v": 1})])
    _write_run(a_dir, index="2", calls=[_call(["session", "list"], {"ok": True, "v": 2})])
    _write_run(b_dir, index="1", calls=[_call(["session", "list"], {"ok": True, "v": 1})])
    _write_run(b_dir, index="2", calls=[_call(["session", "list"], {"ok": True, "v": 3})])

    result = _diff(a_dir, b_dir)

    assert result["integrity"] == {"a": True, "b": True}
    assert result["ok"] is False
    assert [item["path"] for item in result["differences"]] == ["call[0].v"]


def test_repeat_run_integrity_is_checked_per_run(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir, index="1")
    _write_run(a_dir, index="2", comparison={"verdict": "INSUFFICIENT_EVIDENCE", "invariants": []})
    _write_run(b_dir, index="1")
    _write_run(b_dir, index="2")

    result = _diff(a_dir, b_dir)

    assert result["integrity"]["a"] is False
    assert any("bridge/2" in problem for problem in result["integrity_problems"]["a"])


def test_optional_case_present_on_one_side_is_integrity_problem(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir)
    _write_run(a_dir, case="case-extra")
    _write_run(b_dir)

    result = _diff(a_dir, b_dir)

    assert result["integrity"]["b"] is False
    assert any("case-extra" in problem for problem in result["integrity_problems"]["b"])
    assert result["ok"] is False


def test_safety_tokens_is_public_and_matches_diff(tmp_path):
    a_dir, b_dir = _sides(tmp_path)
    failed = {"verdict": "FAIL", "invariants": [{"id": "zero_tx_on_reject", "verdict": "FAIL", "detail": "x"}]}
    _write_run(a_dir, comparison=failed)
    _write_run(b_dir)

    assert ab_diff.safety_tokens(a_dir, "A") == [f"A:{CASE}:zero_tx_on_reject"]
    assert ab_diff.safety_tokens(b_dir, "B") == []


def test_unknown_rule_key_is_refused_as_comparator_error(tmp_path):
    """拼錯或未併入的頂層鍵不得被靜默忽略（P5 併入時須同步擴充白名單）。"""
    a_dir, b_dir = _sides(tmp_path)
    _write_run(a_dir)
    _write_run(b_dir)
    typo = tmp_path / "typo.yaml"
    typo.write_text(RULES_PATH.read_text(encoding="utf-8") + "\nvolatile_key:\n  - pid\n", encoding="utf-8")

    with pytest.raises(HarnessError) as info:
        _diff(a_dir, b_dir, rules_path=typo)

    assert info.value.code == "HARNESS_COMPARATOR_ERROR"
