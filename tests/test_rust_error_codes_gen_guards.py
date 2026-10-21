"""P3-T8 補強：錯誤碼生成器的禁止集合推導、SC-34 精確名稱檢查、namespace 驗證、rustdoc 跳脫與 UTF-8 固定輸出。

以記憶體內合成的 YAML 文件直接呼叫 scripts/rust/gen_error_codes.py 的解析函式，不啟動子程序。
"""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "gen_error_codes.py"
pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_error_codes", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass 於類別建立時需查得模組
    spec.loader.exec_module(mod)
    return mod


def _error_doc(*codes: str, passthrough: bool = False) -> dict:
    doc = {
        "schema_version": 1,
        "revision": 1,
        "namespace": "error_code",
        "codes": [
            {
                "code": code,
                "status": "EXISTING",
                "retryable": "no",
                "reachable": True,
                "scope": {"interfaces": ["rpc"]},
                "meaning": f"{code} 測試說明",
            }
            for code in codes
        ],
    }
    if passthrough:
        doc["passthrough"] = [{"id": "last_error_passthrough"}]
    return doc


def _session_doc(*values: tuple[str, str]) -> dict:
    return {
        "schema_version": 1,
        "revision": 1,
        "namespace": "session_last_error",
        "values": [
            {
                "value": name,
                "kind": "named",
                "reachable": True,
                "meaning": "測試說明",
                "error_code_namespace": ns,
            }
            for name, ns in values
        ],
    }


def test_forbidden_session_name_is_rejected_by_error_codes():
    gen = _load_gen()
    with pytest.raises(gen.GenError, match="SESSION_ONLY_X"):
        gen._parse_error_codes(_error_doc("OK_CODE", "SESSION_ONLY_X"), frozenset({"SESSION_ONLY_X"}))


def test_forbidden_set_is_derived_from_session_namespace():
    gen = _load_gen()
    _, entries = gen._parse_session_last_errors(
        _session_doc(("SESSION_ONLY_X", "forbidden"), ("SHARED_Y", "allowed_shared"))
    )
    forbidden = frozenset(e.value for e in entries if e.namespace == gen.NAMESPACE_FORBIDDEN)
    assert forbidden == {"SESSION_ONLY_X"}
    _, codes = gen._parse_error_codes(_error_doc("SHARED_Y"), forbidden)
    assert [c.code for c in codes] == ["SHARED_Y"]


def test_session_namespace_must_be_declared_and_known():
    gen = _load_gen()
    doc = _session_doc(("SHARED_Y", "allowed_shared"))
    doc["values"][0]["error_code_namespace"] = "bogus"
    with pytest.raises(gen.GenError, match="error_code_namespace"):
        gen._parse_session_last_errors(doc)


def test_sc34_removed_name_is_rejected_by_exact_match():
    gen = _load_gen()
    with pytest.raises(gen.GenError, match="SC-34"):
        gen._parse_error_codes(_error_doc("LAST_ERROR_PASSTHROUGH"))


def test_passthrough_substring_no_longer_over_bans_other_codes():
    gen = _load_gen()
    _, codes = gen._parse_error_codes(_error_doc("PASSTHROUGH_NOTICE_X"))
    assert [c.variant for c in codes] == ["PassthroughNoticeX"]


def test_top_level_passthrough_section_produces_no_variant():
    gen = _load_gen()
    _, codes = gen._parse_error_codes(_error_doc("OK_CODE", passthrough=True))
    assert [c.variant for c in codes] == ["OkCode"]


def test_doc_comment_escapes_rustdoc_sensitive_characters():
    gen = _load_gen()
    lines = gen._doc_comment("[user@]host:port 與 <reason>", "    ")
    assert lines == ["    /// \\[user@\\]host:port 與 \\<reason>"]


def test_force_utf8_stdio_reconfigures_both_streams(monkeypatch):
    gen = _load_gen()
    buf_out = io.BytesIO()
    out = io.TextIOWrapper(buf_out, encoding="cp1252")
    err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    gen._force_utf8_stdio()
    print("寫入 crates/sw-proto/src/error_codes.rs")
    out.flush()
    assert buf_out.getvalue().decode("utf-8") == "寫入 crates/sw-proto/src/error_codes.rs\n"
