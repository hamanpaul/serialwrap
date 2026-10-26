"""A/B 黑箱身分的 config_digest 契約（G4-ab1b-config-digest）。

身分的 `config_digest` 必須反映各夾具實際建立的設定內容（config、profiles 與 env 摘要），而非鎖檔與
applicability 檔的雜湊。涵蓋：
- `tests/contract_blackbox/config_digest.py`：單一隔離根的摘要、逐案例紀錄與彙整（fail-closed）。
- `scripts/rust/ab_blackbox_1b.py`：A／B 兩輪的身分引用各自的設定摘要；lock 與 applicability 雜湊另列獨立欄位。
- `scripts/rust/ab_blackbox.sh`（1A）：同樣改以設定紀錄彙整，不再以 bundle 目錄雜湊冒充 config_digest。

全部在行程內完成（以 monkeypatch 取代 git 與子行程），不啟動 daemon、PTY 或真實裝置。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [
    pytest.mark.core_portable,
    pytest.mark.memory_only,
]

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIGEST = ROOT / "tests" / "contract_blackbox" / "config_digest.py"
BB_HARNESS = ROOT / "tests" / "contract_blackbox" / "bb_harness.py"
EXECUTOR = ROOT / "scripts" / "rust" / "ab_blackbox_1b.py"
SHELL_EXECUTOR = ROOT / "scripts" / "rust" / "ab_blackbox.sh"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"無法載入 {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclass 解析註解時需要模組已登錄於 sys.modules
    spec.loader.exec_module(module)
    return module


def _cd() -> ModuleType:
    """惰性載入 config_digest：紅燈階段模組不存在時只讓用到它的測試失敗。"""
    if not CONFIG_DIGEST.is_file():
        pytest.fail(f"設定摘要模組缺失：{CONFIG_DIGEST}")
    return _load("config_digest_under_test", CONFIG_DIGEST)


def _executor() -> ModuleType:
    return _load("ab_blackbox_1b_identity_under_test", EXECUTOR)


def _write_config(root: Path, *, config_yaml: str, profile_yaml: str | None = None) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "config.yaml").write_text(config_yaml, encoding="utf-8")
    if profile_yaml is not None:
        (root / "live-profiles").mkdir(parents=True, exist_ok=True)
        (root / "live-profiles" / "live.yaml").write_text(profile_yaml, encoding="utf-8")


def _env_for(root: Path, **extra: str) -> dict[str, str]:
    env = {"SERIALWRAP_CONFIG_DIR": str(root / "config")}
    env.update(extra)
    return env


# ── config_digest 模組 ──────────────────────────────────────────────────────


def test_digest_differs_when_config_yaml_differs(tmp_path: Path) -> None:
    """兩個夾具的 config.yaml 內容不同 → 摘要必須不同。"""
    cd = _cd()
    one, two = tmp_path / "one", tmp_path / "two"
    _write_config(one, config_yaml="defaults:\n  timeout_s: 5\n")
    _write_config(two, config_yaml="defaults:\n  timeout_s: 9\n")
    assert cd.fixture_digest(one, _env_for(one))["digest"] != cd.fixture_digest(two, _env_for(two))["digest"]


def test_digest_is_equal_for_same_setting_in_different_roots(tmp_path: Path) -> None:
    """同一設定（含指向隔離根的絕對路徑）於不同 tmp 根 → 摘要相同（根路徑已正規化）。"""
    cd = _cd()
    one, two = tmp_path / "one", tmp_path / "two"
    for root in (one, two):
        _write_config(root, config_yaml=f"defaults:\n  log_dir: {root / 'blog'}\n")
    assert cd.fixture_digest(one, _env_for(one))["digest"] == cd.fixture_digest(two, _env_for(two))["digest"]


def test_digest_differs_when_profile_yaml_differs(tmp_path: Path) -> None:
    """profiles 內容不同 → 摘要必須不同；相同則相同。"""
    cd = _cd()
    one, two, same = tmp_path / "one", tmp_path / "two", tmp_path / "same"
    _write_config(one, config_yaml="x: 1\n", profile_yaml="profiles:\n  live:\n    platform: passthrough\n")
    _write_config(two, config_yaml="x: 1\n", profile_yaml="profiles:\n  live:\n    platform: shell\n")
    _write_config(same, config_yaml="x: 1\n", profile_yaml="profiles:\n  live:\n    platform: passthrough\n")
    digest_one = cd.fixture_digest(one, _env_for(one))["digest"]
    assert digest_one != cd.fixture_digest(two, _env_for(two))["digest"]
    assert digest_one == cd.fixture_digest(same, _env_for(same))["digest"]


def test_env_setting_changes_digest_but_selector_env_does_not(tmp_path: Path) -> None:
    """測試控制的 env 設定進入摘要；A／B 選擇器與證據路徑（由身分欄位另行記錄）不進入摘要。"""
    cd = _cd()
    root = tmp_path / "root"
    _write_config(root, config_yaml="x: 1\n")
    base = cd.fixture_digest(root, _env_for(root, SERIALWRAP_LOG_DIR=str(root / "blog")))["digest"]
    other = cd.fixture_digest(root, _env_for(root, SERIALWRAP_LOG_DIR="/elsewhere"))["digest"]
    assert base != other, "測試設定的 env 差異必須反映在摘要"
    selectors_a = _env_for(
        root, SERIALWRAP_LOG_DIR=str(root / "blog"),
        SERIALWRAP_CLI_EXE="/bin/py-serialwrap", SW_AB_PIDLOG=str(tmp_path / "a.pids"),
    )
    selectors_b = _env_for(
        root, SERIALWRAP_LOG_DIR=str(root / "blog"),
        SERIALWRAP_CLI_EXE="/bundle/bin/serialwrap", SW_AB_PIDLOG=str(tmp_path / "b.pids"),
    )
    assert cd.fixture_digest(root, selectors_a)["digest"] == cd.fixture_digest(root, selectors_b)["digest"]


def test_secret_env_values_never_reach_the_record(tmp_path: Path) -> None:
    """認證類 env 值不得寫入紀錄檔（只保留存在與否）。"""
    cd = _cd()
    root = tmp_path / "root"
    _write_config(root, config_yaml="x: 1\n")
    env = _env_for(root, SW_G5_AB_CHILD_TOKEN="s3cr3t-token-value", SERIALWRAP_TEST_PASS="hunter2-value")
    log = tmp_path / "config.jsonl"
    assert cd.record_items(log, "tests/t.py::test_x", [(root, env)]) == 1
    text = log.read_text(encoding="utf-8")
    assert "s3cr3t-token-value" not in text
    assert "hunter2-value" not in text


def test_record_keeps_per_case_summaries_for_each_fixture(tmp_path: Path) -> None:
    """多個夾具（隔離根）逐案例保存摘要：每個案例的 fixtures 清單對應各自的摘要。"""
    cd = _cd()
    node_one, node_two = "tests/t.py::test_one", "tests/t.py::test_two"
    roots = [tmp_path / f"r{i}" for i in range(3)]
    for index, root in enumerate(roots):
        _write_config(root, config_yaml=f"n: {index}\n")
    log = tmp_path / "config.jsonl"
    cd.record_items(log, node_one, [(roots[0], _env_for(roots[0])), (roots[1], _env_for(roots[1]))])
    cd.record_items(log, node_two, [(roots[2], _env_for(roots[2]))])
    agg = cd.aggregate(log)
    assert agg["entries"] == 3
    assert set(agg["cases"]) == {node_one, node_two}
    fixtures_one = agg["cases"][node_one]["fixtures"]
    assert len(fixtures_one) == 2
    assert {row["digest"] for row in fixtures_one} == {
        cd.fixture_digest(roots[0], _env_for(roots[0]))["digest"],
        cd.fixture_digest(roots[1], _env_for(roots[1]))["digest"],
    }
    assert len(agg["cases"][node_two]["fixtures"]) == 1


def test_aggregate_digest_follows_any_fixture_change(tmp_path: Path) -> None:
    """任一夾具的設定改變 → 彙整摘要改變；設定相同 → 彙整摘要相同。"""
    cd = _cd()

    def build(name: str, timeout: str) -> str:
        root = tmp_path / name
        _write_config(root, config_yaml=f"timeout_s: {timeout}\n")
        log = tmp_path / f"{name}.jsonl"
        cd.record_items(log, "tests/t.py::test_x", [(root, _env_for(root))])
        return cd.aggregate(log)["digest"]

    assert build("base", "5") == build("same", "5")
    assert build("base", "5") != build("changed", "6")


def test_record_is_inert_without_log_path(tmp_path: Path) -> None:
    """未設定紀錄路徑（一般 pytest 執行）時不寫任何檔案。"""
    cd = _cd()
    root = tmp_path / "root"
    _write_config(root, config_yaml="x: 1\n")
    assert cd.record_items(None, "tests/t.py::test_x", [(root, _env_for(root))]) == 0
    assert not any(tmp_path.glob("*.jsonl"))


def test_aggregate_fails_closed_without_records(tmp_path: Path) -> None:
    """紀錄缺失或為空 → ValueError（不得以空摘要放行）。"""
    cd = _cd()
    with pytest.raises(ValueError):
        cd.aggregate(tmp_path / "missing.jsonl")
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        cd.aggregate(empty)


def test_isolated_env_registers_roots_for_the_record_hook(tmp_path: Path) -> None:
    """bb_harness.isolated_env 登記隔離根與其 env，供 conftest 於 call 階段結束時逐案例紀錄。"""
    harness = _load("bb_harness_registry_under_test", BB_HARNESS)
    harness.ISOLATED_ROOTS.clear()
    env = harness.isolated_env(tmp_path / "iso")
    assert harness.ISOLATED_ROOTS, "isolated_env 必須登記隔離根"
    root, registered_env = harness.ISOLATED_ROOTS[-1]
    assert root == tmp_path / "iso"
    assert registered_env is env, "登記的必須是同一個 env 物件，夾具後續修改才會反映在摘要"
    harness.ISOLATED_ROOTS.clear()


# ── 身分與執行器 ────────────────────────────────────────────────────────────


def _patch_identity_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """取代 git 與 release manifest 的外部查詢，讓身分建構留在行程內。"""
    real_check_output = subprocess.check_output

    def fake_check_output(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
        if list(argv[:2]) == ["git", "rev-parse"]:
            return "cafe\n" if "HEAD:contracts" in argv else "deadbeef\n"
        return real_check_output(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    import sw_core.release_manifest as manifest_mod

    monkeypatch.setattr(manifest_mod, "package_tree_sha256", lambda: "tree-sha")


def _bundle_cli(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "release-manifest.json").write_text(
        json.dumps({
            "git_revision": "deadbeef",
            "contract_revision": "cafe",
            "components": {"cli": {"implementation": "rust"}, "daemon": {"package": {"tree_sha256": "tree-sha"}}},
        }),
        encoding="utf-8",
    )
    cli = bundle / "bin" / "serialwrap"
    cli.write_bytes(b"rust cli")
    return cli


def test_identity_builders_reference_config_digest_and_keep_lock_applicability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A／B 身分的 config_digest 等於各自的設定摘要；lock 與 applicability 雜湊另列獨立欄位。"""
    cd = _cd()
    executor = _executor()
    _patch_identity_environment(monkeypatch)
    cli_py = tmp_path / "serialwrap"
    cli_py.write_bytes(b"python cli")
    cli_rust = _bundle_cli(tmp_path)

    digests = []
    for name, timeout in (("one", "5"), ("two", "9")):
        root = tmp_path / name
        _write_config(root, config_yaml=f"timeout_s: {timeout}\n")
        log = tmp_path / f"{name}.jsonl"
        cd.record_items(log, "tests/t.py::test_x", [(root, _env_for(root))])
        digests.append(cd.aggregate(log)["digest"])
    assert digests[0] != digests[1]

    base_identity = executor._baseline_identity(cli_py, "linux", "c" * 64, [], config_digest=digests[0])
    other_identity = executor._baseline_identity(cli_py, "linux", "c" * 64, [], config_digest=digests[1])
    assert base_identity["config_digest"] == digests[0]
    assert other_identity["config_digest"] == digests[1]

    bundle_identity = executor._bundle_identity(cli_rust, "linux", "c" * 64, [], config_digest=digests[1])
    assert bundle_identity["config_digest"] == digests[1]

    expected_lock = hashlib.sha256(executor.LOCK.read_bytes() + executor.APPLICABILITY.read_bytes()).hexdigest()
    assert base_identity["lock_applicability_sha256"] == expected_lock
    assert other_identity["lock_applicability_sha256"] == expected_lock
    assert bundle_identity["lock_applicability_sha256"] == expected_lock


