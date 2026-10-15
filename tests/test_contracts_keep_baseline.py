"""tests/test_contracts_keep_baseline.py — P0 KEEP 列基線凍結（Task P0-T16）。

L1 白箱特徵化測試：直接對 Python 基線（``sw_core``）的現況斷言，函式內才 import
``sw_core``；本檔**不** import ``tools.contracts``。本檔不改產品行為，凍結範圍為：

- daemon logging 的去向、等級、格式與全部訊息模板（``contracts/daemon_logging.yaml``，
  GAP-G-01）；
- TRANSPORT_STALL ``last_error_detail`` 的位元組級 golden（GAP-G-09，
  ``contracts/golden/last_error/transport_stall_hint.json``）；
- config 載入的已知 quirks：錯誤分流（EVT-D-S14）、login_regex 預設值的雙反斜線
  （EVT-D-C1、SES-16.6-04）、templates 只取最後一檔（EVT-D-C2）、字串 "false" 的
  truthiness（EVT-D-C3）。

特徵化測試若與盤點描述不符，以程式為準修正斷言並記入盤點勘誤，不得改動產品程式。
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

# 模組層只放平台 marker（core_portable）；資源 marker 放在函式層（見 T14 規則 2）。
pytestmark = [pytest.mark.core_portable]

REPO = Path(__file__).resolve().parents[1]
SW_CORE = REPO / "sw_core"
DAEMON_LOGGING = REPO / "contracts" / "daemon_logging.yaml"
SESSION_LAST_ERRORS = REPO / "contracts" / "session_last_errors.yaml"
STALL_GOLDEN = REPO / "contracts" / "golden" / "last_error" / "transport_stall_hint.json"
STALL_CAPTURE = REPO / "scripts" / "stage0" / "capture_transport_stall_hint.py"

# cli_trace.py 的 logger 是 CLI 自建 handler 的 JSON 轉錄，不屬 daemon 端 logging；
# 必須在 daemon_logging.yaml 的 excluded_sources 註明。
LOGGER_EXCLUDED_SOURCES = frozenset({"sw_core/cli_trace.py"})
# daemon 入口相關檔：不得含任何 logging 設定（現況為 lastResort 預設）。
DAEMON_ENTRY_FILES = ("sw_core/daemon.py", "sw_core/service.py", "sw_core/__init__.py", "serialwrapd.py")
LEVEL_BY_METHOD = {
    "debug": "DEBUG",
    "info": "INFO",
    "warning": "WARNING",
    "error": "ERROR",
    "exception": "ERROR",
    "critical": "CRITICAL",
}


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fp:
        doc = yaml.safe_load(fp)
    assert isinstance(doc, dict), f"{path.relative_to(REPO)} 頂層應為 mapping"
    return doc


def _is_getlogger_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getLogger"
    )


def _logger_bound_names(tree: ast.AST) -> set[str]:
    """回傳由 ``logging.getLogger(...)`` 賦值而來的名稱（Name 或 Attribute 的屬性名）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_getlogger_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(target.attr)
    return names


def _is_logger_receiver(recv: ast.AST, bound: set[str]) -> bool:
    if _is_getlogger_call(recv):
        return True
    if isinstance(recv, ast.Name):
        return recv.id == "logging" or recv.id in bound
    if isinstance(recv, ast.Attribute):
        return recv.attr in bound
    return False


def _literal_template(call: ast.Call) -> str | None:
    """取第一個引數的字面字串；相鄰字面串接在 AST 已合併為單一 Constant。"""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _collect_logger_calls() -> tuple[dict[tuple[str, str], str], list[str]]:
    """掃描 ``sw_core/**/*.py`` 的 logger 呼叫，回傳 ``{(source, template): level}`` 與無法靜態列出的問題。"""
    found: dict[tuple[str, str], str] = {}
    problems: list[str] = []
    for py in sorted(SW_CORE.glob("**/*.py")):
        rel = py.relative_to(REPO).as_posix()
        if rel in LOGGER_EXCLUDED_SOURCES:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=rel)
        bound = _logger_bound_names(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LEVEL_BY_METHOD
            ):
                continue
            if not _is_logger_receiver(node.func.value, bound):
                continue
            source = f"{rel}:{node.lineno}"
            template = _literal_template(node)
            if template is None:
                problems.append(f"{source} 的 logger 模板非字面字串，無法靜態列出")
                continue
            found[(source, template)] = LEVEL_BY_METHOD[node.func.attr]
    return found, problems


