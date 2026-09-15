"""架構文件負控制：不啟動 daemon，不接真實序列埠。"""
import copy
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("serialwrap_architecture_review", ROOT/"tools/architecture_review.py")
REVIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REVIEW)


def documents():
    folder = ROOT/"docs/architecture"
    return REVIEW.load(folder/"facts.json"), REVIEW.load(folder/"architecture.json")


def test_pinned_facts_match_projection():
    assert REVIEW.verify(ROOT)["ok"]


@pytest.mark.parametrize("defect", ["hash", "node", "relation", "direction", "revision", "duplicate", "self-evidence"])
def test_corrupted_architecture_is_rejected(defect):
    facts, ir = documents()
    facts, ir = copy.deepcopy(facts), copy.deepcopy(ir)
    if defect == "hash": facts["components"][0]["evidence"][0]["excerpt_sha256"] = "0"*64
    elif defect == "node": ir["components"].pop()
    elif defect == "relation": ir["connections"].pop()
    elif defect == "direction": ir["connections"][0]["from"], ir["connections"][0]["to"] = ir["connections"][0]["to"], ir["connections"][0]["from"]
    elif defect == "revision": ir["meta"]["repository"]["revision"] = "0"*40
    elif defect == "duplicate": ir["components"].append(copy.deepcopy(ir["components"][0]))
    elif defect == "self-evidence": facts["components"][0]["evidence"][0]["path"] = "docs/architecture/facts.json"
    with pytest.raises(ValueError): REVIEW.verify(ROOT, facts, ir)


def test_serialwrap_does_not_invent_cortex_roles():
    facts, ir = documents()
    nodes = {n["id"]:n for n in facts["components"]}
    assert not {"manager", "persona", "deck", "workflow-registry"}.intersection(nodes)
    assert {"sessions", "command-records", "capture", "lease", "state-store"} <= set(nodes)
    assert "workflow_review" not in facts
    for index, identity in enumerate(("cli", "service", "arbiter", "execute", "bridge", "target"),1):
        assert nodes[identity]["label"].startswith(str(index)+" ")
    assert "不是獨立服務" in nodes["execute"]["responsibility"]
    assert "command done 不代表背景工作已完成" in nodes["capture"]["responsibility"]


def test_readme_has_bilingual_html_entry():
    text = (ROOT/"README.md").read_text(encoding="utf-8")
    assert text.count("](docs/architecture/architecture.html)") >= 2