def _write_junit(path: Path, nodes: list[str]) -> None:
    import xml.etree.ElementTree as ET

    suite = ET.Element("testsuite", tests=str(len(nodes)), failures="0", errors="0", skipped="0")
    for node in nodes:
        path_text, *segments = node.split("::")
        classname = ".".join([path_text.removesuffix(".py").replace("/", "."), *segments[:-1]])
        ET.SubElement(suite, "testcase", classname=classname, name=segments[-1])
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def _write_junit_skipped(path: Path, nodes: list[str], message: str) -> None:
    import xml.etree.ElementTree as ET

    suite = ET.Element("testsuite", tests=str(len(nodes)), failures="0", errors="0", skipped=str(len(nodes)))
    for node in nodes:
        path_text, *segments = node.split("::")
        classname = ".".join([path_text.removesuffix(".py").replace("/", "."), *segments[:-1]])
        case = ET.SubElement(suite, "testcase", classname=classname, name=segments[-1])
        ET.SubElement(case, "skipped", type="pytest.skip", message=message)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def _execute_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    write_config: dict[str, str] | None,
    approved_a_skips: bool = False,
) -> tuple[ModuleType, Path, int]:
    """以假的 pytest 子行程驅動 `execute()`。`write_config` 為 {"a": timeout, "b": timeout}；None 表示兩輪都不寫設定紀錄。"""
    cd = _cd()
    executor = _executor()
    _patch_identity_environment(monkeypatch)
    platform = "windows" if os.name == "nt" else "linux"
    nodes = ["t_cfg.py::test_one", "t_cfg.py::test_two"]
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "t_cfg.py").write_text("def test_one():\n    pass\n\n\ndef test_two():\n    pass\n", encoding="utf-8")
    python_cli = "serialwrap.py" if os.name == "nt" else "serialwrap"
    (tmp_path / "repo" / python_cli).write_bytes(b"python cli")  # A 輪使用 repo 根目錄的平台入口
    (tmp_path / "repo" / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    daemon = tmp_path / "py-serialwrapd"
    daemon.write_text("stub\n", encoding="utf-8")
    cli = _bundle_cli(tmp_path)
    manifest = cli.parent.parent / "release-manifest.json"
    out = tmp_path / "ab"
    a_junit, b_junit = out / "a-python.xml", out / "b-rust.xml"
    lock_cases = [{"case_id": "c-cfg", "tests": list(nodes)}]
    seen_env: dict[str, dict[str, str]] = {}

    def fake_run(argv, *, env, timeout):  # type: ignore[no-untyped-def]
        if "--collect-only" in argv:
            return subprocess.CompletedProcess(argv, 0, "\n".join(nodes) + "\n", "")
        if f"--junitxml={a_junit}" in argv or f"--junitxml={b_junit}" in argv:
            is_a = f"--junitxml={a_junit}" in argv
            side = "a" if is_a else "b"
            seen_env[side] = dict(env)
            if is_a and approved_a_skips:
                _write_junit_skipped(a_junit, nodes, "rust-only contract")
            else:
                _write_junit(a_junit if is_a else b_junit, nodes)
            if write_config is not None and side in write_config:
                root = tmp_path / f"{side}-root"
                _write_config(root, config_yaml=f"timeout_s: {write_config[side]}\n")
                cd.record_items(Path(env["SW_AB_CONFIGLOG"]), nodes[0], [(root, _env_for(root))])
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(executor, "ROOT", tmp_path / "repo")
    monkeypatch.setattr(executor, "PY_DAEMON", daemon)
    monkeypatch.setattr(executor, "_frozen_cases", lambda: (lock_cases, sorted(nodes)))
    monkeypatch.setattr(executor, "_frozen_case_map", lambda _lock: [{"case_id": "c-cfg"}])
    monkeypatch.setattr(executor, "_applicable_nodes", lambda found, _platform: list(found))
    monkeypatch.setattr(
        executor, "_approved_python_skips",
        lambda *_args: ({node: "rust-only contract" for node in nodes} if approved_a_skips else {}),
    )
    monkeypatch.setattr(executor, "resolve_manifest", lambda _cli: manifest)
    monkeypatch.setattr(executor, "_release_check", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(executor, "_run", fake_run)
    rc = executor.execute(cli=cli, out_dir=out, platform=platform, timeout=60)
    assert seen_env["a"]["SW_AB_CONFIGLOG"] == str(out / "a-config.jsonl")
    assert seen_env["b"]["SW_AB_CONFIGLOG"] == str(out / "b-config.jsonl")
    return executor, out, rc


def test_execute_wires_each_round_config_log_into_its_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """執行器把 SW_AB_CONFIGLOG 傳給 A、B 兩輪；兩側身分引用各自彙整的設定摘要，並保留證據檔。"""
    cd = _cd()
    executor, out, _rc = _execute_harness(tmp_path, monkeypatch, write_config={"a": "5", "b": "9"})
    identity_a = json.loads((out / "identity-a.json").read_text(encoding="utf-8"))
    identity_b = json.loads((out / "identity-b.json").read_text(encoding="utf-8"))
    assert identity_a["config_digest"] == cd.aggregate(out / "a-config.jsonl")["digest"]
    assert identity_b["config_digest"] == cd.aggregate(out / "b-config.jsonl")["digest"]
    assert identity_a["config_digest"] != identity_b["config_digest"]
    expected_lock = hashlib.sha256(executor.LOCK.read_bytes() + executor.APPLICABILITY.read_bytes()).hexdigest()
    assert identity_a["lock_applicability_sha256"] == expected_lock
    assert identity_b["lock_applicability_sha256"] == expected_lock
    evidence_a = {Path(item["path"]).name for item in identity_a["evidence_files"]}
    assert {"a-config.jsonl", "config-digest-a.json"} <= evidence_a
    per_case = json.loads((out / "config-digest-a.json").read_text(encoding="utf-8"))["cases"]
    assert set(per_case) == {"t_cfg.py::test_one"}


def test_execute_records_missing_config_logs_as_failure_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """兩輪都沒有設定紀錄：不以例外中止，summary 記 config_errors 並判 fail（fail-closed，證據不足不得放行）。"""
    _executor_mod, out, rc = _execute_harness(tmp_path, monkeypatch, write_config=None)
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert rc == 1
    assert summary["result"] == "fail"
    assert summary["config_errors"], summary
    assert all("設定摘要" in message for message in summary["config_errors"])
    assert not (out / "summary.error.json").exists()


def test_execute_records_runner_environment_when_all_a_nodes_are_approved_rust_only_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 輪全為核准 rust_only skip 時，記錄 runner 環境；不得把無 fixture 誤報為摘要缺失。"""
    cd = _cd()
    _executor_mod, out, rc = _execute_harness(
        tmp_path, monkeypatch, write_config={"b": "9"}, approved_a_skips=True,
    )
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    config_a = json.loads((out / "config-digest-a.json").read_text(encoding="utf-8"))
    identity_a = json.loads((out / "identity-a.json").read_text(encoding="utf-8"))
    log_rows = [json.loads(line) for line in (out / "a-config.jsonl").read_text(encoding="utf-8").splitlines()]

    assert rc == 1, "比較器未被 stub 通過；本測試只驗 A 輪摘要處理"
    assert summary["config_errors"] == []
    assert config_a["scope"] == "runner-environment-only"
    assert config_a["reason"] == "A 輪所有必要節點均為核准的 rust-only skip，沒有隔離夾具"
    assert identity_a["config_digest"] == cd.aggregate(out / "a-config.jsonl")["digest"]
    assert len(log_rows) == 1 and log_rows[0]["nodeid"] == "__runner_environment__:approved-rust-only-skips"


def test_1a_identity_uses_config_records_instead_of_bundle_directory() -> None:
    """1A ab_blackbox.sh：A／B pytest 皆設定 SW_AB_CONFIGLOG，config_digest 由紀錄彙整而來，不再雜湊 bundle 目錄。"""
    text = SHELL_EXECUTOR.read_text(encoding="utf-8")
    assert text.count("SW_AB_CONFIGLOG=") >= 2, "A 與 B 兩輪都必須寫入設定紀錄"
    assert "cfg_digest = dir_sha256(Path('$BUNDLE_OUT'))" not in text, "config_digest 不得取自 bundle 目錄"
    assert "config_digest.py" in text, "1A 身分必須由 config_digest 模組彙整"
    assert "bundle_tree_sha256" in text, "bundle 雜湊改存獨立欄位，不得刪除"
