from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load():
    """延遲載入 tools.contracts.catalog 模組。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("tools.contracts.catalog")


GOOD_EC = textwrap.dedent("""\
    schema_version: 1
    revision: 1
    namespace: error_code
    codes:
      - code: FOO_BAR
        status: EXISTING
        reachable: true
        scope: {interfaces: [rpc], methods: [x.y], platforms: [posix, windows]}
        meaning: 意義
        side_effect: 無
        retryable: "no"
        uart_tx: none
        agent_action: 處置
        extra_fields: []
        contract_tests: [tests/test_x.py::test_y]
        behavior_change: []
    passthrough: []
    """)


GOOD_LE = textwrap.dedent("""\
    schema_version: 1
    revision: 1
    namespace: session_last_error
    values:
      - value: "DYNAMIC_PREFIX:*"
        kind: dynamic_prefix
        reason_forms: ["SERIAL_READ:EIO"]
        reachable: true
        meaning: 動態意義
        agent_action: 處置
        manual_recovery: 復原
        logging: 無
        detail: 無
        terminal: false
        auto_reprobe: {attached: false, detached: false}
        states: [ATTACHED]
        set_by: ["session_manager.py:100"]
        cleared_by: ["auto:重探@session_manager.py:200"]
        promoted_via: []
        error_code_namespace: allowed_shared
        contract_tests: [tests/test_x.py::test_y]
      - value: FOO_ERROR
        kind: named
        reason_forms: []
        reachable: true
        meaning: 意義
        agent_action: 處置
        manual_recovery: 復原
        logging: 無
        detail: 無
        terminal: false
        auto_reprobe: {attached: true, detached: false}
        states: [ATTACHED]
        set_by: ["session_manager.py:100"]
        cleared_by: ["auto:重探@session_manager.py:200"]
        promoted_via: [session.attach]
        error_code_namespace: allowed_shared
        contract_tests: [tests/test_x.py::test_y]
    """)


GOOD_BC = textwrap.dedent("""\
    schema_version: 1
    revision: 1
    changes:
      - id: BC-01
        kind: BC
        title: 行為變更01
        status: APPROVED
        approval:
          owner: hamanpaul
          date: "2026-10-15"
          decision_ref: OD-04
          basis: owner_decision
        owner_confirmation: not_required
        owner_item: null
        default_if_pending: null
        applies_to:
          python: {min_version: "0.4.0"}
          rust: {stage: "stage-1a"}
        approved_at_revision: 1
        spec_refs: ["§1.1"]
        reconciliation_refs: ["R-01"]
        scope: both
        ab_participation: diff
        phases: ["P0"]
        case_ids: [bc01-case-1]
        compatibility_impact: 相容性影響
        approval_conditions: 核准條件
        related_decisions: ["OD-01"]
      - id: DF-01
        kind: DF
        title: 差異01
        status: APPROVED
        approval:
          owner: spec-reconciliation
          date: "2026-10-15"
          decision_ref: SES-01
          basis: reconciliation
        owner_confirmation: not_required
        owner_item: null
        default_if_pending: null
        applies_to:
          python: {min_version: "0.4.0"}
          rust: {stage: "stage-1a"}
        approved_at_revision: 1
        spec_refs: ["§1.1"]
        reconciliation_refs: ["R-01"]
        scope: both
        ab_participation: diff
        phases: ["P0"]
        case_ids: [df01-case-1]
        compatibility_impact: 相容性影響
        approval_conditions: 核准條件
        related_decisions: []
    pending_owner_confirmations:
      - item: 1
        topic: 主題
        refs: ["REF-1"]
        default: 預設值
        affects: [BC-01]
    safety_invariants:
      - id: zero_tx_on_reject
        title: 零TX
        spec_ref: "§11.3"
        error_codes: []
        case_ids: [bc01-case-1]
      - id: no_forbidden_interleave
        title: 禁止交錯
        spec_ref: "§11.3"
        error_codes: []
        case_ids: []
      - id: no_stale_context_replay
        title: 禁止重放
        spec_ref: "§11.3"
        error_codes: []
        case_ids: []
      - id: no_reopen_before_handoff
        title: 交接前不重開
        spec_ref: "§11.3"
        error_codes: []
        case_ids: []
      - id: no_auto_resend
        title: 不自動重送
        spec_ref: "§11.3"
        error_codes: []
        case_ids: []
      - id: insufficient_evidence_not_pass
        title: 證據不足不過
        spec_ref: "§11.3"
        error_codes: []
        case_ids: []
    """)


GOOD_AUX = textwrap.dedent("""\
    schema_version: 1
    revision: 1
    warning_codes:
      - code: CMD_LENGTH_WARNING
        meaning: 意義
    error_details:
      - value: NOT_BOOTLOADER
        parent_code: SESSION_NOT_READY
        meaning: 意義
    recommended_actions:
      - value: retry
        meaning: 意義
    classifications:
      - value: OK
        recommended_actions: [retry]
        meaning: 意義
    recovery_actions:
      - value: NONE
        meaning: 意義
    recover_actions:
      - value: REATTACH
        meaning: 意義
    """)


def test_good_error_codes_doc_has_no_problems():
    """合法 error_codes 文件不產生任何問題回報。"""
    assert _load().validate_error_codes_doc(yaml.safe_load(GOOD_EC)) == []


def test_good_session_last_errors_doc_has_no_problems():
    """合法 session_last_errors 文件不產生任何問題回報。"""
    assert _load().validate_session_last_errors_doc(yaml.safe_load(GOOD_LE)) == []


def test_good_behavior_changes_doc_has_no_problems():
    """合法 behavior_changes 文件不產生任何問題回報。"""
    assert _load().validate_behavior_changes_doc(yaml.safe_load(GOOD_BC)) == []


def test_good_aux_doc_has_no_problems():
    """合法 diagnostic_aux 文件不產生任何問題回報。"""
    assert _load().validate_aux_doc(yaml.safe_load(GOOD_AUX)) == []


def test_unquoted_yes_retryable_is_rejected_with_quote_hint():
    """retryable 為未加引號的 yes（被解析為布林值）時應提示加引號。"""
    doc = yaml.safe_load(GOOD_EC.replace('retryable: "no"', "retryable: yes"))
    problems = _load().validate_error_codes_doc(doc)
    assert any("codes[0](FOO_BAR).retryable" in p and "引號" in p for p in problems)


def test_load_catalogs_reports_all_missing_files(tmp_path: Path):
    """load_catalogs 讀取空目錄時應彙整回報四個契約檔案遺失。"""
    cat = _load()
    with pytest.raises(cat.CatalogError) as ei:
        cat.load_catalogs(tmp_path)
    joined = "\n".join(ei.value.problems)
    for name in ("error_codes.yaml", "session_last_errors.yaml", "behavior_changes.yaml", "diagnostic_aux.yaml"):
        assert name in joined


def test_reachable_true_accepts_null_unreachable_reason() -> None:
    """reachable 為 true 時 unreachable_reason 可為 null（對應 dataclass 欄位 str | None）。"""
    cat = _load()
    ec = GOOD_EC.replace("reachable: true", "reachable: true\n    unreachable_reason: null")
    le = GOOD_LE.replace("reachable: true", "reachable: true\n    unreachable_reason: null")
    assert cat.validate_error_codes_doc(yaml.safe_load(ec)) == []
    assert cat.validate_session_last_errors_doc(yaml.safe_load(le)) == []


def test_load_catalogs_aggregates_reader_error_across_files(tmp_path: Path) -> None:
    """控制字元只影響所在檔案；其他檔案的結構問題仍須一併彙整回報（T1 要求所有檔案的問題一次列全）。"""
    cat = _load()
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "error_codes.yaml").write_text(GOOD_EC + "\x00\n", encoding="utf-8")
    (contracts / "session_last_errors.yaml").write_text(
        GOOD_LE.replace("schema_version: 1", "schema_version: 2"), encoding="utf-8")
    (contracts / "behavior_changes.yaml").write_text(GOOD_BC, encoding="utf-8")
    (contracts / "diagnostic_aux.yaml").write_text(GOOD_AUX, encoding="utf-8")
    with pytest.raises(cat.CatalogError) as ei:
        cat.load_catalogs(tmp_path)
    joined = "\n".join(ei.value.problems)
    assert "error_codes.yaml" in joined and "YAML 語法或型別錯誤" in joined
    assert "session_last_errors.yaml:schema_version" in joined
    assert "behavior_changes.yaml" not in joined
    assert "diagnostic_aux.yaml" not in joined


STRUCTURAL_CASES = [
    # error_codes 規則
    pytest.param(
        "ec_bad_code_pattern",
        "error_code",
        GOOD_EC.replace("code: FOO_BAR", "code: foo_bar"),
        "codes[0](foo_bar).code",
        id="ec_bad_code_pattern",
    ),
    pytest.param(
        "ec_duplicate_code",
        "error_code",
        GOOD_EC.replace(
            "passthrough: []",
            textwrap.dedent("""\
              - code: FOO_BAR
                status: EXISTING
                reachable: true
                scope: {interfaces: [rpc], methods: [x.y], platforms: [posix, windows]}
                meaning: 意義
                side_effect: 無
                retryable: "no"
                uart_tx: none
                agent_action: 處置
                extra_fields: []
                contract_tests: [tests/test_x.py::test_y]
                behavior_change: []
            passthrough: []
            """),
        ),
        "重複",
        id="ec_duplicate_code",
    ),
    pytest.param(
        "ec_unsorted",
        "error_code",
        GOOD_EC.replace(
            "passthrough: []",
            textwrap.dedent("""\
              - code: AAA
                status: EXISTING
                reachable: true
                scope: {interfaces: [rpc], methods: [x.y], platforms: [posix, windows]}
                meaning: 意義
                side_effect: 無
                retryable: "no"
                uart_tx: none
                agent_action: 處置
                extra_fields: []
                contract_tests: [tests/test_x.py::test_y]
                behavior_change: []
            passthrough: []
            """),
        ),
        "排序",
        id="ec_unsorted",
    ),
    pytest.param(
        "ec_bad_status",
        "error_code",
        GOOD_EC.replace("status: EXISTING", "status: LIVE"),
        ".status",
        id="ec_bad_status",
    ),
    pytest.param(
        "ec_unreachable_needs_reason",
        "error_code",
        GOOD_EC.replace("reachable: true", "reachable: false"),
        ".unreachable_reason",
        id="ec_unreachable_needs_reason",
    ),
    pytest.param(
        "ec_bad_interface",
        "error_code",
        GOOD_EC.replace("interfaces: [rpc]", "interfaces: [daemon]"),
        ".scope.interfaces",
        id="ec_bad_interface",
    ),
    pytest.param(
        "ec_empty_platforms",
        "error_code",
        GOOD_EC.replace("platforms: [posix, windows]", "platforms: []"),
        ".scope.platforms",
        id="ec_empty_platforms",
    ),
    pytest.param(
        "ec_pipe_in_meaning",
        "error_code",
        GOOD_EC.replace("meaning: 意義", 'meaning: "a|b"'),
        ".meaning",
        id="ec_pipe_in_meaning",
    ),
    pytest.param(
        "ec_newline_in_action",
        "error_code",
        GOOD_EC.replace("agent_action: 處置", 'agent_action: "a\\nb"'),
        ".agent_action",
        id="ec_newline_in_action",
    ),
    pytest.param(
        "ec_bool_no_retryable",
        "error_code",
        GOOD_EC.replace('retryable: "no"', "retryable: no"),
        "引號",
        id="ec_bool_no_retryable",
    ),
    pytest.param(
        "ec_bad_contract_test_ref",
        "error_code",
        GOOD_EC.replace("contract_tests: [tests/test_x.py::test_y]", "contract_tests: [not a path]"),
        ".contract_tests",
        id="ec_bad_contract_test_ref",
    ),
    pytest.param(
        "ec_bad_bc_ref",
        "error_code",
        GOOD_EC.replace("behavior_change: []", "behavior_change: [BC-1]"),
        ".behavior_change",
        id="ec_bad_bc_ref",
    ),
    pytest.param(
        "ec_cli_local_needs_exit_codes",
        "error_code",
        GOOD_EC.replace("interfaces: [rpc]", "interfaces: [cli_local]"),
        ".exit_codes",
        id="ec_cli_local_needs_exit_codes",
    ),
    pytest.param(
        "ec_approved_must_be_unreachable",
        "error_code",
        GOOD_EC.replace("status: EXISTING", "status: APPROVED").replace("behavior_change: []", "behavior_change: [BC-03]"),
        ".reachable",
        id="ec_approved_must_be_unreachable",
    ),
    pytest.param(
        "ec_approved_needs_behavior_change",
        "error_code",
        GOOD_EC.replace("status: EXISTING", "status: APPROVED").replace("reachable: true", "reachable: false\n    unreachable_reason: 已核准未實作"),
        ".behavior_change",
        id="ec_approved_needs_behavior_change",
    ),
    pytest.param(
        "ec_implemented_needs_reachable",
        "error_code",
        GOOD_EC.replace("status: EXISTING", "status: IMPLEMENTED").replace("reachable: true", "reachable: false\n    unreachable_reason: 未實作"),
        ".reachable",
        id="ec_implemented_needs_reachable",
    ),
    pytest.param(
        "ec_verified_needs_reachable",
        "error_code",
        GOOD_EC.replace("status: EXISTING", "status: VERIFIED").replace("reachable: true", "reachable: false\n    unreachable_reason: 未驗證"),
        ".reachable",
        id="ec_verified_needs_reachable",
    ),
    pytest.param(
        "ec_implemented_needs_tests",
        "error_code",
        GOOD_EC.replace("status: EXISTING", "status: IMPLEMENTED").replace("contract_tests: [tests/test_x.py::test_y]", "contract_tests: []"),
        ".contract_tests",
        id="ec_implemented_needs_tests",
    ),
    pytest.param(
        "ec_bad_uart_tx",
        "error_code",
        GOOD_EC.replace("uart_tx: none", "uart_tx: maybe"),
        ".uart_tx",
        id="ec_bad_uart_tx",
    ),
    pytest.param(
        "ec_unknown_exit_context",
        "error_code",
        GOOD_EC.replace("interfaces: [rpc]", "interfaces: [cli_local]").replace("behavior_change: []", 'behavior_change: []\n    exit_codes: {"daemon begin": 2}'),
        ".exit_codes",
        id="ec_unknown_exit_context",
    ),
    pytest.param(
        "yaml_duplicate_key",
        "yaml_strict",
        GOOD_EC.replace('retryable: "no"', 'retryable: "no"\n    retryable: "yes"'),
        "重複鍵",
        id="yaml_duplicate_key",
    ),
    pytest.param(
        "yaml_syntax_error",
        "yaml_strict",
        "codes: [",
        "CatalogError",
        id="yaml_syntax_error",
    ),
    pytest.param(
        "yaml_control_char",
        "yaml_strict",
        "schema_version: 1\x00\n",
        "YAML 語法或型別錯誤",
        id="yaml_control_char",
    ),
    pytest.param(
        "yaml_recursion_depth",
        "yaml_strict",
        "a: " + "[" * 5000 + "]" * 5000 + "\n",
        "YAML 語法或型別錯誤",
        id="yaml_recursion_depth",
    ),
    pytest.param(
        "ec_harness_prefix_forbidden",
        "error_code",
        GOOD_EC.replace("code: FOO_BAR", "code: HARNESS_TIMEOUT"),
        "HARNESS_",
        id="ec_harness_prefix_forbidden",
    ),
    pytest.param(
        "ec_unknown_top_key",
        "error_code",
        GOOD_EC + "extra: 1\n",
        "extra",
        id="ec_unknown_top_key",
    ),
    pytest.param(
        "ec_passthrough_bad_ref",
        "error_code",
        GOOD_EC.replace(
            "passthrough: []",
            textwrap.dedent("""\
            passthrough:
              - id: pt_test
                namespace_ref: other
                scope: {interfaces: [rpc], methods: [x.y], platforms: [posix]}
                rule: 規則
                owner_confirmation: not_required
                reconciliation_refs: [ref1]
            """),
        ),
        ".namespace_ref",
        id="ec_passthrough_bad_ref",
    ),
    pytest.param(
        "ec_header_schema_version",
        "error_code",
        GOOD_EC.replace("schema_version: 1", "schema_version: 2"),
        "schema_version",
        id="ec_header_schema_version",
    ),
    pytest.param(
        "ec_header_revision_zero",
        "error_code",
        GOOD_EC.replace("revision: 1", "revision: 0"),
        "revision",
        id="ec_header_revision_zero",
    ),
    pytest.param(
        "ec_header_namespace",
        "error_code",
        GOOD_EC.replace("namespace: error_code", "namespace: session_last_error"),
        "namespace",
        id="ec_header_namespace",
    ),
    pytest.param(
        "ec_missing_required_key",
        "error_code",
        GOOD_EC.replace("    meaning: 意義\n", ""),
        "缺少必要鍵 'meaning'",
        id="ec_missing_required_key",
    ),
    pytest.param(
        "ec_missing_top_key",
        "error_code",
        GOOD_EC.replace("passthrough: []\n", ""),
        "缺少頂層鍵 'passthrough'",
        id="ec_missing_top_key",
    ),
    pytest.param(
        "ec_unreachable_null_reason",
        "error_code",
        GOOD_EC.replace("reachable: true", "reachable: false\n    unreachable_reason: null"),
        ".unreachable_reason",
        id="ec_unreachable_null_reason",
    ),
    pytest.param(
        "ec_passthrough_bad_id",
        "error_code",
        GOOD_EC.replace(
            "passthrough: []",
            textwrap.dedent("""\
            passthrough:
              - id: Bad-Id
                namespace_ref: session_last_errors
                scope: {interfaces: [rpc], methods: [x.y], platforms: [posix]}
                rule: 規則
                owner_confirmation: not_required
                reconciliation_refs: [ref1]
            """),
        ),
        "passthrough[0](Bad-Id).id",
        id="ec_passthrough_bad_id",
    ),
    pytest.param(
        "ec_passthrough_duplicate_id",
        "error_code",
        GOOD_EC.replace(
            "passthrough: []",
            textwrap.dedent("""\
            passthrough:
              - id: pt_test
                namespace_ref: session_last_errors
                scope: {interfaces: [rpc], methods: [x.y], platforms: [posix]}
                rule: 規則
                owner_confirmation: not_required
                reconciliation_refs: [ref1]
              - id: pt_test
                namespace_ref: session_last_errors
                scope: {interfaces: [rpc], methods: [x.y], platforms: [posix]}
                rule: 規則
                owner_confirmation: not_required
                reconciliation_refs: [ref1]
            """),
        ),
        "passthrough[1](pt_test).id",
        id="ec_passthrough_duplicate_id",
    ),
    pytest.param(
        "ec_exit_code_value_type",
        "error_code",
        GOOD_EC.replace("interfaces: [rpc]", "interfaces: [cli_local]").replace("behavior_change: []", 'behavior_change: []\n    exit_codes: {doctor: "two"}'),
        "值必須為整數",
        id="ec_exit_code_value_type",
    ),
    # session_last_errors 規則
    pytest.param(
        "le_dynamic_needs_reason_forms",
        "session_last_error",
        GOOD_LE.replace('reason_forms: ["SERIAL_READ:EIO"]', "reason_forms: []"),
        ".reason_forms",
        id="le_dynamic_needs_reason_forms",
    ),
    pytest.param(
        "le_bad_state",
        "session_last_error",
        GOOD_LE.replace("states: [ATTACHED]", "states: [ONLINE]"),
        ".states",
        id="le_bad_state",
    ),
    pytest.param(
        "le_bad_promoted_via",
        "session_last_error",
        GOOD_LE.replace("promoted_via: [session.attach]", "promoted_via: [command.submit]"),
        ".promoted_via",
        id="le_bad_promoted_via",
    ),
    pytest.param(
        "le_bad_namespace_flag",
        "session_last_error",
        GOOD_LE.replace("error_code_namespace: allowed_shared", "error_code_namespace: maybe"),
        ".error_code_namespace",
        id="le_bad_namespace_flag",
    ),
    pytest.param(
        "le_terminal_cannot_reprobe",
        "session_last_error",
        GOOD_LE.replace(
            "terminal: false\n    auto_reprobe: {attached: true, detached: false}",
            "terminal: true\n    auto_reprobe: {attached: true, detached: false}",
        ).replace(
            'cleared_by: ["auto:重探@session_manager.py:200"]\n    promoted_via: [session.attach]',
            'cleared_by: ["operator:session.attach"]\n    promoted_via: [session.attach]',
        ),
        "values[1](FOO_ERROR).terminal",
        id="le_terminal_cannot_reprobe",
    ),
    pytest.param(
        "le_terminal_no_auto_clear",
        "session_last_error",
        GOOD_LE.replace(
            "value: FOO_ERROR\n    kind: named\n    reason_forms: []\n    reachable: true\n    meaning: 意義\n    agent_action: 處置\n    manual_recovery: 復原\n    logging: 無\n    detail: 無\n    terminal: false\n    auto_reprobe: {attached: true, detached: false}",
            "value: FOO_ERROR\n    kind: named\n    reason_forms: []\n    reachable: true\n    meaning: 意義\n    agent_action: 處置\n    manual_recovery: 復原\n    logging: 無\n    detail: 無\n    terminal: true\n    auto_reprobe: {attached: false, detached: false}",
        ).replace('cleared_by: ["auto:重探@session_manager.py:200"]', 'cleared_by: ["auto:裝置在線@session_manager.py:1846"]'),
        ".cleared_by",
        id="le_terminal_no_auto_clear",
    ),
    pytest.param(
        "le_promoted_needs_attached",
        "session_last_error",
        GOOD_LE.replace(
            "value: FOO_ERROR\n    kind: named\n    reason_forms: []\n    reachable: true\n    meaning: 意義\n    agent_action: 處置\n    manual_recovery: 復原\n    logging: 無\n    detail: 無\n    terminal: false\n    auto_reprobe: {attached: true, detached: false}\n    states: [ATTACHED]",
            "value: FOO_ERROR\n    kind: named\n    reason_forms: []\n    reachable: true\n    meaning: 意義\n    agent_action: 處置\n    manual_recovery: 復原\n    logging: 無\n    detail: 無\n    terminal: false\n    auto_reprobe: {attached: true, detached: false}\n    states: [DETACHED]",
        ).replace("promoted_via: [session.attach]", "promoted_via: [session.recover]"),
        ".promoted_via",
        id="le_promoted_needs_attached",
    ),
    pytest.param(
        "le_bad_cleared_by",
        "session_last_error",
        GOOD_LE.replace('cleared_by: ["auto:重探@session_manager.py:200"]', "cleared_by: [whenever]"),
        ".cleared_by",
        id="le_bad_cleared_by",
    ),
    pytest.param(
        "le_unreachable_null_reason",
        "session_last_error",
        GOOD_LE.replace("reachable: true", "reachable: false\n    unreachable_reason: null"),
        "values[1](FOO_ERROR).unreachable_reason",
        id="le_unreachable_null_reason",
    ),
    # behavior_changes 規則
    pytest.param(
        "bc_unquoted_date",
        "behavior_change",
        GOOD_BC.replace('date: "2026-10-15"', "date: 2026-10-15"),
        "引號",
        id="bc_unquoted_date",
    ),
    pytest.param(
        "bc_kind_mismatch",
        "behavior_change",
        GOOD_BC.replace("id: BC-01\n    kind: BC", "id: BC-01\n    kind: SC"),
        ".kind",
        id="bc_kind_mismatch",
    ),
    pytest.param(
        "bc_bc_requires_od04",
        "behavior_change",
        GOOD_BC.replace("decision_ref: OD-04", "decision_ref: OD-06"),
        "OD-04",
        id="bc_bc_requires_od04",
    ),
    pytest.param(
        "bc_owner_matches_basis",
        "behavior_change",
        GOOD_BC.replace("owner: spec-reconciliation", "owner: hamanpaul"),
        ".approval.owner",
        id="bc_owner_matches_basis",
    ),
    pytest.param(
        "bc_rust_only_no_python",
        "behavior_change",
        GOOD_BC.replace("scope: both\n    ab_participation: diff", "scope: rust_only\n    ab_participation: absolute_only"),
        ".applies_to.python",
        id="bc_rust_only_no_python",
    ),
    pytest.param(
        "bc_manifest_scope_needs_python",
        "behavior_change",
        GOOD_BC.replace("scope: both\n    ab_participation: diff", "scope: python_when_manifest_present\n    ab_participation: absolute_only").replace('python: {min_version: "0.4.0"}', "python: null"),
        ".applies_to.python",
        id="bc_manifest_scope_needs_python",
    ),
    pytest.param(
        "bc_ab_participation_mismatch",
        "behavior_change",
        GOOD_BC.replace("ab_participation: diff", "ab_participation: absolute_only"),
        ".ab_participation",
        id="bc_ab_participation_mismatch",
    ),
    pytest.param(
        "bc_revision_exceeds_file",
        "behavior_change",
        GOOD_BC.replace("approved_at_revision: 1", "approved_at_revision: 2"),
        ".approved_at_revision",
        id="bc_revision_exceeds_file",
    ),
    pytest.param(
        "bc_contract_revision_key_forbidden",
        "behavior_change",
        GOOD_BC.replace("approved_at_revision: 1", "approved_at_revision: 1\n    contract_revision: 1"),
        "tree SHA",
        id="bc_contract_revision_key_forbidden",
    ),
    pytest.param(
        "bc_invariant_set_incomplete",
        "behavior_change",
        GOOD_BC.replace(
            '  - id: no_auto_resend\n    title: 不自動重送\n    spec_ref: "§11.3"\n    error_codes: []\n    case_ids: []\n',
            "",
        ),
        "safety_invariants",
        id="bc_invariant_set_incomplete",
    ),
    pytest.param(
        "bc_invariant_unknown_case",
        "behavior_change",
        GOOD_BC.replace("error_codes: []\n    case_ids: [bc01-case-1]", "error_codes: []\n    case_ids: [nope-case]"),
        "case_ids",
        id="bc_invariant_unknown_case",
    ),
    pytest.param(
        "bc_duplicate_case_id",
        "behavior_change",
        GOOD_BC.replace("case_ids: [df01-case-1]", "case_ids: [bc01-case-1]"),
        "case_ids",
        id="bc_duplicate_case_id",
    ),
    pytest.param(
        "bc_pending_needs_item",
        "behavior_change",
        GOOD_BC.replace(
            "owner_confirmation: not_required\n    owner_item: null\n    default_if_pending: null",
            "owner_confirmation: pending_default\n    owner_item: null\n    default_if_pending: 預設值",
        ),
        ".owner_item",
        id="bc_pending_needs_item",
    ),
    pytest.param(
        "bc_affects_unknown",
        "behavior_change",
        GOOD_BC.replace("affects: [BC-01]", "affects: [SC-99]"),
        "affects",
        id="bc_affects_unknown",
    ),
    pytest.param(
        "bc_basis_not_in_enum",
        "behavior_change",
        GOOD_BC.replace("basis: owner_decision", "basis: whatever"),
        ".approval.basis",
        id="bc_basis_not_in_enum",
    ),
    pytest.param(
        "bc_basis_kind_mismatch",
        "behavior_change",
        GOOD_BC.replace("basis: owner_decision", "basis: reconciliation"),
        "kind 為 BC 時 basis 必須為 owner_decision",
        id="bc_basis_kind_mismatch",
    ),
    pytest.param(
        "bc_pending_needs_default",
        "behavior_change",
        GOOD_BC.replace(
            "owner_confirmation: not_required\n    owner_item: null\n    default_if_pending: null",
            "owner_confirmation: pending_default\n    owner_item: 1\n    default_if_pending: null",
        ),
        ".default_if_pending",
        id="bc_pending_needs_default",
    ),
    # diagnostic_aux 規則
    pytest.param(
        "aux_duplicate_classification",
        "aux",
        GOOD_AUX.replace(
            "classifications:\n  - value: OK\n    recommended_actions: [retry]\n    meaning: 意義",
            "classifications:\n  - value: OK\n    recommended_actions: [retry]\n    meaning: 意義\n  - value: OK\n    recommended_actions: [retry]\n    meaning: 意義2",
        ),
        "重複",
        id="aux_duplicate_classification",
    ),
    pytest.param(
        "aux_classification_action_unknown",
        "aux",
        GOOD_AUX.replace("recommended_actions: [retry]", 'recommended_actions: ["attach／recover"]'),
        ".recommended_actions",
        id="aux_classification_action_unknown",
    ),
]


@pytest.mark.parametrize(
    ("test_id", "validator_type", "doc_or_text", "expected_needle"),
    STRUCTURAL_CASES,
)
def test_catalog_structural_rules(
    test_id: str,
    validator_type: str,
    doc_or_text: str,
    expected_needle: str,
    tmp_path: Path,
) -> None:
    """參數化驗證契約目錄結構規則與語法檢查。"""
    cat = _load()

    if validator_type == "yaml_strict":
        p = tmp_path / f"{test_id}.yaml"
        p.write_text(doc_or_text, encoding="utf-8")
        with pytest.raises(cat.CatalogError) as ei:
            cat.load_yaml_strict(p)
        if expected_needle == "CatalogError":
            assert not isinstance(ei.value, yaml.YAMLError)
        else:
            assert any(expected_needle in prob for prob in ei.value.problems)
        return

    doc = yaml.safe_load(doc_or_text)
    if validator_type == "error_code":
        problems = cat.validate_error_codes_doc(doc)
    elif validator_type == "session_last_error":
        problems = cat.validate_session_last_errors_doc(doc)
    elif validator_type == "behavior_change":
        problems = cat.validate_behavior_changes_doc(doc)
    elif validator_type == "aux":
        problems = cat.validate_aux_doc(doc)
    else:
        raise ValueError(f"未知的 validator_type: {validator_type}")

    assert any(expected_needle in prob for prob in problems), (
        f"期望 problems 中包含 '{expected_needle}'，實際問題清單為: {problems}"
    )


# ==============================================================================
# Task T3: contracts/error_codes.yaml（RPC／CLI error_code 權威目錄）真實目錄測試
# ==============================================================================


def _ec():
    return _load().load_error_codes(ROOT)  # (codes, passthrough)；只讀 error_codes.yaml


def _ec_by_code():
    return {e.code: e for e in _ec()[0]}


def _get_change_status(change_id: str) -> str | None:
    try:
        res = _load().load_behavior_changes(ROOT)
        changes = res[0] if isinstance(res, (tuple, list)) else res
        for c in changes:
            if getattr(c, "id", None) == change_id:
                return getattr(c, "status", None)
    except Exception:
        return None
    return None


def _assert_status_tracks_change(entry, change_status: str | None) -> None:
    # 不凍結字面 APPROVED：依變更狀態推導（P1 翻轉後本測試仍成立）
    if change_status in (None, "PROPOSED", "APPROVED"):
        assert entry.status == "APPROVED" and entry.reachable is False
    else:
        assert entry.status in ("IMPLEMENTED", "VERIFIED") and entry.reachable is True and entry.contract_tests


def test_ec_real_catalog_loads() -> None:
    """error_codes.yaml 真實契約目錄可正常載入。"""
    _ec()  # 不 raise 即通過


def test_ec_credentials_unresolved_not_an_error_code() -> None:
    """CREDENTIALS_UNRESOLVED 僅為 session last_error，不得出現在 error_code 目錄。"""
    assert "CREDENTIALS_UNRESOLVED" not in _ec_by_code()


def test_ec_passthrough_single_meta_entry() -> None:
    """passthrough 必須恰有一筆 last_error_passthrough meta entry。"""
    codes, passthrough = _ec()
    assert [p.id for p in passthrough] == ["last_error_passthrough"]
    pt = passthrough[0]
    assert pt.namespace_ref == "session_last_errors"
    assert pt.owner_confirmation in {"pending_default", "confirmed"}


def test_ec_bc03_status_tracks_change() -> None:
    """COMMAND_ABORTED_IN_FLIGHT 狀態應依 BC-03 推進，且屬性符合規範。"""
    by = _ec_by_code()
    assert "COMMAND_ABORTED_IN_FLIGHT" in by
    e = by["COMMAND_ABORTED_IN_FLIGHT"]
    _assert_status_tracks_change(e, _get_change_status("BC-03"))
    assert e.retryable == "no"
    assert e.uart_tx == "possible"
    assert "BC-03" in e.behavior_change
    assert tuple(e.scope.interfaces) == ("rec",)


def test_ec_sc19_status_tracks_change() -> None:
    """TEST_DEVICE_ALLOWLIST_REJECTED 狀態應依 SC-19 推進，且介面為 proc。"""
    by = _ec_by_code()
    assert "TEST_DEVICE_ALLOWLIST_REJECTED" in by
    e = by["TEST_DEVICE_ALLOWLIST_REJECTED"]
    _assert_status_tracks_change(e, _get_change_status("SC-19"))
    assert tuple(e.scope.interfaces) == ("proc",)
    assert "SC-19" in e.behavior_change


def test_ec_proc_only_codes() -> None:
    """DAEMON_ALREADY_RUNNING 與 SOCKET_UNAVAILABLE 之介面僅為 proc。"""
    by = _ec_by_code()
    for code in ("DAEMON_ALREADY_RUNNING", "SOCKET_UNAVAILABLE"):
        assert code in by
        assert tuple(by[code].scope.interfaces) == ("proc",)


def test_ec_rec_codes() -> None:
    """REC 錯誤碼之 interfaces 必須包含 rec。"""
    by = _ec_by_code()
    for code in ("FLUSHED_BY_RECOVERY", "FLUSHED_BY_SHUTDOWN", "SEND_FAILED", "COMMAND_FAILED"):
        assert code in by
        assert "rec" in by[code].scope.interfaces


def test_ec_frame_codes() -> None:
    """RPC framing 錯誤碼之 interfaces 必須包含 rpc_frame。"""
    by = _ec_by_code()
    for code in ("INVALID_JSON", "INVALID_REQUEST", "INVALID_METHOD", "EXCEPTION"):
        assert code in by
        assert "rpc_frame" in by[code].scope.interfaces


def test_ec_client_codes() -> None:
    """CLI client 錯誤碼之 interfaces 必須包含 cli_client。"""
    by = _ec_by_code()
    for code in ("TIMEOUT", "SOCKET_ERROR", "EMPTY_RESPONSE", "INVALID_RESPONSE"):
        assert code in by
        assert "cli_client" in by[code].scope.interfaces


def test_ec_retryable_frozen_values() -> None:
    """關鍵錯誤碼之 retryable 凍結值比對。"""
    by = _ec_by_code()
    assert by["AUTOBOOT_QUIET"].retryable == "yes"
    assert by["READY_UNCONFIRMED"].retryable == "no"
    assert by["SEND_FAILED"].retryable == "no"
    assert by["TIMEOUT"].retryable == "conditional"
    assert by["FLUSHED_BY_RECOVERY"].retryable == "yes"
    assert by["INVALID_JSON"].retryable == "fix_args"
    assert by["INTERNAL_ERROR"].retryable == "no"
    assert by["DAEMON_NOT_READY"].retryable == "conditional"


def test_ec_uart_tx_gate_rejects_are_none() -> None:
    """閘門拒絕與未發送即清除之碼 uart_tx 為 none，特定情境為 confirmed 或 possible。"""
    by = _ec_by_code()
    none_codes = (
        "SESSION_NOT_READY",
        "AUTOBOOT_QUIET",
        "READY_UNCONFIRMED",
        "CMD_TOO_LONG",
        "CMD_CONTAINS_NEWLINE",
        "SESSION_QUEUE_FULL",
        "FLUSHED_BY_RECOVERY",
    )
    for code in none_codes:
        assert by[code].uart_tx == "none"
    assert by["PROMPT_TIMEOUT"].uart_tx == "confirmed"
    assert by["SEND_FAILED"].uart_tx == "possible"


def test_ec_unreachable_set() -> None:
    """EXISTING 不可達碼集合精確凍結，且所有 APPROVED 碼必須不可達。"""
    by = _ec_by_code()
    unreachable_existing = {c for c, e in by.items() if not e.reachable and e.status == "EXISTING"}
    assert unreachable_existing == {"FLASH_TARGET_IS_CONSOLE", "UNKNOWN_EVENT_CMD", "COMMAND_FAILED"}
    approved = {c for c, e in by.items() if e.status == "APPROVED"}
    unreachable = {c for c, e in by.items() if not e.reachable}
    assert approved <= unreachable


def test_ec_ok_response_codes() -> None:
    """出現在 ok:true 回應中的代碼其 retryable 必須為 no。"""
    by = _ec_by_code()
    ok_response_entries = [e for e in by.values() if e.ok_response is True]
    assert len(ok_response_entries) > 0, "應至少有一筆 ok_response 為 true 的代碼"
    for e in ok_response_entries:
        assert e.retryable == "no"


def test_ec_shape_quirks_frozen() -> None:
    """特定錯誤碼之 extra_fields 形狀特性凍結。"""
    by = _ec_by_code()
    assert "error" in by["INVALID_RULE_SCHEMA"].extra_fields
    assert "detail" in by["LOG_OPEN_FAILED"].extra_fields
    assert "retry_after_s" in by["AUTOBOOT_QUIET"].extra_fields
    assert "retry_after_s" not in by["READY_UNCONFIRMED"].extra_fields


def test_ec_exit_codes_frozen() -> None:
    """CLI local 錯誤碼之 exit_codes 映射凍結。"""
    by = _ec_by_code()
    assert by["REMOTE_NOT_SUPPORTED"].exit_codes == {"daemon start": 2, "connect": 1, "benches": 1, "remote": 1}
    assert by["INVALID_COMPLETION_REGEX"].exit_codes["cmd submit"] == 1
    assert by["SAMPLE_READ_FAILED"].exit_codes == {"profile test": 0}
    assert {"event", "daemon stop", "connect", "benches", "remote"} <= set(by["INTERNAL_ERROR"].exit_codes)
    assert by["INVALID_ARGS"].exit_codes["argparse-boundary"] == 1


def test_ec_non_codes_absent() -> None:
    """非錯誤碼之輔助或警告代碼不得收錄於 error_codes 目錄。"""
    by = _ec_by_code()
    non_codes = {"CMD_LENGTH_WARNING", "NOT_BOOTLOADER", "INVALID_INTERACTIVE_ENCODING", "EMPTY_COMMAND"}
    assert not (non_codes & set(by))


def test_ec_constants_recorded() -> None:
    """對應之 Python 常數名稱正確記錄於目錄 entry。"""
    by = _ec_by_code()
    assert by["CMD_TOO_LONG"].constant == "ERROR_CMD_TOO_LONG"
    assert by["INVALID_COMPLETION_REGEX"].constant == "ERROR_INVALID_COMPLETION_REGEX"


# ==============================================================================
# Task T4: session_last_errors.yaml 與 diagnostic_aux.yaml 真實目錄測試
# ==============================================================================


def _le():
    return _load().load_session_last_errors(ROOT)


def _le_by_val():
    return {e.value: e for e in _le()}


def _aux():
    return _load().load_aux(ROOT)


def test_le_real_dynamic_prefixes() -> None:
    """動態前綴 last_error 項目集合精確比對。"""
    import types

    cat = types.SimpleNamespace(last_errors=_le())
    assert {v.value for v in cat.last_errors if v.kind == "dynamic_prefix"} == {
        "REPROBE_FAILED:*",
        "ATTACH_FAILED:*",
        "BRIDGE_DOWN:*",
    }


def test_le_real_credentials_unresolved_frozen() -> None:
    """CREDENTIALS_UNRESOLVED 之屬性凍結（terminal、禁止自動重探、獨立命名空間、logging 與手動恢復）。"""
    by = _le_by_val()
    assert "CREDENTIALS_UNRESOLVED" in by
    entry = by["CREDENTIALS_UNRESOLVED"]
    assert entry.terminal is True
    assert entry.auto_reprobe_attached is False
    assert entry.auto_reprobe_detached is False
    assert entry.error_code_namespace == "forbidden"
    assert "credentials_unresolved" in entry.logging
    assert "session attach" in entry.manual_recovery


def test_le_real_user_env_missing_unreachable() -> None:
    """USER_ENV_MISSING 為不可達（reachable is False），且附有非空 unreachable_reason。"""
    by = _le_by_val()
    assert "USER_ENV_MISSING" in by
    entry = by["USER_ENV_MISSING"]
    assert entry.reachable is False
    assert bool(entry.unreachable_reason)


def test_le_real_prompt_unavailable_reprobe_attached_only() -> None:
    """PROMPT_UNAVAILABLE 僅在 ATTACHED 狀態下自動重探。"""
    by = _le_by_val()
    assert "PROMPT_UNAVAILABLE" in by
    entry = by["PROMPT_UNAVAILABLE"]
    assert entry.auto_reprobe_attached is True
    assert entry.auto_reprobe_detached is False


def test_le_real_transport_stall_detail_frozen() -> None:
    """TRANSPORT_STALL 之 detail 與 logging 內容凍結。"""
    by = _le_by_val()
    assert "TRANSPORT_STALL" in by
    entry = by["TRANSPORT_STALL"]
    assert "transport_stall_hint" in entry.detail
    assert "transport_stall_suspected" in entry.logging
    assert "WARNING" in entry.logging


def test_le_real_bootloader_stuck_detail() -> None:
    """BOOTLOADER_STUCK 之 detail 欄位包含 matched_prompt=。"""
    by = _le_by_val()
    assert "BOOTLOADER_STUCK" in by
    entry = by["BOOTLOADER_STUCK"]
    assert "matched_prompt=" in entry.detail


def test_le_real_promoted_attach_subset() -> None:
    """提升至 RPC error_code 的 last_error 子集驗證。"""
    by = _le_by_val()
    for code in ("PROMPT_UNAVAILABLE", "CREDENTIALS_UNRESOLVED", "RX_FLOOD"):
        assert by[code].promoted_via == ("session.attach", "session.recover")
    for code in ("CLEARED", "RELEASED", "REBOUND"):
        assert by[code].promoted_via == ()


def test_le_real_reprobe_matches_runtime_rule(tmp_path: Path) -> None:
    """named last_error 之 auto_reprobe 欄位與 SessionManager 執行期邏輯逐值一致。"""
    from sw_core.session_manager import SessionManager
    from sw_core.wal import WalWriter

    sm = SessionManager(
        [],
        WalWriter(wal_dir=str(tmp_path)),
        on_ready=lambda _sid: None,
        on_detached=lambda _sid: None,
    )
    by = _le_by_val()
    named_entries = [e for e in by.values() if e.kind == "named"]
    assert len(named_entries) > 0
    for entry in named_entries:
        assert sm._is_reprobe_prompt_error("ATTACHED", entry.value) == entry.auto_reprobe_attached, (
            f"{entry.value} in ATTACHED mismatch"
        )
        assert sm._is_reprobe_prompt_error("DETACHED", entry.value) == entry.auto_reprobe_detached, (
            f"{entry.value} in DETACHED mismatch"
        )


def test_le_real_console_unlock_prompt_timeout_not_terminal() -> None:
    """CONSOLE_UNLOCK_PROMPT_TIMEOUT 非終態，且兩狀態皆具備自動重探資格。"""
    by = _le_by_val()
    assert "CONSOLE_UNLOCK_PROMPT_TIMEOUT" in by
    entry = by["CONSOLE_UNLOCK_PROMPT_TIMEOUT"]
    assert entry.terminal is False
    assert entry.auto_reprobe_attached is True
    assert entry.auto_reprobe_detached is True


def test_le_real_cleared_by_present() -> None:
    """每個 last_error 值之 cleared_by 皆非空，且 CREDENTIALS_UNRESOLVED 僅包含操作者清除項。"""
    entries = _le()
    for entry in entries:
        assert entry.cleared_by, f"{entry.value} 之 cleared_by 不得為空"
    by = {e.value: e for e in entries}
    cu = by["CREDENTIALS_UNRESOLVED"]
    assert all(item.startswith("operator:") for item in cu.cleared_by)
    assert "operator:session.attach" in cu.cleared_by
    assert "operator:session.recover" in cu.cleared_by


def test_le_real_not_in_error_code_namespace() -> None:
    """所有標記為 forbidden 之 last_error 值均不得出現在 error_codes 目錄中。"""
    cat = _load()
    ec_codes, _ = cat.load_error_codes(ROOT)
    ec_names = {e.code for e in ec_codes}
    entries = _le()
    forbidden = [e.value for e in entries if e.error_code_namespace == "forbidden"]
    assert len(forbidden) > 0
    for val in forbidden:
        assert val not in ec_names


def test_aux_real_classifications() -> None:
    """診斷分類之唯一性、特定項目動作及 recommended_actions 覆蓋性驗證。"""
    aux = _aux()

    def _c_val(c):
        return getattr(c, "value", c[0])

    def _c_acts(c):
        return getattr(c, "recommended_actions", c[1])

    def _ra_val(r):
        return getattr(r, "value", r[0])

    c_vals = [_c_val(c) for c in aux.classifications]
    assert len(set(c_vals)) == len(c_vals), "classifications 值必須唯一"

    by_val = {_c_val(c): _c_acts(c) for c in aux.classifications}
    assert "BOOTLOADER" in by_val
    assert tuple(by_val["BOOTLOADER"]) == ("recover_interactive",)
    assert "OK" in by_val
    assert tuple(by_val["OK"]) == ("none",)

    rec_actions = {_ra_val(r) for r in aux.recommended_actions}
    for acts in by_val.values():
        for act in acts:
            assert act in rec_actions, f"classification action {act} 未在 aux.recommended_actions 中"


def test_aux_real_recommended_actions_cover_emitters() -> None:
    """程式碼中所有 recommended_action 輸出點之字面值必須收錄於 aux.recommended_actions。"""
    import ast

    def _extract_constants(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return _extract_constants(node.body) | _extract_constants(node.orelse)
        return set()

    found_actions: set[str] = set()
    for py_file in sorted((ROOT / "sw_core").glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "recommended_action":
                        found_actions |= _extract_constants(v)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "recommended_action"
                    ):
                        found_actions |= _extract_constants(node.value)

    assert len(found_actions) > 0, "應至少掃描出若干 recommended_action 輸出點"
    aux = _aux()
    aux_actions = {getattr(r, "value", r[0]) for r in aux.recommended_actions}
    assert found_actions <= aux_actions, f"未收錄的 recommended_actions: {found_actions - aux_actions}"


def test_aux_real_warning_not_error_code() -> None:
    """CMD_LENGTH_WARNING 僅存在於 aux.warning_codes，不得出現於 error_codes。"""
    aux = _aux()
    warn_codes = {getattr(w, "code", w[0]) for w in aux.warning_codes}
    assert "CMD_LENGTH_WARNING" in warn_codes

    cat = _load()
    ec_codes, _ = cat.load_error_codes(ROOT)
    ec_names = {e.code for e in ec_codes}
    assert "CMD_LENGTH_WARNING" not in ec_names


# ==============================================================================
# Task T5: contracts/behavior_changes.yaml（BC／SC／DF 登記）真實目錄測試
# ==============================================================================


def _bc():
    return _load().load_behavior_changes(ROOT)


def _bc_changes():
    return _bc()[0]


def _bc_by_id():
    return {c.id: c for c in _bc_changes()}


def test_bc_real_five_bcs_registered() -> None:
    """五項 BC 全部登記，核准資訊與狀態符合 OD-04 決策。"""
    changes, _, _ = _bc()
    bc_entries = [c for c in changes if c.kind == "BC"]
    assert {c.id for c in bc_entries} == {"BC-01", "BC-02", "BC-03", "BC-04", "BC-05"}
    for c in bc_entries:
        assert c.status != "PROPOSED"
        assert c.approval.decision_ref == "OD-04"
        assert c.approval.date == "2026-10-15"
        assert c.approval.owner == "hamanpaul"


def test_bc_real_approval_owner_by_basis() -> None:
    """每筆變更之 approval.owner 符合 basis 映射，SC-19 為 OD-11，DF 均為 reconciliation。"""
    cat = _load()
    mapping = cat.APPROVAL_OWNER_BY_BASIS
    changes, _, _ = _bc()
    by_id = {c.id: c for c in changes}
    for c in changes:
        assert c.approval.owner == mapping[c.approval.basis], (
            f"{c.id} approval.owner mismatch: expected {mapping[c.approval.basis]}, got {c.approval.owner}"
        )
    sc19 = by_id["SC-19"]
    assert sc19.approval.basis == "owner_decision"
    assert sc19.approval.decision_ref == "OD-11"
    df_entries = [c for c in changes if c.kind == "DF"]
    assert len(df_entries) > 0
    for df in df_entries:
        assert df.approval.basis == "reconciliation"


def test_bc_real_bc_case_ids_frozen() -> None:
    """BC-03 案例 ID 凍結；BC-01 包含 oracle progress 案例與 OD-13 關聯決策。"""
    by_id = _bc_by_id()
    assert by_id["BC-03"].case_ids == (
        "bc03-aborted-in-flight-after-1byte",
        "bc03-unknown-outcome-send-failed",
        "bc03-no-auto-resend",
    )
    assert "bc01-f12-oracle-progress" in by_id["BC-01"].case_ids
    assert "OD-13" in by_id["BC-01"].related_decisions


def test_bc_real_sc_df_ranges() -> None:
    """SC-01~SC-27 與 DF-01~DF-17、DF-19 編號範圍凍結，DF-19 屬性完備。"""
    changes, _, _ = _bc()
    by_id = {c.id: c for c in changes}
    sc_ids = {c.id for c in changes if c.kind == "SC"}
    assert sc_ids == {f"SC-{i:02d}" for i in range(1, 28)}
    df_ids = {c.id for c in changes if c.kind == "DF"}
    assert df_ids == {f"DF-{i:02d}" for i in range(1, 18)} | {"DF-19"}

    df19 = by_id["DF-19"]
    assert df19.scope == "both"
    assert list(df19.phases) == ["P1", "P5", "P7"]
    assert df19.case_ids == ("df19-self-test-probe-quiet-fence",)
    assert "SES-16.6-08" in df19.reconciliation_refs
    assert df19.applies_rust_stage == "slice-2.1"


def test_bc_real_rust_only_python_null() -> None:
    """scope 為 rust_only 者 applies_python_min 為 None，特定 SC 項目為 rust_only。"""
    changes, _, _ = _bc()
    by_id = {c.id: c for c in changes}
    for c in changes:
        if c.scope == "rust_only":
            assert c.applies_python_min is None, f"{c.id} applies_python_min 應為 None"
    for sc_id in ("SC-15", "SC-17", "SC-18", "SC-22", "SC-23", "SC-24", "SC-26"):
        assert by_id[sc_id].scope == "rust_only", f"{sc_id} scope 應為 rust_only"
    assert by_id["SC-25"].scope in {"rust_only", "python_when_manifest_present"}


def test_bc_real_sc22_switch_guard_not_force_overridable() -> None:
    """SC-22 supervision-mode set 護欄不可由 --force 覆寫，且停止必經交接（R1-F3）。"""
    by_id = _bc_by_id()
    sc22 = by_id["SC-22"]
    assert sc22.scope == "rust_only"
    assert any("§12.1" in ref for ref in sc22.spec_refs)
    assert "daemon.handoff_stop" in sc22.approval_conditions
    assert "supervision_set_safety_check" in sc22.approval_conditions
    assert "--force" in sc22.approval_conditions
    assert "force_ignored" in sc22.compatibility_impact
    assert sc22.case_ids == ("sc22-supervision-mode-set",)


def test_bc_real_ab_participation() -> None:
    """每筆變更之 ab_participation 與 scope 對應一致（both 為 diff，其餘為 absolute_only）。"""
    changes, _, _ = _bc()
    for c in changes:
        expected = "diff" if c.scope == "both" else "absolute_only"
        assert c.ab_participation == expected, (
            f"{c.id} ab_participation 期望為 {expected}，實際為 {c.ab_participation}"
        )


def test_bc_real_no_contract_revision_key() -> None:
    """contracts/behavior_changes.yaml 原始 YAML 文字不得包含 contract_revision 鍵。"""
    path = ROOT / "contracts" / "behavior_changes.yaml"
    text = path.read_text(encoding="utf-8")
    assert "contract_revision:" not in text


def test_bc_real_safety_invariants() -> None:
    """六項安全不變量清單及其關聯錯誤碼集合精確比對。"""
    cat = _load()
    _, _, invariants = _bc()
    assert [i.id for i in invariants] == list(cat.SAFETY_INVARIANTS)
    inv_by_id = {i.id: i for i in invariants}

    ec_codes, _ = cat.load_error_codes(ROOT)
    expected_zero_tx = {
        c.code
        for c in ec_codes
        if c.uart_tx == "none" and ({"rpc", "rec"} & set(c.scope.interfaces))
    }
    assert set(inv_by_id["zero_tx_on_reject"].error_codes) == expected_zero_tx

    expected_no_auto_resend = {
        c.code for c in ec_codes if c.uart_tx in {"possible", "confirmed"}
    }
    assert set(inv_by_id["no_auto_resend"].error_codes) == expected_no_auto_resend


def test_bc_real_pending_items() -> None:
    """待擁有者確認項目與其對應之 owner_item 及編號清單驗證。"""
    changes, confirmations, _ = _bc()
    pending = {c.id: c.owner_item for c in changes if c.owner_confirmation == "pending_default"}
    assert pending == {"SC-08": 3, "SC-04": 4, "DF-02": 5, "SC-26": 6, "SC-27": 7}
    assert [o.item for o in confirmations] == [1, 2, 3, 4, 5, 6, 7]


def test_bc_real_case_ids_globally_unique() -> None:
    """behavior_changes 中所有案例 ID（case_ids）全域唯一，無重複定義。"""
    changes, _, _ = _bc()
    all_case_ids = [cid for c in changes for cid in c.case_ids]
    assert len(all_case_ids) == len(set(all_case_ids)), (
        f"存在重複的 case_ids: {[cid for cid in all_case_ids if all_case_ids.count(cid) > 1]}"
    )


def test_bc_real_error_code_refs_resolve() -> None:
    """error_codes.yaml 中每筆 behavior_change 參照之 ID 均存在於 behavior_changes 目錄。"""
    cat = _load()
    ec_codes, _ = cat.load_error_codes(ROOT)
    changes, _, _ = _bc()
    change_ids = {c.id for c in changes}
    for ec in ec_codes:
        for ref in ec.behavior_change:
            assert ref in change_ids, (
                f"error_code {ec.code} 參照未知的 behavior_change ID: {ref}"
            )


def test_bc_real_sc23_sc18_autostart_hold_registered() -> None:
    """SC-23 與 SC-18 登記交接拉起抑制（R5-F1）護欄、條件與案例 ID。"""
    by_id = _bc_by_id()
    sc23 = by_id["SC-23"]
    for needle in (
        "RefuseManualStart=yes",
        "Restart=no",
        "90-serialwrap-update-hold.conf",
        "重新交接",
        "update_autostart_hold",
    ):
        assert needle in sc23.compatibility_impact, f"SC-23 compatibility_impact 缺少 '{needle}'"
    assert "§12.1 步驟 3" in sc23.approval_conditions
    assert sc23.scope == "rust_only"
    assert sc23.case_ids == ("sc23-setup-rollback",)

    sc18 = by_id["SC-18"]
    for needle in ("install-journal.json", "autostart.lock"):
        assert needle in sc18.compatibility_impact, f"SC-18 compatibility_impact 缺少 '{needle}'"
    assert sc18.scope == "rust_only"
    assert sc18.case_ids == ("sc18-windows-ondemand-autostart",)
