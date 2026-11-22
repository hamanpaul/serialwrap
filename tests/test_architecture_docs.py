"""架構文件負控制：不啟動 daemon，不接真實序列埠。"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]
ARCH = ROOT/"docs/architecture"


def _load_tool(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT/rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REVIEW = _load_tool("serialwrap_architecture_review", "tools/architecture_review.py")
OVERLAY = REVIEW.OVERLAY
FEATURE = _load_tool("serialwrap_architecture_feature_check", "tools/architecture_feature_check.py")


def documents():
    return REVIEW.load(ARCH/"facts.json"), REVIEW.load(ARCH/"architecture.json")


SOURCE_REVISION = "12eb9a0ebead674a74bca58ffb0cd462074f0347"  # main（含 issue 282 停機修正與 OD-107）
ARCHIFY_REVISION = "208acba8d412335185b2056fedf54b3405df43fb"  # tt-a1i/archify v3.2.0
PAL_NODES = {"pal-serial", "pal-rpc", "pal-console", "pal-lock", "pal-devices", "pal-files"}


def test_pinned_facts_match_projection():
    result = REVIEW.verify(ROOT)
    assert result["ok"] and result["source_revision"] == SOURCE_REVISION
    toolchain = REVIEW.load(ARCH/"toolchain.json")
    assert toolchain["schema_version"] == 2
    assert (toolchain["archify_revision"], toolchain["archify_version"]) == (ARCHIFY_REVISION, "3.2.0")
    overview = toolchain["pages"]["overview"]
    assert overview["source_revision"] == SOURCE_REVISION and overview["diagram_type"] == "architecture"
    assert set(overview["sha256"]) == {"facts", "ir", "delivery", "html"}
    # 每頁的檔案雜湊、加工區塊、事實檢查與 pages.json／README 一致性（.gitattributes 讓 docs/architecture 跨平台維持原位元組）。
    summary = REVIEW.verify_pages(ROOT)
    assert "overview" in summary and set(summary) == set(toolchain["pages"])
    facts, ir = documents()
    meta = ir["meta"]
    assert meta["locale"] == "zh-TW" and meta["output"] == "docs/architecture/architecture.html"
    assert meta["quality_profile"] == "showcase" and "renderer" not in meta and "viewBox" not in meta
    assert "0.5.0" not in meta["title"]


@pytest.mark.parametrize("defect", ["hash", "node", "relation", "direction", "revision", "duplicate", "self-evidence",
                                    "boundary", "boundary-member", "double-membership"])
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
    elif defect == "boundary": ir["boundaries"].pop()
    elif defect == "boundary-member": ir["boundaries"][0]["wraps"] = ir["boundaries"][0]["wraps"][1:]
    elif defect == "double-membership":
        facts["boundaries"][0]["wraps"].append(facts["boundaries"][1]["wraps"][0])
        ir["boundaries"][0]["wraps"] = list(facts["boundaries"][0]["wraps"])
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
    # Rust 的實際執行單位；凍結的 Python daemon（sw_core/）與其類別名不得冒充執行期元件。舊的單一 pal 節點已拆成平台能力。
    assert {"session-actor", "fence", "console-hub", "daemon-main", "watcher"} | PAL_NODES <= set(nodes)
    assert "pal" not in nodes
    anchors = [a for record in facts["components"] + facts["relations"] + facts["boundaries"] for a in record["evidence"]]
    assert anchors and not [a["path"] for a in anchors if a["path"].startswith("sw_core/") or a["path"].endswith(".py")]
    assert {a["path"].split("/")[1] for a in anchors if a["path"].startswith("crates/")} == {"sw-cli", "sw-core", "sw-daemon", "sw-pal", "sw-proto"}
    shown = " ".join(n["label"] + " " + n.get("sublabel", "") for n in facts["components"])
    assert not [name for name in ("CommandArbiter", "UARTBridge", "execute_command", "SerialwrapService") if name in shown]
    assert "委派集合為空" in nodes["cli"]["responsibility"]
    assert [c["resolution"]["state"] for c in facts["conflicts"] if c["id"] == "bundle-python-fields"] == ["resolved"]
    # 四個真實的行程／函式庫／外部邊界；每個元件恰好屬於一個邊界（profiles 依原始碼歸 daemon，見 conflicts）。
    bounds = {b["id"]: b for b in facts["boundaries"]}
    assert set(bounds) == {"daemon-process", "external", "pal-layer", "user-side"}
    assert all(b["kind"] == "region" for b in facts["boundaries"])
    assert set(bounds["user-side"]["wraps"]) == {"cli", "human"}
    assert set(bounds["external"]["wraps"]) == {"target", "flasher", "event-handler"}
    assert set(bounds["pal-layer"]["wraps"]) == PAL_NODES
    assert {"service", "bridge", "state-store", "wal", "watcher", "profiles"} <= set(bounds["daemon-process"]["wraps"])
    wrapped = [w for b in facts["boundaries"] for w in b["wraps"]]
    assert len(wrapped) == len(set(wrapped)) and set(wrapped) == set(nodes)
    assert [c["resolution"]["state"] for c in facts["conflicts"] if c["id"] == "profiles-ownership"] == ["resolved"]
    assert [b["label"] for b in ir["boundaries"]] == [b["label"] for b in facts["boundaries"]]
    # 平台差異「Linux｜Windows」只寫在平台層節點的 sublabel；每個平台能力都至少被一個元件使用。
    for identity, node in nodes.items():
        assert ("｜" in node.get("sublabel", "")) == (identity in PAL_NODES), identity
    used = {r["to"] for r in facts["relations"]}
    assert PAL_NODES <= used
    assert not [r for r in facts["relations"] if "｜" in r["label"]]


def test_readme_has_bilingual_html_entry():
    text = (ROOT/"README.md").read_text(encoding="utf-8")
    assert text.count("](docs/architecture/architecture.html)") >= 2


# ---------------------------------------------------------------- 加工、頁面清單與工具一致性

def _pages_and_toolchain():
    return REVIEW.load(ARCH/"pages.json"), REVIEW.load(ARCH/"toolchain.json")


def test_overlay_tool_is_the_pinned_skill_copy():
    _, toolchain = _pages_and_toolchain()
    overlay = toolchain["overlay"]
    assert overlay["tool"] == "tools/architecture_overlay.py"
    assert overlay["skill_path"] == "architecture-fact-layer/scripts/viewer_overlay.py"
    assert overlay["skill_revision"] == toolchain["skill_revision"]
    assert hashlib.sha256((ROOT/overlay["tool"]).read_bytes()).hexdigest() == overlay["sha256"]
    assert overlay["version"] == OVERLAY.VERSION


def test_pages_json_matches_feature_readme():
    pages, _ = _pages_and_toolchain()
    readme = (ARCH/"features/README.md").read_text(encoding="utf-8")
    REVIEW.check_pages_readme(pages, readme)
    assert [f["order"] for f in pages["features"]] == list(range(1, 14))
    entries = REVIEW.page_entries(pages)
    assert entries[0]["id"] == "overview" and sum(1 for e in entries if "parent" not in e) == 14
    broken = copy.deepcopy(pages)
    broken["features"][4]["title"] = "檔案搬運"
    with pytest.raises(ValueError):
        REVIEW.check_pages_readme(broken, readme)
    with pytest.raises(ValueError):
        REVIEW.check_pages_readme(pages, readme.replace("| 07 | `07-event-engine`", "| 07 | `07-events`"))


def test_every_page_carries_current_overlay():
    pages, toolchain = _pages_and_toolchain()
    available = set(toolchain["pages"])
    for page_id, entry in toolchain["pages"].items():
        html = (ARCH/entry["html"]).read_bytes()
        original = OVERLAY.remove(html)
        assert hashlib.sha256(original).hexdigest() == entry["sha256"]["delivery"]
        assert OVERLAY.apply(original, REVIEW.overlay_manifest(pages, available), page_id) == html
        nav = html.decode("utf-8").split('<nav id="sw-overlay-nav"', 1)[1].split("</nav>", 1)[0]
        assert nav.count('aria-current="page"') == 1
        assert nav.count("<a ") == len(available) - 1  # 只有已整合的別頁是連結
        assert nav.count('aria-disabled="true"') == len(REVIEW.page_entries(pages)) - len(available)


def _tamper(kind):
    pages, toolchain = _pages_and_toolchain()
    toolchain = copy.deepcopy(toolchain)
    entry = toolchain["pages"]["overview"]
    html = (ARCH/entry["html"]).read_bytes()
    if kind == "overlay-block":
        changed = html.replace("滾輪縮放".encode(), "拖曳縮放".encode(), 1)
    elif kind == "svg":
        changed = html.replace(b'data-node-id="bridge"', b'data-node-id="bridge2"', 1)
    elif kind == "outside-block":
        changed = html.replace(b"<title>", b"<title>edited ", 1)
    elif kind == "dead-link":
        # 導覽列指向不存在的頁面：以「多一個可點續頁」的清單重新產生加工區塊（不依賴目前已整合幾頁）
        fake_pages = copy.deepcopy(pages)
        ghost = f"{fake_pages['features'][0]['id']}-2"
        fake_pages["features"][0]["continuations"] = [ghost]
        changed = OVERLAY.apply(OVERLAY.remove(html), REVIEW.overlay_manifest(fake_pages, set(toolchain["pages"]) | {ghost}),
                                "overview")
        assert f"{ghost}.html".encode() in changed and not (ARCH/"features"/f"{ghost}.html").exists()
    else:
        raise AssertionError(kind)
    assert changed != html
    # 模擬有人連 toolchain 的最終雜湊一起改，驗證不能只靠自報雜湊。
    entry["sha256"]["html"] = hashlib.sha256(changed).hexdigest()
    read = lambda rel: changed if rel == entry["html"] else (ARCH/rel).read_bytes()
    return toolchain, pages, read


@pytest.mark.parametrize("kind", ["overlay-block", "svg", "outside-block", "dead-link"])
def test_overlay_tampering_is_rejected(kind):
    toolchain, pages, read = _tamper(kind)
    with pytest.raises(ValueError):
        REVIEW.verify_pages(ROOT, toolchain, pages, read=read)


def test_unintegrated_or_drifted_pages_are_rejected():
    pages, toolchain = _pages_and_toolchain()
    stale = copy.deepcopy(toolchain)
    stale["overlay"]["sha256"] = "0"*64
    with pytest.raises(ValueError):
        REVIEW.verify_pages(ROOT, stale, pages)
    ghost = copy.deepcopy(toolchain)
    ghost["pages"]["99-ghost"] = copy.deepcopy(ghost["pages"]["overview"])
    with pytest.raises(ValueError):
        REVIEW.verify_pages(ROOT, ghost, pages)
    bad_pages = copy.deepcopy(pages)
    bad_pages["features"][0]["continuations"] = ["01-command-exec-3"]
    with pytest.raises(ValueError):
        REVIEW.load_pages(ROOT, bad_pages)


# ---------------------------------------------------------------- 功能頁規格的可檢查部分

def _feature_fixture(tmp_path, page_id="05-file-transfer", **mutate):
    facts, _ = documents()
    anchor = copy.deepcopy(facts["components"][0]["evidence"][0])
    parts = [{"id": f"p{i}", "label": f"參與者 {i}", "type": "backend", "overview_component": None,
              "responsibility": "測試", "basis": "observed", "evidence": [anchor]} for i in range(3)]
    steps = [{"id": f"s{i}", "from": f"p{i % 3}", "to": f"p{(i + 1) % 3}", "label": f"步驟 {i}", "phase": "甲" if i < 2 else "乙",
              "basis": "observed", "evidence": [anchor]} for i in range(4)]
    doc = {"schema_version": 1, "document_type": "feature-facts",
           "feature": {"id": page_id, "order": int(page_id[:2]), "title": "測試"},
           "repository": copy.deepcopy(facts["repository"]), "diagram_type": "sequence",
           "scope": {"includes": ["x"], "excludes": []}, "participants": parts, "steps": steps, "unknowns": [], "conflicts": []}
    ir = {"schema_version": 1, "diagram_type": "sequence",
          "meta": {"title": "serialwrap｜測試", "locale": "zh-TW", "output": f"docs/architecture/features/{page_id}.html",
                   "quality_profile": "showcase", "repository": {**facts["repository"], "provider": "github"}},
          "participants": [{"id": p["id"], "label": p["label"]} for p in parts],
          "messages": [{"id": s["id"], "from": s["from"], "to": s["to"], "label": s["label"], "y": 170 + 40 * i} for i, s in enumerate(steps)],
          "segments": [{"from": 150, "to": 250, "label": "甲"}, {"from": 260, "to": 330, "label": "乙"}]}
    for key, fn in mutate.items():
        fn(doc, ir)
    (tmp_path/f"{page_id}.facts.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (tmp_path/f"{page_id}.json").write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    return tmp_path/f"{page_id}.facts.json"


def test_feature_fixture_passes(tmp_path):
    probs = FEATURE.check(_feature_fixture(tmp_path))
    assert list(probs) == [] and probs.warnings == []


def _too_many_participants(doc, ir):
    anchor = doc["participants"][0]["evidence"][0]
    for i in range(3, 9):
        doc["participants"].append({**doc["participants"][0], "id": f"p{i}", "label": f"參與者 {i}", "evidence": [anchor]})
        ir["participants"].append({"id": f"p{i}", "label": f"參與者 {i}"})


@pytest.mark.parametrize("rule, mutate", [
    ("participants", _too_many_participants),
    ("steps", lambda d, i: [d["steps"].append({**d["steps"][1], "id": f"x{n}", "phase": "乙"}) or
                            i["messages"].append({**i["messages"][1], "id": f"x{n}", "y": 400 + 40 * n}) for n in range(27)]),
    ("single-message-phase", lambda d, i: d["steps"][3].update(phase="丙")),
    ("variant", lambda d, i: d["steps"][0].update(variant="emphasis")),
    ("ir-variant", lambda d, i: i["messages"][0].update(variant="dashed")),
    ("self-arrow", lambda d, i: (d["steps"][0].update(to=d["steps"][0]["from"]), i["messages"][0].update(to=d["steps"][0]["from"]))),
    ("viewbox", lambda d, i: i["meta"].update(viewBox=[1200, 800])),
    ("unknown-page", None),
])
def test_feature_check_rules_are_enforced(tmp_path, rule, mutate):
    path = _feature_fixture(tmp_path, page_id="05-file-transfer-9") if rule == "unknown-page" else _feature_fixture(tmp_path, m=mutate)
    assert FEATURE.check(path), rule


def test_feature_check_warnings_do_not_block(tmp_path):
    long_label = "非常長的參與者名稱超過十五單位"
    path = _feature_fixture(tmp_path, m=lambda d, i: (d["steps"][0].update(note="送出｜等待"), d["participants"][0].update(label=long_label),
                                                      i["participants"][0].update(label=long_label)))
    probs = FEATURE.check(path)
    assert list(probs) == [] and len(probs.warnings) == 2
    ok = FEATURE.check(_feature_fixture(tmp_path, m=lambda d, i: d["steps"][0].update(note="Linux｜Windows：PTY｜TCP")))
    assert ok.warnings == []
