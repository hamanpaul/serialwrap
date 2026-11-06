"""`scripts/rust/compare_test_baseline.py` 的測試（parity-amendments-2026-10-26 #179，P6 審查 F1）。

07 Task 1 Step 4 的 gate 由「06 既有測試全部仍綠燈」改為「與 P5 基線逐 test binary 結果相同」：
P5 基線本身就有骨架紅燈，舊 gate 恆不成立（空洞）。本檔只以合成的 cargo test 輸出驗證解析與比對規則，
腳本以 importlib 在行程內載入，不呼叫 cargo。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("compare_test_baseline", ROOT / "scripts/rust/compare_test_baseline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASE_LOG = """\
   Compiling sw-core v0.3.2 (/x/crates/sw-core)
     Running unittests src/lib.rs (target/debug/deps/sw_core-0123456789abcdef)

running 2 tests
test unit_a ... ok
test unit_b ... FAILED

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

     Running tests/arbiter.rs (target/debug/deps/arbiter-fedcba9876543210)

running 3 tests
test admit_ok ... ok
test admit_red ... FAILED
test slow_case ... ignored, needs hw

test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s

   Doc-tests sw_core

running 1 test
test crates/sw-core/src/lib.rs - doc (line 3) ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""


def test_parse_groups_by_binary():
    m = _mod()
    got = m.parse_cargo_log(BASE_LOG)
    assert got == {
        "unittests src/lib.rs (sw_core)": {"unit_a": "ok", "unit_b": "FAILED"},
        "tests/arbiter.rs (arbiter)": {"admit_ok": "ok", "admit_red": "FAILED", "slow_case": "ignored"},
        "doc-tests sw_core": {"crates/sw-core/src/lib.rs - doc (line 3)": "ok"},
    }


def test_identical_results_pass():
    m = _mod()
    base = m.parse_cargo_log(BASE_LOG)
    report = m.compare(base, json.loads(json.dumps(base)))
    assert report["regressions"] == [] and report["missing"] == [] and report["ok"] is True


def test_base_red_staying_red_is_not_a_regression_but_green_to_red_is():
    m = _mod()
    base = m.parse_cargo_log(BASE_LOG)
    head = json.loads(json.dumps(base))
    head["tests/arbiter.rs (arbiter)"]["admit_ok"] = "FAILED"
    report = m.compare(base, head)
    assert report["ok"] is False
    assert report["regressions"] == [("tests/arbiter.rs (arbiter)", "admit_ok", "ok", "FAILED")]


def test_missing_binary_or_test_fails():
    m = _mod()
    base = m.parse_cargo_log(BASE_LOG)
    head = json.loads(json.dumps(base))
    del head["unittests src/lib.rs (sw_core)"]
    del head["tests/arbiter.rs (arbiter)"]["admit_red"]
    report = m.compare(base, head)
    assert report["ok"] is False
    assert ("unittests src/lib.rs (sw_core)", "*") in report["missing"]
    assert ("tests/arbiter.rs (arbiter)", "admit_red") in report["missing"]


def test_head_only_tests_and_improvements_are_informational():
    m = _mod()
    base = m.parse_cargo_log(BASE_LOG)
    head = json.loads(json.dumps(base))
    head["tests/arbiter.rs (arbiter)"]["admit_red"] = "ok"
    head["tests/console.rs (console)"] = {"new_red": "FAILED"}
    report = m.compare(base, head)
    assert report["ok"] is True
    assert report["improvements"] == [("tests/arbiter.rs (arbiter)", "admit_red", "FAILED", "ok")]
    assert report["head_only"] == [("tests/console.rs (console)", "new_red", "FAILED")]


def test_strict_flags_any_status_change():
    m = _mod()
    base = m.parse_cargo_log(BASE_LOG)
    head = json.loads(json.dumps(base))
    head["tests/arbiter.rs (arbiter)"]["admit_red"] = "ok"
    assert m.compare(base, head, strict=True)["ok"] is False


def test_cli_compare_exit_codes(tmp_path, capsys):
    m = _mod()
    base = tmp_path / "base.log"
    base.write_text(BASE_LOG, encoding="utf-8")
    same = tmp_path / "head.log"
    same.write_text(BASE_LOG, encoding="utf-8")
    assert m.main(["compare", str(base), str(same)]) == 0
    worse = tmp_path / "worse.log"
    worse.write_text(BASE_LOG.replace("test admit_ok ... ok", "test admit_ok ... FAILED"), encoding="utf-8")
    assert m.main(["compare", str(base), str(worse)]) == 1
    out = capsys.readouterr().out
    assert "REGRESSION tests/arbiter.rs (arbiter)::admit_ok ok→FAILED" in out
    # 執行數為 0（例如漏了 --features test-fakes）不得當成「相同」
    empty = tmp_path / "empty.log"
    empty.write_text("   Compiling x\n", encoding="utf-8")
    assert m.main(["compare", str(empty), str(empty)]) == 2


def test_failed_to_ignored_is_a_reduction_and_fails():
    """sol 補審 #5：基線 `FAILED` 的測試在待測加上 `#[ignore]` 變成 `ignored`，等於縮減實際執行量，預設即須 FAIL。"""
    m = _mod()
    base = m.parse_cargo_log(BASE_LOG)
    head = json.loads(json.dumps(base))
    head["tests/arbiter.rs (arbiter)"]["admit_red"] = "ignored"
    report = m.compare(base, head)
    assert report["ok"] is False
    assert report["reductions"] == [("tests/arbiter.rs (arbiter)", "admit_red", "FAILED", "ignored")]


def test_ignored_staying_ignored_and_ignored_to_failed_are_not_reductions():
    m = _mod()
    base = m.parse_cargo_log(BASE_LOG)
    head = json.loads(json.dumps(base))
    head["tests/arbiter.rs (arbiter)"]["slow_case"] = "FAILED"
    report = m.compare(base, head)
    assert report["reductions"] == [] and report["ok"] is True


def test_approved_ignore_is_allowed_only_by_exact_name():
    """核准的平台排除須逐名記錄（`binary::測試名`），其他 `FAILED → ignored` 仍 FAIL。"""
    m = _mod()
    base = m.parse_cargo_log(BASE_LOG)
    head = json.loads(json.dumps(base))
    head["tests/arbiter.rs (arbiter)"]["admit_red"] = "ignored"
    head["unittests src/lib.rs (sw_core)"]["unit_b"] = "ignored"
    allow = {"tests/arbiter.rs (arbiter)::admit_red"}
    report = m.compare(base, head, allow_ignored=allow)
    assert report["ok"] is False
    assert report["reductions"] == [("unittests src/lib.rs (sw_core)", "unit_b", "FAILED", "ignored")]
    allow.add("unittests src/lib.rs (sw_core)::unit_b")
    assert m.compare(base, head, allow_ignored=allow)["ok"] is True


def test_cli_failed_to_ignored_exits_nonzero(tmp_path, capsys):
    m = _mod()
    base = tmp_path / "base.log"
    base.write_text(BASE_LOG, encoding="utf-8")
    head = tmp_path / "head.log"
    head.write_text(BASE_LOG.replace("test admit_red ... FAILED", "test admit_red ... ignored"), encoding="utf-8")
    assert m.main(["compare", str(base), str(head)]) == 1
    assert "REDUCED tests/arbiter.rs (arbiter)::admit_red FAILED→ignored" in capsys.readouterr().out
    assert m.main(["compare", str(base), str(head), "--allow-ignored", "tests/arbiter.rs (arbiter)::admit_red"]) == 0
