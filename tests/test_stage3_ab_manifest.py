"""P9-T3：A/B manifest 載入、凍結與案例涵蓋檢查（tools/stage3/ab_manifest.py）的測試（TDD 紅燈）。

本檔只依規格（docs/rust-refactor/plans/10-stage3-canary.md Task 3）撰寫；實作與 Task 1／Task 2 替身尚未存在。
所有 tools.stage3 與 stage3_fakes 的匯入皆為測試函式內的延遲匯入，使整套 collect 不因前置 Task 缺席而中斷。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os

import pytest
import yaml

# 只用行程內物件與暫存檔；不開 socket、子行程、PTY 或裝置。
pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

GIT_KEY = "git:rev-parse HEAD:contracts"


# ---- 延遲匯入（Task 1 模組與 Task 2 替身；前置缺席時只影響執行的測試，不影響 collect）----
def _fk():
    import stage3_fakes  # noqa: PLC0415

    return stage3_fakes


def _am():
    from tools.stage3 import ab_manifest  # noqa: PLC0415

    return ab_manifest


def _he():
    from tools.stage3.reason_codes import HarnessError  # noqa: PLC0415

    return HarnessError


# ---- 測試輔助 ----
def _runner(rev: str, docker_id: str | None = None):
    """回傳腳本化 runner：git rev-parse 回 rev；可選 docker image inspect 回 docker_id。"""
    script = {GIT_KEY: [{"rc": 0, "stdout": rev + "\n", "stderr": ""}]}
    if docker_id is not None:
        script["docker:image inspect"] = [{"rc": 0, "stdout": docker_id + "\n", "stderr": ""}]
    return _fk().RecordingRunner(script)


def _git_ok(rev: str):
    return _runner(rev)


def _load(path):
    return _am().load_manifest(path)


def _verify(m, tmp_path):
    return _am().verify_frozen(m, repo_root=tmp_path, run=_git_ok(m.contract_revision))


def _cases_by_id(tmp_path, m) -> dict:
    """讀取 cases.yaml（P2 結構：頂層 cases 清單，每案含 case_id、invariants、destructive）。"""
    doc = yaml.safe_load((tmp_path / m.case_list.path).read_text(encoding="utf-8"))
    return {c["case_id"]: c for c in doc["cases"]}


def _edit_manifest(path, mutate) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _build_manifest(tmp_path, overrides: dict):
    """以 write_ab_manifest 建 ab-manifest；sides.1.cli_sha_override 由測試側套用到 B 的 release-manifest。"""
    overrides = dict(overrides)
    cli_sha = overrides.pop("sides.1.cli_sha_override", None)
    path = _fk().write_ab_manifest(tmp_path, **overrides)
    if cli_sha is not None:
        doc = json.loads(path.read_text(encoding="utf-8"))
        rm = tmp_path / doc["sides"][1]["release_manifest"]["path"]
        md = json.loads(rm.read_text(encoding="utf-8"))
        md["components"]["cli"]["sha256"] = cli_sha  # B 的 CLI 與 A 不同
        rm.write_text(json.dumps(md), encoding="utf-8")
    return path


# ---- 凍結與竄改偵測 ----
def test_frozen_manifest_has_no_violations(tmp_path):
    m = _load(_fk().write_ab_manifest(tmp_path))
    assert _verify(m, tmp_path) == []


def test_case_list_drift_detected(tmp_path):
    m = _load(_fk().write_ab_manifest(tmp_path))
    p = tmp_path / m.case_list.path
    p.write_text(p.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    v = _verify(m, tmp_path)
    assert f"case_list sha256 mismatch: {m.case_list.path}" in v


def test_candidate_launcher_swapped_detected(tmp_path):
    m = _load(_fk().write_ab_manifest(tmp_path))
    (m.sides[1].bundle_dir / "bin" / "serialwrapd").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    v = _verify(m, tmp_path)
    assert "release component sha256 mismatch: side B daemon.launcher" in v


def test_baseline_python_tree_tamper_detected(tmp_path):
    m = _load(_fk().write_ab_manifest(tmp_path))
    victim = next((m.sides[0].bundle_dir / "py").rglob("*.py"))
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# tamper\n", encoding="utf-8")
    v = _verify(m, tmp_path)
    assert "release tree mismatch: side A daemon.package" in v


def test_trimmed_case_list_rejected(tmp_path):
    path = _fk().write_ab_manifest(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    dropped = doc["case_ids"].pop()
    path.write_text(json.dumps(doc), encoding="utf-8")
    m = _load(path)
    assert f"required case missing: {dropped}" in _am().verify_case_coverage(m, repo_root=tmp_path)


def test_stage3_requires_python_baseline_daemon(tmp_path):
    path = _fk().write_ab_manifest(tmp_path, stage="3")
    doc = json.loads(path.read_text(encoding="utf-8"))
    a_manifest = tmp_path / doc["sides"][0]["release_manifest"]["path"]
    md = json.loads(a_manifest.read_text(encoding="utf-8"))
    md["components"]["daemon"]["implementation"] = "rust"
    a_manifest.write_text(json.dumps(md), encoding="utf-8")
    with pytest.raises(_he()) as ei:
        _load(path)
    assert ei.value.code == "HARNESS_MANIFEST_INVALID"


# ---- load_manifest 拒絕規則（參數化表）----
LOAD_REJECT_CASES = [
    ("a_not_baseline", {"sides.0.role": "candidate"}),
    ("l3_missing_acceptance", {"layer": "l3", "acceptance": None}),
    ("l3_missing_endpoint", {"layer": "l3", "endpoint": None}),
    ("l3_failure_retention_systemd", {"layer": "l3", "failure_retention": "systemd-user"}),
    ("l2_without_activate", {"layer": "l2", "sides.0.activate_argv": []}),
    ("l15_with_hardware", {"dut.kind": "hardware"}),
    ("adapter_layer_mismatch", {"suite.adapter": "l2_regression"}),
    ("docker_without_digest", {"dut.kind": "docker", "dut.image_digest": None}),
    ("cli_exe_not_component", {"sides.1.cli_exe": "/usr/bin/serialwrap"}),
    ("reverify_wrong_order", {"reverify.enabled": True, "reverify.order": ["A", "B"]}),
    ("unknown_template_key", {"suite.extra_args": ["{python}"]}),
    ("stage2_cli_differs", {"stage": "2.1", "sides.1.cli_sha_override": "0" * 64}),
    ("stage3_wrong_case_set", {"required_case_set": "slice_2.1"}),
]


@pytest.mark.parametrize(
    "overrides",
    [o for _, o in LOAD_REJECT_CASES],
    ids=[n for n, _ in LOAD_REJECT_CASES],
)
def test_load_rejects(tmp_path, overrides):
    path = _build_manifest(tmp_path, overrides)
    with pytest.raises(_he()) as ei:
        _load(path)
    assert ei.value.code == "HARNESS_MANIFEST_INVALID"


# ---- 其餘 verify_frozen／render／runtime_digest 測試 ----
def test_render_argv_substitutes(tmp_path):
    assert _am().render_argv(["{cli_exe}", "x"], {"cli_exe": "/b"}) == ["/b", "x"]


def test_contract_revision_mismatch(tmp_path):
    m = _load(_fk().write_ab_manifest(tmp_path))
    other = ("0" * 40) if m.contract_revision != "0" * 40 else ("1" * 40)
    v = _am().verify_frozen(m, repo_root=tmp_path, run=_git_ok(other))
    assert any(s.startswith("contract_revision mismatch:") for s in v)


def test_docker_digest_mismatch(tmp_path):
    path = _fk().write_ab_manifest(
        tmp_path,
        **{
            "dut.kind": "docker",
            "dut.image": "serialwrap-dut:test",
            "dut.image_digest": "sha256:" + "a" * 64,
        },
    )
    m = _load(path)
    v = _am().verify_frozen(m, repo_root=tmp_path, run=_runner(m.contract_revision, "sha256:" + "b" * 64))
    assert "dut image digest mismatch" in v


def test_tool_tree_drift(tmp_path):
    m = _load(_fk().write_ab_manifest(tmp_path))
    p = tmp_path / "func-test" / "runner.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    assert "tool tree mismatch: func-test" in _verify(m, tmp_path)


def test_invariant_not_covered(tmp_path):
    path = _fk().write_ab_manifest(tmp_path)
    m = _load(path)
    cases = _cases_by_id(tmp_path, m)
    refs = [cid for cid in m.case_ids if "no_auto_resend" in (cases.get(cid, {}).get("invariants") or [])]
    assert refs, "fixture 前提：case_ids 須含引用 no_auto_resend 的案例"
    _edit_manifest(path, lambda d: d.__setitem__("case_ids", [c for c in d["case_ids"] if c not in refs]))
    m2 = _load(path)
    assert "invariant not covered: no_auto_resend" in _am().verify_case_coverage(m2, repo_root=tmp_path)


def test_destructive_needs_authorization(tmp_path):
    path = _fk().write_ab_manifest(tmp_path, layer="l2", authorization=None)
    m = _load(path)
    cases = _cases_by_id(tmp_path, m)
    destructive = sorted(cid for cid, c in cases.items() if c.get("destructive") is True)
    assert destructive, "fixture 前提：cases.yaml 須含 destructive 案例"
    target = destructive[0]

    def _add_target(d):
        if target not in d["case_ids"]:
            d["case_ids"] = list(d["case_ids"]) + [target]

    _edit_manifest(path, _add_target)
    m2 = _load(path)
    assert f"destructive case without authorization: {target}" in _am().verify_case_coverage(m2, repo_root=tmp_path)


def test_windows_com0com_requires_allowlist(tmp_path):
    path = _fk().write_ab_manifest(
        tmp_path,
        platform="windows",
        **{
            "dut.transport": "com0com-pair",
            "dut.com_pair": {"daemon_side": "COM20", "bridge_side": "COM21"},
            "test_device_allowlist": [],
        },
    )
    m = _load(path)
    v = _am().verify_case_coverage(m, repo_root=tmp_path)
    assert "windows com0com instance without test_device_allowlist" in v


def test_runtime_digest_ignores_stage_and_marker(tmp_path):
    fk = _fk()
    am = _am()
    b_a = fk.make_bundle(tmp_path / "a", stage="2.4")
    b_b = fk.make_bundle(tmp_path / "b", stage="3")
    doc_a = json.loads((b_a / "release-manifest.json").read_text(encoding="utf-8"))
    doc_b = json.loads((b_b / "release-manifest.json").read_text(encoding="utf-8"))
    assert am.runtime_digest(doc_a) == am.runtime_digest(doc_b)

    # 換 CLI binary（同時更新宣告的 sha256）→ digest 必須不同
    cli_b = b_b / "bin" / "serialwrap"
    cli_b.write_bytes(b"#!/bin/sh\necho changed\n")
    doc_c = copy.deepcopy(doc_b)
    doc_c["components"]["cli"]["sha256"] = hashlib.sha256(cli_b.read_bytes()).hexdigest()
    assert am.runtime_digest(doc_c) != am.runtime_digest(doc_a)


# ---- R2-F7：verify_bundle／bundle_tree_sha256／runtime_tree_sha256 ----
def test_verify_bundle_strings_match_verify_frozen(tmp_path):
    am = _am()
    m = _load(_fk().write_ab_manifest(tmp_path))
    b = m.sides[1].bundle_dir
    (b / "bin" / "serialwrapd").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    expected = "release component sha256 mismatch: side B daemon.launcher"
    assert am.verify_bundle(b, m.sides[1].manifest_doc, label="side B") == [expected]
    assert expected in _verify(m, tmp_path)


def test_verify_bundle_missing_cli(tmp_path):
    fk = _fk()
    am = _am()
    b = fk.make_bundle(tmp_path / "b", stage="3")
    doc = json.loads((b / "release-manifest.json").read_text(encoding="utf-8"))
    (b / "bin" / "serialwrap").unlink()
    assert am.verify_bundle(b, doc, label="side A") == [f"file missing: {b / 'bin' / 'serialwrap'}"]


def test_verify_bundle_path_escape(tmp_path):
    fk = _fk()
    am = _am()
    b = fk.make_bundle(tmp_path / "b", stage="3")
    doc = json.loads((b / "release-manifest.json").read_text(encoding="utf-8"))
    doc["components"]["cli"]["path"] = "../x"
    assert "release component path escapes bundle: side A cli" in am.verify_bundle(b, doc, label="side A")


def test_bundle_tree_sha256_covers_every_file(tmp_path):
    am = _am()
    b = _fk().make_bundle(tmp_path / "b", stage="3")
    skill = b / "assets" / "skill" / "SKILL.md"
    pyc = b / "bin" / "__pycache__" / "x.pyc"
    sums = b / "SHA256SUMS"

    def _add_skill():
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("v1\n", encoding="utf-8")

    def _modify_skill():
        skill.write_text("v2\n", encoding="utf-8")

    def _delete_skill():
        skill.unlink()

    def _add_pyc():
        pyc.parent.mkdir(parents=True, exist_ok=True)
        pyc.write_bytes(b"\x00pyc")

    def _modify_sums():
        sums.write_text(sums.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")

    prev = am.bundle_tree_sha256(b)  # 基準 d0
    for step in (_add_skill, _modify_skill, _delete_skill, _add_pyc, _modify_sums):
        step()
        cur = am.bundle_tree_sha256(b)
        assert cur != prev, f"digest 未因 {step.__name__} 改變"
        prev = cur

    # 檔案建立順序不同、內容相同的樹 → digest 相同
    content = {"a.txt": b"A", "b.txt": b"B", "sub/c.txt": b"C"}
    t1 = tmp_path / "t1"
    t2 = tmp_path / "t2"
    for root, order in ((t1, ["a.txt", "sub/c.txt", "b.txt"]), (t2, ["b.txt", "sub/c.txt", "a.txt"])):
        for rel in order:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content[rel])
    assert am.bundle_tree_sha256(t1) == am.bundle_tree_sha256(t2)


def test_bundle_tree_sha256_rejects_symlink(tmp_path):
    # core_portable 不得以平台 skipif 排除（test_resource_markers 守門）；改為能力檢查：
    # 主機無法建立 symlink（如 Windows 未開開發人員模式）才於執行期 skip。
    am = _am()
    b = _fk().make_bundle(tmp_path / "b", stage="3")
    try:
        os.symlink(b / "bin" / "serialwrap", b / "bin" / "link")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"此主機無法建立 symlink：{exc}")
    with pytest.raises(_he()) as ei:
        am.bundle_tree_sha256(b)
    assert ei.value.code == "HARNESS_ARTIFACT_MISMATCH"


def test_runtime_tree_sha256_ignores_only_manifest_metadata(tmp_path):
    am = _am()
    b = _fk().make_bundle(tmp_path / "b", stage="3")
    manifest_path = b / "release-manifest.json"
    sums_path = b / "SHA256SUMS"
    orig_manifest = manifest_path.read_bytes()
    orig_sums = sums_path.read_bytes()
    doc = json.loads(orig_manifest.decode("utf-8"))
    d_before = am.runtime_digest(doc)
    r0 = am.runtime_tree_sha256(b)
    t0 = am.bundle_tree_sha256(b)

    # 只改中繼欄位：created_at、stage 與 SHA256SUMS → runtime_tree 不變，bundle_tree 改變
    mutated = dict(doc)
    mutated["created_at"] = "2030-01-01T00:00:00+00:00"
    mutated["stage"] = "2.4"
    manifest_path.write_text(json.dumps(mutated), encoding="utf-8")
    sums_path.write_text("0" * 64 + "  bogus\n", encoding="utf-8")
    assert am.runtime_tree_sha256(b) == r0
    assert am.bundle_tree_sha256(b) != t0

    # 還原後新增未列於 manifest 的旁載檔 → verify_bundle 仍通過，但 runtime_tree 與 bundle_tree 都必須計入
    manifest_path.write_bytes(orig_manifest)
    sums_path.write_bytes(orig_sums)
    assert am.runtime_tree_sha256(b) == r0
    (b / "bin" / "version.dll").write_bytes(b"MZ")
    assert am.verify_bundle(b, doc, label="side B") == []
    assert am.runtime_tree_sha256(b) != r0
    assert am.bundle_tree_sha256(b) != t0
    assert am.runtime_digest(doc) == d_before

    # 非根目錄的 SHA256SUMS 不是中繼檔，必須計入 runtime_tree
    r1 = am.runtime_tree_sha256(b)
    (b / "bin" / "SHA256SUMS").write_text("x\n", encoding="utf-8")
    assert am.runtime_tree_sha256(b) != r1
