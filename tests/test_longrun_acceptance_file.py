"""長跑門檻唯一來源的契約測試。"""

from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "realhw" / "acceptance" / "longrun_acceptance.yaml"
SCHEMA = ROOT / "realhw" / "acceptance" / "longrun_acceptance.schema.json"


def _load_acceptance_and_schema():
    import yaml
    from jsonschema import Draft202012Validator

    acceptance = yaml.safe_load(ACCEPTANCE.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return acceptance, schema, Draft202012Validator


def _validate_rejected(validator, schema, value):
    assert list(validator(schema).iter_errors(value))


def test_acceptance_file_schema():
    acceptance, schema, validator = _load_acceptance_and_schema()
    validator(schema).validate(acceptance)

    thresholds = acceptance["thresholds"]
    for key in ("unexpected_cmd_error_rate", "effective_throughput", "stuck_attached"):
        assert thresholds[key]["status"] == "frozen"
    deferred_fields = {
        "resources": {"rss_peak_kb", "rss_steady_kb", "rss_slope_kb_per_h"},
        "min_effective_load": {"min_completed_per_board_source", "max_idle_gap_s"},
        "comparison_uncertainty": {
            "error_rate_abs_tolerance",
            "throughput_ratio_tolerance",
            "stuck_s_tolerance",
            "max_reruns",
        },
    }
    for key, fields in deferred_fields.items():
        threshold = thresholds[key]
        assert threshold["status"] == "deferred"
        assert set(threshold["per_platform"]) == {"linux", "windows"}
        for platform in ("linux", "windows"):
            values = threshold["per_platform"][platform]
            assert set(values) == fields
            assert all(value is None for value in values.values())

        for platform in ("linux", "windows"):
            missing_platform = copy.deepcopy(acceptance)
            del missing_platform["thresholds"][key]["per_platform"][platform]
            _validate_rejected(validator, schema, missing_platform)

        scalar_platform = copy.deepcopy(acceptance)
        scalar_platform["thresholds"][key]["per_platform"]["linux"] = 123
        _validate_rejected(validator, schema, scalar_platform)

        scalar_threshold = copy.deepcopy(acceptance)
        scalar_threshold["thresholds"][key]["per_platform"] = 123
        _validate_rejected(validator, schema, scalar_threshold)

        extra_field = copy.deepcopy(acceptance)
        extra_field["thresholds"][key]["per_platform"]["linux"]["unexpected"] = 1
        _validate_rejected(validator, schema, extra_field)

        direct_numeric_field = copy.deepcopy(acceptance)
        direct_numeric_field["thresholds"][key][next(iter(fields))] = 1
        _validate_rejected(validator, schema, direct_numeric_field)

    tolerance_subtree = copy.deepcopy(acceptance)
    tolerance_subtree["thresholds"]["comparison_uncertainty"]["tolerance"] = {"value": 0.1}
    _validate_rejected(validator, schema, tolerance_subtree)

    frozen_with_null = copy.deepcopy(acceptance)
    frozen_with_null["thresholds"]["resources"]["status"] = "frozen"
    _validate_rejected(validator, schema, frozen_with_null)

    scalar_min_load = copy.deepcopy(acceptance)
    scalar_min_load["thresholds"]["min_effective_load"]["per_platform"]["linux"][
        "min_completed_per_board_source"
    ] = 100
    _validate_rejected(validator, schema, scalar_min_load)


def test_allowed_source_regex_matches_engine():
    import yaml

    acceptance = yaml.safe_load(ACCEPTANCE.read_text(encoding="utf-8"))
    pattern = re.compile(acceptance["safety"]["allowed_tx_source_regex"])
    for source in [*(f"agent:rhw{i}" for i in range(8)), "human:client-1", "system", "system:recover", "system:self_test"]:
        assert pattern.fullmatch(source)
    assert pattern.fullmatch("agent") is None
    assert pattern.fullmatch("agent:intruder") is None


def test_frozen_values_exact():
    import yaml

    acceptance = yaml.safe_load(ACCEPTANCE.read_text(encoding="utf-8"))
    thresholds = acceptance["thresholds"]
    assert thresholds["unexpected_cmd_error_rate"]["max_ratio"] == 0.01
    assert thresholds["effective_throughput"]["min_ratio_of_baseline"] == 0.95
    assert acceptance["workload"]["min_continuous_duration_s"] == 172800
    assert acceptance["workload"]["allow_segment_stitching"] is False
    assert all(acceptance["safety"][key] == 0 for key in (
        "unauthorized_tx", "forbidden_double_holder", "cross_context_replay", "dangerous_auto_resend"
    ))
    assert acceptance["safety"]["waivable"] is False
    assert acceptance["workload"]["windows_excl_probe_interval_s"] == 300
    assert acceptance["evidence"]["require_windows_exclusive_evidence"] is True


def test_single_source_of_thresholds():
    forbidden = []
    for directory in (ROOT / "realhw", ROOT / "reliability", ROOT / "regression"):
        for path in directory.rglob("*"):
            if not path.is_file() or path == ACCEPTANCE or path == SCHEMA or path == Path(__file__):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if ("0.95" in text and "throughput" in text) or "172800" in text:
                forbidden.append(path.relative_to(ROOT).as_posix())
    assert forbidden == []


def test_engine_does_not_import_yaml_or_gate():
    paths = [*(ROOT / "realhw").glob("*.py"), *(ROOT / "realhw" / "cases").glob("*.py")]
    excluded = ROOT / "realhw" / "acceptance" / "gate.py"
    for path in paths:
        if path == excluded:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imports.append(module)
                imports.extend(f"{module}.{alias.name}" for alias in node.names)
        assert "yaml" not in imports, path.relative_to(ROOT)
        assert "realhw.acceptance.gate" not in imports, path.relative_to(ROOT)