@pytest.mark.memory_only
def test_daemon_logging_all_calls_listed():
    """sw_core 全部 logger 呼叫（source 行 + 模板）與 daemon_logging.yaml 的 messages 一一對應。"""
    doc = _load_yaml(DAEMON_LOGGING)
    excluded = set(doc.get("excluded_sources") or [])
    assert LOGGER_EXCLUDED_SOURCES <= excluded, (
        f"cli_trace.py 的 CLI 自建 handler 須於 excluded_sources 註明，目前為 {sorted(excluded)}"
    )
    messages = doc["messages"]
    listed: dict[tuple[str, str], str] = {}
    for msg in messages:
        listed[(msg["source"], msg["template"])] = msg["level"]
        if msg["level"] == "INFO":
            assert msg.get("dropped") is True, f"INFO 訊息須標 dropped: true：{msg['source']}"

    found, problems = _collect_logger_calls()
    assert not problems, "logger 模板無法靜態列出：\n" + "\n".join(problems)

    missing = sorted(set(found) - set(listed))
    stale = sorted(set(listed) - set(found))
    level_mismatch = sorted(k for k in set(found) & set(listed) if found[k] != listed[k])
    assert not missing, "程式有 logger 呼叫但 daemon_logging.yaml 未列（source, template）：\n" + "\n".join(
        f"  {src} {tpl!r}" for src, tpl in missing
    )
    assert not stale, "daemon_logging.yaml 有過期項（程式已無此呼叫）：\n" + "\n".join(
        f"  {src} {tpl!r}" for src, tpl in stale
    )
    assert not level_mismatch, "等級與程式不符：\n" + "\n".join(
        f"  {src} 程式={found[(src, tpl)]} yaml={listed[(src, tpl)]}" for src, tpl in level_mismatch
    )


@pytest.mark.memory_only
def test_daemon_has_no_logging_config():
    """daemon 入口檔不含 basicConfig／addHandler／setLevel／logging.config（現況：走 lastResort）。"""
    forbidden_attrs = {"basicConfig", "addHandler", "setLevel"}
    hits: list[str] = []
    for rel in DAEMON_ENTRY_FILES:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                hits.append(f"{rel}:{node.lineno} .{node.attr}")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr == "config"
                and isinstance(node.value, ast.Name)
                and node.value.id == "logging"
            ):
                hits.append(f"{rel}:{node.lineno} logging.config")
            elif isinstance(node, ast.ImportFrom) and node.module == "logging.config":
                hits.append(f"{rel}:{node.lineno} from logging.config")
            elif isinstance(node, ast.Import) and any(a.name == "logging.config" for a in node.names):
                hits.append(f"{rel}:{node.lineno} import logging.config")
    assert not hits, "daemon 入口出現 logging 設定，代表基線已變，須停下回報：\n" + "\n".join(hits)


