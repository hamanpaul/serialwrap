# tests/test_contracts_cli_schema.py
"""CLI 契約 schema 驗證測試（Task P0-T9，L1 白箱）。

驗證 contracts/cli_schema.json 凍結的全域選項、頂層群組、子命令、選項、
RPC 映射、exit code 總表、重試與逾時清單、環境變數與版本資訊。
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]


def _schema() -> dict:
    """載入 CLI 契約 schema。"""
    return json.loads((ROOT / "contracts" / "cli_schema.json").read_text(encoding="utf-8"))


_KIND = {
    argparse._StoreAction: "store",
    argparse._StoreTrueAction: "store_true",
    argparse._StoreFalseAction: "store_false",
    argparse._StoreConstAction: "store_const",
    argparse._CountAction: "count",
    argparse._AppendAction: "append",
    argparse._VersionAction: "version",
}


def _canon_action(a: argparse.Action) -> dict:
    """將 argparse.Action 物件正規化為與 schema x-global-options / options 相符的 dict。"""
    kind = _KIND.get(type(a), type(a).__name__)
    out = {
        "flags": list(a.option_strings),
        "action": kind,
        "dest": None if kind == "version" else a.dest,
    }
    if kind in ("store", "append"):
        out["type"] = "str" if a.type in (None, str) else getattr(a.type, "__name__", repr(a.type))
    if kind not in ("version",):
        out["default"] = a.default
    if a.choices is not None and not isinstance(a.choices, dict):
        out["choices"] = list(a.choices)
    if a.required and a.option_strings:
        out["required"] = True
    if a.metavar is not None and a.option_strings:
        out["metavar"] = a.metavar
    if not a.option_strings:
        out["flags"], out["positional"] = [], a.dest
    if a.nargs not in (None, 0):
        out["nargs"] = a.nargs
    return out


def _non_help(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    """取得 parser 中非 help 且非 subparsers 的所有 action。"""
    return [
        a
        for a in parser._actions
        if not isinstance(a, (argparse._HelpAction, argparse._SubParsersAction))
    ]


def _canon_mutex_list(mutex_list: list[dict] | None) -> list[tuple[tuple[str, ...], bool]]:
    """將互斥組清單正規化為 (tuple of sorted dests, required) 的有序清單。"""
    if not mutex_list:
        return []
    canon = []
    for m in mutex_list:
        canon.append((tuple(sorted(m.get("dests", []))), bool(m.get("required", False))))
    return sorted(canon)


def _parser_mutex(parser: argparse.ArgumentParser) -> list[tuple[tuple[str, ...], bool]]:
    """提取 parser 的互斥組並正規化。"""
    canon = []
    for g in getattr(parser, "_mutually_exclusive_groups", []):
        dests = tuple(sorted(a.dest for a in g._group_actions))
        canon.append((dests, bool(g.required)))
    return sorted(canon)


def _literal_set(path: str, name: str) -> set[str]:
    """以 AST 解析指定檔案中的集合或 frozenset 賦值字面常數。"""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            value = node.value.args[0] if isinstance(node.value, ast.Call) else node.value
            return {e.value for e in value.elts}
    raise AssertionError(f"{name} not found in {path}")


def _scan_env_vars(rel_paths: list[str]) -> set[str]:
    """以 AST 掃描檔案中所有 os.environ.get / os.environ[...] / os.getenv 的常數字串名稱。"""
    names: set[str] = set()
    for rel in rel_paths:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "get":
                    val = func.value
                    if (isinstance(val, ast.Attribute) and val.attr == "environ") or (
                        isinstance(val, ast.Name) and val.id == "environ"
                    ):
                        if (
                            node.args
                            and isinstance(node.args[0], ast.Constant)
                            and isinstance(node.args[0].value, str)
                        ):
                            names.add(node.args[0].value)
                elif isinstance(func, ast.Attribute) and func.attr == "getenv":
                    if isinstance(func.value, ast.Name) and func.value.id == "os":
                        if (
                            node.args
                            and isinstance(node.args[0], ast.Constant)
                            and isinstance(node.args[0].value, str)
                        ):
                            names.add(node.args[0].value)
                elif isinstance(func, ast.Name) and func.id == "getenv":
                    if (
                        node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                    ):
                        names.add(node.args[0].value)
            elif isinstance(node, ast.Subscript):
                val = node.value
                if (isinstance(val, ast.Attribute) and val.attr == "environ") or (
                    isinstance(val, ast.Name) and val.id == "environ"
                ):
                    sl = node.slice
                    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                        names.add(sl.value)
    return names


def _get_minimal_argv(parser: argparse.ArgumentParser) -> list[str]:
    """根據子命令 parser 的必要選項、必要互斥組與 positional 參數合成最小合法 argv。"""
    argv = []
    handled_actions = set()
    for g in getattr(parser, "_mutually_exclusive_groups", []):
        if g.required and g._group_actions:
            a = g._group_actions[0]
            handled_actions.add(a)
            if a.option_strings:
                flag = a.option_strings[0]
                if isinstance(a, argparse._StoreTrueAction):
                    argv.append(flag)
                else:
                    argv.extend([flag, "test"])
    for a in parser._actions:
        if a in handled_actions or isinstance(
            a, (argparse._HelpAction, argparse._SubParsersAction)
        ):
            continue
        if not a.option_strings:
            if a.choices:
                argv.append(str(a.choices[0]))
            elif getattr(a, "type", None) is int:
                argv.append("1")
            else:
                argv.append("test")
        elif a.required:
            flag = a.option_strings[0]
            if isinstance(a, argparse._StoreTrueAction):
                argv.append(flag)
            elif a.choices:
                argv.extend([flag, str(a.choices[0])])
            elif getattr(a, "type", None) is int:
                argv.extend([flag, "1"])
            else:
                argv.extend([flag, "test"])
    return argv


def test_global_options_match_build_parser():
    """驗證全域選項與 build_parser() 一致。"""
    from sw_core.cli import build_parser

    got = [_canon_action(a) for a in _non_help(build_parser())]
    assert got == _schema()["x-global-options"]


def test_group_order_matches_registration():
    """驗證群組順序與 parser 註冊順序一致。"""
    from sw_core.cli import build_parser

    sub = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    assert [g["name"] for g in _schema()["x-groups"]] == list(sub.choices)


def test_cli_schema_valid_draft2020():
    """驗證 cli_schema.json 符合 JSON Schema Draft 2020-12 規範。"""
    import jsonschema

    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)


def test_every_subcommand_options_match():
    """雙向驗證每個群組與子命令的選項、互斥組與名稱集合。"""
    from sw_core.cli import build_parser

    parser = build_parser()
    schema = _schema()
    groups_by_name = {g["name"]: g for g in schema.get("x-groups", [])}

    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))

    bc_path = ROOT / "contracts" / "behavior_changes.yaml"
    changes_by_id = {}
    if bc_path.is_file():
        import yaml

        bc_data = yaml.safe_load(bc_path.read_text(encoding="utf-8")) or {}
        changes_by_id = {c["id"]: c.get("status") for c in bc_data.get("changes", [])}

    pending_list = schema.get("x-pending", [])
    pending_subcmds = {}
    for p in pending_list:
        if "adds_subcommand" in p:
            g_pending, sc_pending = p["adds_subcommand"]
            pending_subcmds[(g_pending, sc_pending)] = p.get("change")

    # (a) 以 parser 為準走訪
    for group_name, group_parser in sub.choices.items():
        assert group_name in groups_by_name, f"群組 {group_name} 未在 schema x-groups 中定義"
        g_schema = groups_by_name[group_name]

        expected_group_options = g_schema.get("options") or []
        actual_group_options = [_canon_action(a) for a in _non_help(group_parser)]
        assert actual_group_options == expected_group_options, (
            f"群組 {group_name} 群組層選項不符：實際 {actual_group_options} != 期望 {expected_group_options}"
        )

        expected_mutex = _canon_mutex_list(g_schema.get("mutex") or [])
        actual_mutex = _parser_mutex(group_parser)
        assert actual_mutex == expected_mutex, (
            f"群組 {group_name} 群組層 mutex 不符：實際 {actual_mutex} != 期望 {expected_mutex}"
        )

        sub_actions = [
            a for a in group_parser._actions if isinstance(a, argparse._SubParsersAction)
        ]
        if sub_actions:
            sub_action = sub_actions[0]
            parser_sub_names = set(sub_action.choices.keys())
            schema_subcmds = g_schema.get("subcommands") or []
            schema_sub_names = {sc["name"] for sc in schema_subcmds}

            extra_parser_subs = parser_sub_names - schema_sub_names
            for extra in extra_parser_subs:
                change_id = pending_subcmds.get((group_name, extra))
                status = changes_by_id.get(change_id)
                if status == "APPROVED":
                    raise AssertionError(
                        f"核准未實作的子命令已出現：{group_name} {extra}（change: {change_id}）"
                    )
                raise AssertionError(
                    f"parser 多出未在 schema 登錄的子命令：{group_name} {extra}"
                )

            assert parser_sub_names == schema_sub_names, (
                f"群組 {group_name} 子命令集合不符：parser={parser_sub_names} != schema={schema_sub_names}"
            )
        else:
            assert g_schema.get("subcommands") is None, (
                f"群組 {group_name} parser 無子命令，但 schema subcommands 不為 None"
            )

    # (b) 以 schema 為準走訪
    for g_schema in schema.get("x-groups", []):
        gname = g_schema["name"]
        assert gname in sub.choices, f"schema 群組 {gname} 不存在於 parser"
        group_parser = sub.choices[gname]

        subcmds = g_schema.get("subcommands")
        if subcmds is not None:
            sub_actions = [
                a for a in group_parser._actions if isinstance(a, argparse._SubParsersAction)
            ]
            assert sub_actions, f"schema 宣告群組 {gname} 有子命令，但 parser 無 subparsers"
            sc_choices = sub_actions[0].choices

            for sc in subcmds:
                sc_name = sc["name"]
                assert sc_name in sc_choices, f"schema 子命令 {gname} {sc_name} 不存在於 parser"
                sc_parser = sc_choices[sc_name]

                actual_opts = [_canon_action(a) for a in _non_help(sc_parser)]
                expected_opts = sc.get("options") or []
                assert actual_opts == expected_opts, (
                    f"子命令 {gname} {sc_name} 選項不符：實際 {actual_opts} != 期望 {expected_opts}"
                )

                actual_sc_mutex = _parser_mutex(sc_parser)
                expected_sc_mutex = _canon_mutex_list(sc.get("mutex") or [])
                assert actual_sc_mutex == expected_sc_mutex, (
                    f"子命令 {gname} {sc_name} mutex 不符：實際 {actual_sc_mutex} != 期望 {expected_sc_mutex}"
                )

    # 檢查 x-pending 中已 IMPLEMENTED 的變更是否已自 x-pending 刪除
    for p in pending_list:
        change_id = p.get("change")
        status = changes_by_id.get(change_id)
        if status in ("IMPLEMENTED", "VERIFIED"):
            raise AssertionError(
                f"變更 {change_id} 已 IMPLEMENTED，必須已併入 x-groups 並自 x-pending 刪除"
            )


def test_rpc_mapping_matches_cli():
    """驗證每個 rpc 非 null 的子命令呼叫 sw_core.cli.main() 時送出的 method 與 params。"""
    import io
    from contextlib import redirect_stderr, redirect_stdout
    from unittest.mock import patch
    from sw_core.cli import build_parser, main

    schema = _schema()
    parser = build_parser()
    sub_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))

    tested_count = 0
    for g in schema.get("x-groups", []):
        gname = g["name"]
        subcmds = g.get("subcommands") or []
        for sc in subcmds:
            rpc_info = sc.get("rpc")
            if rpc_info is None:
                continue

            scname = sc["name"]
            assert gname in sub_action.choices, f"群組 {gname} 不在 parser choices"
            gp = sub_action.choices[gname]
            sc_sub = next(a for a in gp._actions if isinstance(a, argparse._SubParsersAction))
            assert scname in sc_sub.choices, f"子命令 {gname} {scname} 不在 parser choices"
            scp = sc_sub.choices[scname]

            min_argv = _get_minimal_argv(scp)
            argv = ["--socket", "/tmp/dummy.sock", gname, scname] + min_argv

            calls: list[tuple[str, dict]] = []

            def _record_rpc(sock_path, method, params, *args, **kwargs):
                calls.append((method, params))
                return {"ok": True, "version": "0.3.2"}

            with patch("sw_core.cli.rpc_call", side_effect=_record_rpc), patch(
                "sw_core.client.rpc_call", side_effect=_record_rpc
            ), patch("sw_core.client._rpc_call_once", side_effect=_record_rpc), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(
                io.StringIO()
            ):
                try:
                    main(argv)
                except SystemExit:
                    pass

            assert calls, f"呼叫 {argv} 未觸發任何 rpc_call"
            actual_method, actual_params = calls[0]

            expected_method = rpc_info["method"]
            assert actual_method == expected_method, (
                f"{gname} {scname} method 不符：實際 {actual_method} != 期望 {expected_method}"
            )

            omit_when_none = set(rpc_info.get("omit_when_none", []))
            send_null = set(rpc_info.get("send_null", []))

            for k in omit_when_none:
                assert k not in actual_params, (
                    f"{gname} {scname} 參數 {k} 在未指定時應省略，卻出現在 params: {actual_params}"
                )

            for k in send_null:
                assert k in actual_params and actual_params[k] is None, (
                    f"{gname} {scname} 參數 {k} 在未指定時應送 null，實際為: {actual_params.get(k)}"
                )

            tested_count += 1

    assert tested_count > 0, "未找到任何含有 rpc 定義的子命令"


def test_retry_methods_match_client():
    """驗證 x-retry.methods 等於 sw_core/client.py 的 RETRYABLE_READONLY_METHODS。"""
    schema = _schema()
    expected = _literal_set("sw_core/client.py", "RETRYABLE_READONLY_METHODS")
    actual = set(schema["x-retry"]["methods"])
    assert actual == expected, f"重試方法清單不符：實際 {actual} != 期望 {expected}"


def test_long_methods_match_cli():
    """驗證 x-timeouts.long_methods 等於 sw_core/cli.py 的 _LONG_RPC_METHODS。"""
    schema = _schema()
    expected = _literal_set("sw_core/cli.py", "_LONG_RPC_METHODS")
    actual = set(schema["x-timeouts"]["long_methods"])
    assert actual == expected, f"長逾時方法清單不符：實際 {actual} != 期望 {expected}"


def test_env_vars_listed():
    """驗證 sw_core/cli.py、sw_core/client.py 讀取的環境變數皆登錄於 x-env。"""
    schema = _schema()
    env_entries = schema.get("x-env", [])
    declared = {e["name"] for e in env_entries}
    scanned = _scan_env_vars(["sw_core/cli.py", "sw_core/client.py"])
    assert scanned.issubset(declared), f"掃描到的環境變數未全數登錄於 x-env：{scanned - declared}"
    assert "SERIALWRAP_LOG_LEVEL" in declared, "SERIALWRAP_LOG_LEVEL 必須在 x-env 中"


def test_fixed_timeouts_have_sources():
    """驗證 x-fixed-timeouts 每筆的 source 指向的行含該數值常數。"""
    schema = _schema()
    timeouts = schema.get("x-fixed-timeouts", [])
    assert timeouts, "x-fixed-timeouts 不得為空"
    for item in timeouts:
        src = item.get("source")
        assert src and ":" in src, f"項目 {item.get('name')} 缺少合法 source 標註：{src}"
        file_part, line_part = src.rsplit(":", 1)
        target_file = ROOT / file_part
        assert target_file.is_file(), f"source 指向的檔案不存在：{file_part}"

        if "-" in line_part:
            start_s, end_s = line_part.split("-", 1)
            start_line, end_line = int(start_s), int(end_s)
        else:
            start_line = end_line = int(line_part)

        full_tree = ast.parse(target_file.read_text(encoding="utf-8"))
        constants = {
            node.value
            for node in ast.walk(full_tree)
            if isinstance(node, ast.Constant)
            and hasattr(node, "lineno")
            and start_line <= node.lineno <= end_line
        }

        expected_values = []
        if "value" in item:
            expected_values.append(item["value"])
        for k in ("posix", "windows"):
            if k in item and isinstance(item[k], dict):
                expected_values.extend(item[k].values())

        for val in expected_values:
            assert val in constants, (
                f"項目 {item.get('name')} 的數值 {val} 未出現在 {src}（行 {start_line}-{end_line}）中，"
                f"該處常數包含：{constants}"
            )


def test_version_output_matches():
    """驗證 cli.main(['--version']) 的 stdout 等於 x-version.flag_output 替換版本後的字串。"""
    import io
    from contextlib import redirect_stdout
    from sw_core.cli import main
    from sw_core.version import resolve_version

    schema = _schema()
    flag_output = schema["x-version"]["flag_output"]
    version_str = resolve_version()
    expected = flag_output.replace("<VERSION>", version_str)

    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            main(["--version"])
        except SystemExit:
            pass
    actual = buf.getvalue()
    assert actual == expected, f"--version 輸出不符：實際 {actual!r} != 期望 {expected!r}"


def test_delegation_set_matches_arch():
    """驗證 x-delegation-1a 恰為 arch §7.4 的 8 個群組，且每個都在 x-groups 名稱中。"""
    schema = _schema()
    expected = {
        "daemon",
        "service",
        "supervision-mode",
        "remote",
        "connect",
        "benches",
        "setup",
        "doctor",
    }
    actual = set(schema.get("x-delegation-1a", []))
    assert actual == expected, f"x-delegation-1a 不符 arch §7.4：實際 {actual} != 期望 {expected}"

    all_group_names = {g["name"] for g in schema.get("x-groups", [])}
    for g in actual:
        assert g in all_group_names, f"委派群組 {g} 未在 x-groups 中定義"


def test_exit_codes_consistent_with_catalog():
    """驗證 x-exit-codes 與目錄 error_codes.yaml 的 exit_codes 雙向一致（C15 本地鏡像）。"""
    import yaml

    try:
        from tools.contracts.catalog import CLI_CONTEXTS
    except ImportError:
        CLI_CONTEXTS = (
            "_run_rpc",
            "daemon start",
            "daemon stop",
            "event",
            "connect",
            "benches",
            "remote",
            "setup",
            "service <action>",
            "cmd submit",
            "profile test",
            "doctor",
            "argparse-boundary",
            "fallthrough",
        )

    schema = _schema()
    x_exit_codes = schema.get("x-exit-codes", [])

    for row in x_exit_codes:
        ctx = row["context"]
        assert ctx in CLI_CONTEXTS, f"未知的 CLI context：{ctx} 不在 CLI_CONTEXTS 中"

    cat_path = ROOT / "contracts" / "error_codes.yaml"
    assert cat_path.is_file(), "contracts/error_codes.yaml 不存在"
    cat_data = yaml.safe_load(cat_path.read_text(encoding="utf-8")) or {}

    schema_exit_map = {}
    for row in x_exit_codes:
        ctx = row["context"]
        schema_exit_map[ctx] = row.get("codes", {})

    catalog_codes = {}
    for item in cat_data.get("codes", []):
        code_name = item["code"]
        catalog_codes[code_name] = item.get("exit_codes", [])

    for code_name, exit_list in catalog_codes.items():
        for entry in exit_list:
            ctx = entry["context"]
            exit_val = entry["exit"]
            assert ctx in schema_exit_map, f"目錄碼 {code_name} 的 context {ctx} 不在 x-exit-codes 中"
            assert code_name in schema_exit_map[ctx], (
                f"目錄碼 {code_name}（context: {ctx}）未登錄於 x-exit-codes"
            )
            assert schema_exit_map[ctx][code_name] == exit_val, (
                f"目錄碼 {code_name}（context: {ctx}）exit code 不符："
                f"{schema_exit_map[ctx][code_name]} != {exit_val}"
            )

    for ctx, codes in schema_exit_map.items():
        for code_name, exit_val in codes.items():
            assert code_name in catalog_codes, f"x-exit-codes 中 {ctx} 的碼 {code_name} 不存在於目錄中"
            matched = any(
                e.get("context") == ctx and e.get("exit") == exit_val
                for e in catalog_codes[code_name]
            )
            assert matched, (
                f"x-exit-codes 項 ({ctx}, {code_name}, {exit_val}) 未在目錄 error_codes.yaml 登記"
            )


def test_pending_ids_exist():
    """驗證 x-pending 每筆的 change 存在於 behavior_changes.yaml。"""
    import yaml

    schema = _schema()
    bc_path = ROOT / "contracts" / "behavior_changes.yaml"
    assert bc_path.is_file(), "contracts/behavior_changes.yaml 不存在"
    bc_data = yaml.safe_load(bc_path.read_text(encoding="utf-8")) or {}
    known_changes = {c["id"] for c in bc_data.get("changes", [])}
    pending = schema.get("x-pending", [])
    assert pending, "x-pending 不得為空"
    for p in pending:
        change_id = p.get("change")
        assert change_id in known_changes, (
            f"pending 項 change {change_id} 不存在於 behavior_changes.yaml"
        )


def test_doctor_and_profile_test_always_exit_0():
    """驗證 x-exit-codes 中 context doctor 與 profile test 的 failure 欄為 0。"""
    schema = _schema()
    x_exit_codes = schema.get("x-exit-codes", [])
    by_context = {row["context"]: row for row in x_exit_codes}

    assert "doctor" in by_context, "doctor context 未在 x-exit-codes 中定義"
    assert by_context["doctor"]["failure"] == 0, (
        f"doctor 的 failure exit code 必須為 0，實際為 {by_context['doctor']['failure']}"
    )

    assert "profile test" in by_context, "profile test context 未在 x-exit-codes 中定義"
    assert by_context["profile test"]["failure"] == 0, (
        f"profile test 的 failure exit code 必須為 0，實際為 {by_context['profile test']['failure']}"
    )
