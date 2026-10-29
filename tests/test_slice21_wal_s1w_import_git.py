"""sol P5-86b54d8-2：OD-61 匯入證據的可追溯性以臨時 git repo 實跑——量測 commit 之後只改 WAL append 熱路徑的相依
（sw-proto 的 wal_types／pyjson、sw-pal wal_fs）即不再代表受測版本；無關變更不影響。

與 `test_slice21_wal_s1w_import.py` 分檔：本檔以子行程執行 git（process_rpc），該檔為 memory_only（模組層資源 marker 不得混用）。"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("slice21_gate_judge", ROOT / "scripts" / "rust" / "slice21_gate_judge.py")
judge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(judge)

FORMAL = {"streams": 4, "records": 10000, "payload": 1024, "rotate_bytes": 67108864, "prefill_ratio": 0.9375, "rate": 0}


def _doc(rev: str) -> dict:
    # 原生主機（§1.2 門檻、免 Python 基線；OD-66）：本檔只驗可追溯性。
    return {"schema": "wal_s1w.v1", "verdict": "PASS", "failures": [], "params": dict(FORMAL), "thresholds": {"platform": "linux"},
            "append_ms": {"p50": 0.003, "p95": 0.008, "p99": 0.015}, "rotate_max_ms": 2.1,
            "env": {"virtualization": "native", "git_revision": rev, "source_tree_dirty": False, "fs_type": "ext4", "os": "posix"}}


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                           *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("changed,expected", [
    ("crates/sw-proto/src/wal_types.rs", "EVIDENCE_MISSING"),   # WAL append 熱路徑的 crc32_hex
    ("crates/sw-proto/src/pyjson.rs", "EVIDENCE_MISSING"),      # WalRecord::to_line
    ("crates/sw-pal/src/wal_fs/posix.rs", "EVIDENCE_MISSING"),
    ("docs/unrelated.md", "PASS"),                              # 對照：與 WAL 無關的變更不使證據失效
])
def test_import_rejected_after_wal_dependency_change_in_real_repo(tmp_path, monkeypatch, changed, expected):
    """sol P5-86b54d8-2：在臨時 git repo 實跑——量測 commit 之後只改 sw-proto（或 WAL 檔）即不再代表受測版本。"""
    import yaml

    paths = yaml.safe_load((ROOT / "tests" / "acceptance" / "slice21_gate_expect.yaml").read_text(encoding="utf-8"))[
        "wal_s1w"]["relevant_paths"]
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel in ("crates/sw-proto/src/wal_types.rs", "crates/sw-proto/src/pyjson.rs", "crates/sw-pal/src/wal_fs/posix.rs",
                "docs/unrelated.md"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "measured")
    measured = _git(repo, "rev-parse", "HEAD")
    (repo / changed).write_text("v2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "later")
    monkeypatch.setattr(judge, "ROOT", repo)
    st, reasons = judge.wal_s1w_verdict(None, imported=_doc(measured), host_label="owner-native", formal_params=FORMAL,
                                        relevant_paths=paths)
    assert st == expected, reasons
    if expected == "EVIDENCE_MISSING":
        assert any(changed in r for r in reasons), reasons


def test_wal_s1w_example_records_source_tree_dirty():
    text = (ROOT / "crates" / "sw-core" / "examples" / "wal_s1w.rs").read_text(encoding="utf-8")
    assert '"source_tree_dirty"' in text and '"--porcelain"' in text
