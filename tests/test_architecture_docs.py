"""架構文件負控制：不啟動 daemon，不接真實序列埠。"""
import copy
import importlib.util
from pathlib import Path
import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("serialwrap_architecture_review", ROOT/"tools/architecture_review.py")
REVIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REVIEW)


def documents():
    folder = ROOT/"docs/architecture"
    return REVIEW.load(folder/"facts.json"), REVIEW.load(folder/"architecture.json")


SOURCE_REVISION = "85a16533a329a2a7d76b025de3762a82a4cd0472"  # Stage 2（Rust CLI＋Rust daemon，0.5.0）合併點
ARCHIFY_REVISION = "208acba8d412335185b2056fedf54b3405df43fb"  # tt-a1i/archify v3.2.0


def test_pinned_facts_match_projection():
    result = REVIEW.verify(ROOT)
    assert result["ok"] and result["source_revision"] == SOURCE_REVISION
    folder = ROOT/"docs/architecture"
    toolchain = REVIEW.load(folder/"toolchain.json")
    assert toolchain["source_revision"] == SOURCE_REVISION
    assert (toolchain["archify_revision"], toolchain["archify_version"]) == (ARCHIFY_REVISION, "3.2.0")
    # 三個交付檔的位元組雜湊與 CI workflow 的引擎釘選由 tools/architecture_review.py 檢查（Linux CI）；
    # 本測試在 Windows 也必跑，工作樹的行尾與是否帶 .github/ 不保證，這裡只驗 JSON 內容。
    assert set(toolchain["sha256"]) == {"facts.json", "architecture.json", "architecture.html"}
    facts, ir = documents()
    meta = ir["meta"]
    assert meta["locale"] == "zh-TW" and meta["output"] == "docs/architecture/architecture.html"
    assert meta["quality_profile"] == "showcase" and "renderer" not in meta


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
    # Stage 2：圖上是 Rust 的實際執行單位；凍結的 Python daemon（sw_core/）與其類別名不得冒充執行期元件。
    assert {"session-actor", "fence", "console-hub", "daemon-main", "pal"} <= set(nodes)
    anchors = [a for record in facts["components"] + facts["relations"] for a in record["evidence"]]
    assert anchors and not [a["path"] for a in anchors if a["path"].startswith("sw_core/") or a["path"].endswith(".py")]
    assert {a["path"].split("/")[1] for a in anchors if a["path"].startswith("crates/")} == {"sw-cli", "sw-core", "sw-daemon", "sw-pal", "sw-proto"}
    shown = " ".join(n["label"] + " " + n.get("sublabel", "") for n in facts["components"])
    assert not [name for name in ("CommandArbiter", "UARTBridge", "execute_command", "SerialwrapService") if name in shown]
    assert "委派集合為空" in nodes["cli"]["responsibility"]
    assert [c["resolution"]["state"] for c in facts["conflicts"] if c["id"] == "bundle-python-fields"] == ["resolved"]
    # 平台差異以「Linux｜Windows」寫在 sublabel；不得以 boundary 冒充部署或安全邊界。
    assert facts["boundaries"] == [] and ir["boundaries"] == []
    for identity in ("service", "bridge", "console-hub", "daemon-main", "watcher", "pal"):
        assert "｜" in nodes[identity]["sublabel"], identity


def test_readme_has_bilingual_html_entry():
    text = (ROOT/"README.md").read_text(encoding="utf-8")
    assert text.count("](docs/architecture/architecture.html)") >= 2