@pytest.mark.process_rpc
def test_lastresort_format_and_level():
    """未設定 handler 時，WARNING 以 ``%(message)s`` 寫到 stderr，INFO 被丟棄。"""
    code = (
        "import logging; l=logging.getLogger('serialwrap'); "
        "l.info('i'); l.warning('w %s', 1)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    assert proc.stdout == b"", "lastResort 不應寫 stdout"
    # Windows 文字模式會把 \n 轉成 \r\n；正規化後比對，維持 core_portable 可跨平台執行。
    stderr = proc.stderr.decode("utf-8", errors="replace").replace("\r\n", "\n")
    assert stderr == "w 1\n", f"stderr 應恰為 'w 1\\n'（INFO 丟棄、格式 %(message)s），實際 {stderr!r}"

    # 核對 daemon_logging.yaml 的 sink 欄位宣告與實測一致
    doc = _load_yaml(DAEMON_LOGGING)
    sink = doc["sink"]
    assert sink["stream"] == "stderr"
    assert sink["min_level"] == "WARNING"
    assert sink["format"] == "%(message)s"
    assert sink["info_dropped"] is True


@pytest.mark.memory_only
def test_last_error_logging_templates_match():
    """session_last_errors.yaml 的 logging 欄內，WARNING 模板須與 daemon_logging.yaml 逐字相同。"""
    dl = _load_yaml(DAEMON_LOGGING)
    le = _load_yaml(SESSION_LAST_ERRORS)
    by_source = {m["source"]: m for m in dl["messages"]}
    named = {v["value"]: v for v in le["values"] if v.get("kind") == "named"}
    pairs = (
        ("CREDENTIALS_UNRESOLVED", "sw_core/session_manager.py:1211"),
        ("TRANSPORT_STALL", "sw_core/session_manager.py:1311"),
    )
    for code, source in pairs:
        msg = by_source.get(source)
        assert msg is not None, f"daemon_logging.yaml 缺少 {source} 的訊息（{code} 的 WARNING）"
        assert msg["level"] == "WARNING", f"{source} 應為 WARNING，實際 {msg['level']}"
        logging_col = named[code]["logging"]
        assert msg["template"] in logging_col, (
            f"{code} 的 logging 欄未逐字含 {source} 的 WARNING 模板：{msg['template']!r}"
        )


@pytest.mark.memory_only
def test_transport_stall_hint_golden():
    """transport_stall_hint 在四組代表輸入下輸出與位元組級 golden 逐字相同（GAP-G-09）。"""
    golden = json.loads(STALL_GOLDEN.read_text(encoding="utf-8"))
    cases = golden["cases"]
    assert cases, "golden cases 不得為空"

    from sw_core import transport_stall

    for idx, case in enumerate(cases):
        with mock.patch("sw_core.transport_stall.resolve_usb_busid", return_value=case["busid"]):
            got = transport_stall.transport_stall_hint(case["real_path"], case["rx_age_s"])
        assert got == case["expected"], (
            f"case[{idx}]（real_path={case['real_path']!r}, rx_age_s={case['rx_age_s']!r}, "
            f"busid={case['busid']!r}）輸出與 golden 不符"
        )


@pytest.mark.process_rpc
def test_transport_stall_hint_capture_reproducible(tmp_path):
    """重跑 capture 腳本的輸出（除 captured_by 外）與入庫 golden 相同。"""
    assert STALL_CAPTURE.is_file(), f"缺少 {STALL_CAPTURE.relative_to(REPO)}（T16 Step 4 產生）"
    out = tmp_path / "transport_stall_hint.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.run(
        [sys.executable, str(STALL_CAPTURE), "--out", str(out)],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    assert out.read_bytes() == STALL_GOLDEN.read_bytes(), "重跑輸出與入庫 golden 不一致"


def _write_yaml_dir(root: Path, files: dict[str, dict]) -> str:
    root.mkdir(parents=True, exist_ok=True)
    for name, doc in files.items():
        (root / name).write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    return str(root)


def _profile_doc(
    name: str = "p",
    *,
    template: dict | None = None,
    defaults: dict | None = None,
    targets: list | None = None,
) -> dict:
    """產生一個最小 profile YAML（一個 template＋一個 target）；測試只改動要測的欄位。"""
    doc: dict = {"profiles": {name: {"platform": "shell", **(template or {})}}}
    if defaults is not None:
        doc["defaults"] = defaults
    if targets is None:
        targets = [{"com": "COM0", "profile": name, "device_by_id": "/dev/serial/by-id/test-dut"}]
    doc["targets"] = targets
    return doc


@pytest.mark.memory_only
def test_config_error_split(tmp_path):
    """EVT-D-S14：raise 類錯誤穿出 load_profiles（型別與訊息逐字）；靜默類回傳預設或被忽略。"""
    from sw_core.config import load_profiles

    failures: list[str] = []

    # --- raise 類（盤點 09 §2.2：_as_max_console_line_chars、_as_unlock_idle） ---
    raise_cases = [
        ("max_console_line_chars=true", {"max_console_line_chars": True}, ValueError,
         "max_console_line_chars 必須是正整數或 null"),
        ("max_console_line_chars='8'", {"max_console_line_chars": "8"}, ValueError,
         "max_console_line_chars 必須是正整數或 null"),
        ("max_console_line_chars=1.5", {"max_console_line_chars": 1.5}, ValueError,
         "max_console_line_chars 必須是正整數或 null"),
        ("max_console_line_chars=0", {"max_console_line_chars": 0}, ValueError,
         "max_console_line_chars 必須是正整數或 null"),
        ("console_unlock_idle_s=true", {"console_unlock_idle_s": True}, ValueError,
         "console_unlock_idle_s 必須是有限正數"),
        ("console_unlock_idle_s=abc", {"console_unlock_idle_s": "abc"}, ValueError,
         "console_unlock_idle_s 必須是有限正數"),
        ("console_unlock_idle_s=-1", {"console_unlock_idle_s": -1}, ValueError,
         "console_unlock_idle_s 必須是有限正數"),
        ("console_unlock_idle_s=inf", {"console_unlock_idle_s": float("inf")}, ValueError,
         "console_unlock_idle_s 必須是有限正數"),
        ("console_unlock_idle_s=nan", {"console_unlock_idle_s": float("nan")}, ValueError,
         "console_unlock_idle_s 必須是有限正數"),
    ]
    for idx, (case_id, template, exc_type, message) in enumerate(raise_cases):
        d = _write_yaml_dir(tmp_path / f"raise_{idx}", {"p.yaml": _profile_doc(template=template)})
        try:
            load_profiles(d)
        except exc_type as exc:
            if str(exc) != message:
                failures.append(f"raise[{case_id}] 訊息為 {str(exc)!r}，預期 {message!r}")
        except Exception as exc:  # noqa: BLE001 - 只為記錄實際型別
            failures.append(f"raise[{case_id}] 例外型別為 {type(exc).__name__}，預期 {exc_type.__name__}")
        else:
            failures.append(f"raise[{case_id}] 未拋出例外（盤點認定為 raise 類）")

    # YAML 語法錯直接穿出 load_profiles（daemon 啟動失敗）
    raw_dir = tmp_path / "raise_yaml"
    raw_dir.mkdir()
    (raw_dir / "bad.yaml").write_text("profiles: [unclosed\n", encoding="utf-8")
    try:
        load_profiles(str(raw_dir))
    except yaml.YAMLError:
        pass
    except Exception as exc:  # noqa: BLE001
        failures.append(f"raise[yaml_syntax] 例外型別為 {type(exc).__name__}，預期 yaml.YAMLError 子類")
    else:
        failures.append("raise[yaml_syntax] 未拋出例外（盤點認定為 raise 類）")

    # --- 靜默類（盤點 09 §2.2：_as_int／_as_float／_as_opt_str／_as_str_keep_empty／_load_uart 等） ---
    silent_cases = [
        # _as_float 失敗 → 預設值
        ("timeout_s=abc→default", {"template": {"timeout_s": "abc"}},
         lambda r: r.profiles[0].timeout_s, 10.0),
        # _as_float(bool) → 1.0（不報錯）
        ("quiet_window_s=true→1.0", {"template": {"quiet_window_s": True}},
         lambda r: r.profiles[0].quiet_window_s, 1.0),
        # _as_int 失敗（"1.5"）→ 預設 115200
        ("uart.baud=1.5→default", {"template": {"uart": {"baud": "1.5"}}},
         lambda r: r.profiles[0].uart.baud, 115200),
        # _as_int 截斷（3.9 → 3）
        ("uart.baud=3.9→3", {"template": {"uart": {"baud": 3.9}}},
         lambda r: r.profiles[0].uart.baud, 3),
        # _load_uart 非 dict → 視同 {}
        ("uart=non-dict→default", {"template": {"uart": "abc"}},
         lambda r: r.profiles[0].uart.baud, 115200),
        # _as_str_keep_empty 保留空字串
        ("ready_probe=''→''", {"template": {"ready_probe": ""}},
         lambda r: r.profiles[0].ready_probe, ""),
        # _as_opt_str 空字串 → None
        ("console_unlock_env=''→None", {"template": {"console_unlock_env": ""}},
         lambda r: r.profiles[0].console_unlock_env, None),
        # bootloader_prompts 非 list → ()
        ("bootloader_prompts=non-list→()", {"template": {"bootloader_prompts": "abc"}},
         lambda r: r.profiles[0].bootloader_prompts, ()),
        # defaults.max_sessions 非數字 → 預設 16
        ("defaults.max_sessions=abc→16", {"defaults": {"max_sessions": "abc"}},
         lambda r: r.max_sessions, 16),
        # target 缺 device_by_id → 靜默略過
        ("target_without_device_by_id→dropped",
         {"targets": [{"com": "COM0", "profile": "p"}]},
         lambda r: len(r.profiles), 0),
        # target 引用未定義 profile → 靜默略過
        ("unknown_profile_ref→dropped",
         {"targets": [{"com": "COM0", "profile": "nope", "device_by_id": "/dev/serial/by-id/test-dut"}]},
         lambda r: len(r.profiles), 0),
        # targets 非 list → 靜默略過（全走動態偵測）
        ("targets_non_list→dropped", {"targets": "abc"},
         lambda r: len(r.profiles), 0),
    ]
    for idx, (case_id, kwargs, getter, expected) in enumerate(silent_cases):
        d = _write_yaml_dir(tmp_path / f"silent_{idx}", {"p.yaml": _profile_doc(**kwargs)})
        try:
            got = getter(load_profiles(d))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"silent[{case_id}] 不應拋出，實際 {type(exc).__name__}: {exc}")
            continue
        if got != expected:
            failures.append(f"silent[{case_id}] 得 {got!r}，預期 {expected!r}")

    # 頂層非 dict 的 YAML 檔（sw_core/config.py:327）被靜默忽略
    top_non_dict_dir = tmp_path / "top_non_dict"
    top_non_dict_dir.mkdir()
    (top_non_dict_dir / "p.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    try:
        res = load_profiles(str(top_non_dict_dir))
        if len(res.profiles) != 0 or len(res.templates) != 0:
            failures.append(f"silent[top_non_dict] 預期空結果，實際 {res}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"silent[top_non_dict] 不應拋出，實際 {type(exc).__name__}: {exc}")

    assert not failures, "EVT-D-S14 config 錯誤分流與表格不符：\n" + "\n".join(failures)


@pytest.mark.memory_only
def test_login_regex_default_double_backslash(tmp_path):
    """EVT-D-C1／SES-16.6-04：login_regex／password_regex 預設值保留雙反斜線（已知缺陷，不修）。"""
    from sw_core import config

    expected_login = r"(?mi)^login:\\s*$"
    expected_password = r"(?mi)^password:\\s*$"

    for cls in (config.ProfileTemplate, config.SessionProfile):
        fields = {f.name: f for f in dataclasses.fields(cls)}
        assert fields["login_regex"].default == expected_login, f"{cls.__name__}.login_regex 預設值應為雙反斜線字面值"
        assert fields["password_regex"].default == expected_password, (
            f"{cls.__name__}.password_regex 預設值應為雙反斜線字面值"
        )

    # 原始碼中的字面值確實含雙反斜線（對應 config.py 的 r"..." 寫法）
    source = (REPO / "sw_core" / "config.py").read_text(encoding="utf-8")
    assert 'r"(?mi)^login:\\\\s*$"' in source, "config.py 應保留 login_regex 的雙反斜線字面值"

    # 現況：對 "login: " 永遠不匹配（實測 False）
    assert re.search(expected_login, "login: ") is None, "現況應為不匹配（EVT-D-C1 已知缺陷）"

    # YAML 未定義 login_regex 時，_template_from_dict 的 fallback 同樣是雙反斜線
    d = _write_yaml_dir(tmp_path / "login_default", {"p.yaml": _profile_doc()})
    result = config.load_profiles(d)
    assert result.templates[0].login_regex == expected_login
    assert result.profiles[0].login_regex == expected_login


@pytest.mark.memory_only
def test_templates_last_file_wins(tmp_path):
    """EVT-D-C2：多個 profile YAML 時，load_profiles 的 templates 只回傳最後一個檔（依載入排序）。"""
    from sw_core.config import load_profiles

    d = _write_yaml_dir(
        tmp_path / "multi",
        {
            "a.yaml": _profile_doc("alpha"),
            "b.yaml": _profile_doc("bravo"),
        },
    )
    result = load_profiles(d)
    names = [t.profile_name for t in result.templates]
    assert names == ["bravo"], (
        f"EVT-D-C2：templates 應只含最後一檔（b.yaml）的 bravo，實際 {names!r}"
    )


@pytest.mark.memory_only
def test_string_false_is_truthy(tmp_path):
    """EVT-D-C3：YAML 字串 "false" 經 bool() 被當成 True（現況凍結）。"""
    from sw_core.config import load_profiles

    d = _write_yaml_dir(tmp_path / "truthy", {"p.yaml": _profile_doc(template={"uart": {"xonxoff": "false"}})})
    result = load_profiles(d)
    assert result.profiles, "應產生一個 session profile"
    assert result.profiles[0].uart.xonxoff is True, (
        'EVT-D-C3：字串 "false" 應被 bool() 當成 True（現況），實際 xonxoff='
        f"{result.profiles[0].uart.xonxoff!r}"
    )

    # 盤點 09 §8.3 D-C3 第二處：event_engine schema 的 auto_enable_com_on_load 與 debug
    from sw_core.event_engine.schema import validate_rule_dict

    rule_doc = {
        "schema_version": 1,
        "owner": "test",
        "name": "truthiness-test",
        "kind": "tool",
        "selectors": ["COM0"],
        "pattern": {"kind": "contains", "value": "test"},
        "handler": {"shell": "true"},
        "auto_enable_com_on_load": "false",
        "debug": "false",
    }
    rule = validate_rule_dict(rule_doc)
    assert rule.auto_enable_com_on_load is True, (
        'EVT-D-C3：字串 "false" 經 bool() 應為 True，實際 auto_enable_com_on_load='
        f"{rule.auto_enable_com_on_load!r}"
    )
    assert rule.debug is True, (
        'EVT-D-C3：字串 "false" 經 bool() 應為 True，實際 debug='
        f"{rule.debug!r}"
    )
