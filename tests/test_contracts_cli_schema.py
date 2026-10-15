# tests/test_contracts_cli_schema.py
"""CLI 契約 schema 驗證測試（Task P0-T9，L1 白箱）。

驗證 contracts/cli_schema.json 凍結的全域選項、頂層群組、子命令、選項、
RPC 映射（逐子命令、逐 argv 分支）、exit code 總表、重試與逾時清單（含 AST 完整性掃描）、
環境變數與版本資訊。
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


# 預設值由環境變數推導（constants.py 於 import 時計算），pytest 隔離目錄每次 mkdtemp 不同，
# 無法以字面值凍結；schema 以 {"env_default": <常數名>} 記錄來源。本函式只在實際預設值等於
# sw_core.cli 模組全域（build_parser() 於呼叫時讀取的同一來源）的當次值時才替換，因此仍逐項
# 驗證「預設值確實來自該常數」，而非放寬為任意值。
_ENV_DEFAULT_CONSTANTS = {"profile_dir": "PROFILE_DIR", "lock": "LOCK_PATH"}


def _canon_default(a: argparse.Action):
    """回傳 argparse 預設值；環境推導的路徑預設值正規化為 {"env_default": <常數名>}。"""
    name = _ENV_DEFAULT_CONSTANTS.get(a.dest)
    if name is not None and isinstance(a.default, str):
        from sw_core import cli as cli_mod

        if a.default == getattr(cli_mod, name):
            return {"env_default": name}
    return a.default


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
        out["default"] = _canon_default(a)
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
    """以 AST 解析指定檔案中的集合、frozenset 或 tuple 賦值字面常數。"""
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


# --- RPC 映射（逐子命令、逐 argv 分支）-----------------------------------------

_FLAG_ONLY = (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse._CountAction)


def _rpc_of(gname: str, scname: str) -> dict:
    """取得 x-groups 中 `<gname> <scname>` 的 rpc 物件。"""
    for g in _schema().get("x-groups", []):
        if g["name"] == gname:
            for sc in g.get("subcommands") or []:
                if sc["name"] == scname:
                    return sc["rpc"]
    raise AssertionError(f"x-groups 中找不到 {gname} {scname}")


def _rpc_cases() -> list:
    """所有 rpc 非 null 的子命令，供參數化使用（不寫死數量）。"""
    cases = []
    for g in _schema().get("x-groups", []):
        for sc in g.get("subcommands") or []:
            if sc.get("rpc") is not None:
                cases.append(pytest.param(g["name"], sc["name"], id=f"{g['name']} {sc['name']}"))
    return cases


def _leaf_parser(gname: str, scname: str) -> argparse.ArgumentParser:
    """取得 `serialwrap <群組> <子命令>` 的 leaf parser（與 build_parser() 同一物件）。"""
    from sw_core.cli import build_parser

    top = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    group = next(
        a for a in top.choices[gname]._actions if isinstance(a, argparse._SubParsersAction)
    )
    return group.choices[scname]


def _sentinel(a: argparse.Action, doc_path: Path) -> str:
    """為選項／positional 產生可辨識的字面值（各 dest 不同），使參數來源錯配可被偵測。"""
    if a.dest == "file":
        return str(doc_path)
    if a.choices:
        return str(next(iter(a.choices)))
    if a.type is int:
        return "3"
    if a.type is float:
        return "2.5"
    return f"v-{a.dest}"


def _param_dests(src: str) -> set[str]:
    """參數來源對應的 argparse dest（"selector|com" 展開為 selector 與 com）。"""
    return {"selector", "com"} if src == "selector|com" else {src}


def _variants(leaf: argparse.ArgumentParser, rpc: dict, doc_path: Path):
    """產生 (名稱, 參數動作清單, 值表, 探測參數鍵或 None) 分支。

    minimal：必要選項＋每個必要互斥組的第一成員；alt:<dest>：必要互斥組每個成員各跑一次；
    full：所有選項（互斥組只取第一成員）。
    blank:<key>：full 中該參數來源的所有 str dest 改送空字串。探測不依 schema 的省略分類，
    故 omit_when_none 誤植（應為 omit_when_falsy）或漏列時仍會被偵測。
    blank-one:<dest>：只清空 selector|com 其中一個 dest，驗證 `or` 的回退。
    """
    actions = _non_help(leaf)
    groups = list(getattr(leaf, "_mutually_exclusive_groups", []))
    member_dests = {a.dest for g in groups for a in g._group_actions}
    extra_dests = {a.dest for g in groups for a in g._group_actions[1:]}
    base = [
        a for a in actions
        if a.dest not in member_dests and (not a.option_strings or a.required)
    ]
    required_groups = [g for g in groups if g.required]
    full = [a for a in actions if a.dest not in extra_dests]
    full_dests = {a.dest for a in full}

    plans: list[tuple[str, list[argparse.Action], set[str], str | None]] = [
        ("minimal", base + [g._group_actions[0] for g in required_groups], set(), None),
    ]
    for g in required_groups:
        for a in g._group_actions:
            chosen = [a if h is g else h._group_actions[0] for h in required_groups]
            plans.append((f"alt:{a.dest}", base + chosen, set(), None))
    plans.append(("full", full, set(), None))

    by_dest = {a.dest: a for a in actions}
    for key, src in rpc["params"].items():
        if key.startswith("$"):
            continue
        dests = _param_dests(src)
        if not dests <= full_dests:
            continue
        if not all(by_dest[d].type in (None, str) and not by_dest[d].choices for d in dests):
            continue
        plans.append((f"blank:{key}", full, dests, key))
        if len(dests) > 1:
            for d in sorted(dests):
                plans.append((f"blank-one:{d}", full, {d}, key))

    for name, acts, blank, probe_key in plans:
        values = {a.dest: _sentinel(a, doc_path) for a in acts}
        for d in blank:
            values[d] = ""
        yield name, acts, values, probe_key


def _argv_tail(acts: list[argparse.Action], values: dict[str, str]) -> list[str]:
    """將分支轉為子命令之後的 argv（選項先、positional 後）。"""
    out: list[str] = []
    for a in acts:
        if a.option_strings:
            out.append(a.option_strings[0])
            if not isinstance(a, _FLAG_ONLY):
                out.append(values[a.dest])
    for a in acts:
        if not a.option_strings:
            out.append(values[a.dest])
    return out


def _source_value(src: str, ns: argparse.Namespace):
    """依 x-conventions.params_source 取得參數值（dest 名 ／ "selector|com"）。"""
    if src == "selector|com":
        return ns.selector or ns.com
    return getattr(ns, src)


def _expected_params(rpc: dict, ns: argparse.Namespace, doc_obj: dict) -> dict:
    """由 schema 與解析後的 namespace 推導應送出的 params（不含 send_null 的 null 判定，另行核對）。"""
    if "$document" in rpc["params"]:
        return doc_obj
    omit_none = set(rpc.get("omit_when_none", []))
    omit_falsy = set(rpc.get("omit_when_falsy", []))
    out: dict = {}
    for key, src in rpc["params"].items():
        val = _source_value(src, ns)
        if key in omit_none and val is None:
            continue
        if key in omit_falsy and not val:
            continue
        out[key] = val
    return out


def test_rpc_cases_nonempty():
    """x-groups 必須至少登錄一個 rpc 子命令（避免參數化為空而被靜默略過）。"""
    assert _rpc_cases(), "x-groups 中未找到任何 rpc 非 null 的子命令"


@pytest.mark.parametrize("gname,scname", _rpc_cases())
def test_rpc_mapping_matches_cli(gname, scname, tmp_path):
    """逐 argv 分支驗證 rpc：method、params 鍵集合與順序、值來源、omit_when_none／omit_when_falsy／send_null／local_reject_empty。

    每個 str 參數來源都以空字串探測（不依 schema 的省略分類）；列於 local_reject_empty 者須本地拒絕（rc≠0、不送 RPC）。
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout
    from unittest.mock import patch

    from sw_core.cli import build_parser, main

    rpc = _rpc_of(gname, scname)
    leaf = _leaf_parser(gname, scname)
    parser = build_parser()
    doc_path = tmp_path / "rule.json"
    doc_path.write_text("{}", encoding="utf-8")
    doc_obj = json.loads(doc_path.read_text(encoding="utf-8"))
    sock = str(tmp_path / "serialwrapd.sock")
    send_null = set(rpc.get("send_null", []))
    union_none: set[str] = set()

    local_reject = set(rpc.get("local_reject_empty", []))
    for name, acts, values, probe_key in _variants(leaf, rpc, doc_path):
        tail = _argv_tail(acts, values)
        argv = ["--socket", sock, gname, scname] + tail
        ns = parser.parse_args(argv)

        calls: list[tuple[str, dict]] = []

        def _record_rpc(sock_path, method, params, *args, **kwargs):
            calls.append((method, params))
            return {"ok": True, "version": "0.3.2"}

        rc = None
        with patch("sw_core.cli.rpc_call", side_effect=_record_rpc), patch(
            "sw_core.client.rpc_call", side_effect=_record_rpc
        ), patch("sw_core.client._rpc_call_once", side_effect=_record_rpc), redirect_stdout(
            io.StringIO()
        ), redirect_stderr(
            io.StringIO()
        ):
            try:
                rc = main(argv)
            except SystemExit as exc:
                rc = exc.code

        if probe_key is not None and probe_key in local_reject:
            assert not calls and rc not in (0, None), (
                f"{gname} {scname} [{name}] 登錄為本地拒絕空字串，但實際 calls={calls} rc={rc}"
            )
            continue

        assert len(calls) == 1, (
            f"{gname} {scname} [{name}] 預期恰送出 1 次 RPC，實際 {len(calls)} 次（rc={rc}，argv={tail}）"
        )
        actual_method, actual = calls[0]
        assert actual_method == rpc["method"], (
            f"{gname} {scname} [{name}] method 不符：實際 {actual_method} != 期望 {rpc['method']}"
        )

        expected = _expected_params(rpc, ns, doc_obj)
        assert actual == expected, (
            f"{gname} {scname} [{name}] params 不符：實際 {actual} != 期望 {expected}（argv={tail}）"
        )

        none_keys = {k for k, v in actual.items() if v is None}
        assert none_keys <= send_null, (
            f"{gname} {scname} [{name}] 以 null 送出但未列入 send_null：{none_keys - send_null}"
        )
        union_none |= none_keys

        if name == "full" and "$document" not in rpc["params"]:
            assert list(actual) == list(rpc["params"]), (
                f"{gname} {scname} wire 鍵順序不符：實際 {list(actual)} != rpc.params {list(rpc['params'])}"
            )

    assert send_null <= union_none, (
        f"{gname} {scname} send_null 列出的鍵在任何分支都未以 null 出現：{send_null - union_none}"
    )


# --- 其餘 schema 對照 --------------------------------------------------------


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


def test_retry_methods_match_client():
    """驗證 x-retry.methods 等於 sw_core/client.py 的 RETRYABLE_READONLY_METHODS。"""
    schema = _schema()
    expected = _literal_set("sw_core/client.py", "RETRYABLE_READONLY_METHODS")
    actual = set(schema["x-retry"]["methods"])
    assert actual == expected, f"重試方法清單不符：實際 {actual} != 期望 {expected}"


def test_timeout_enrich_matches_client():
    """驗證 x-retry.timeout_enrich.excluded_methods 等於 sw_core/client.py 的 _PROBE_METHODS。"""
    schema = _schema()
    expected = _literal_set("sw_core/client.py", "_PROBE_METHODS")
    actual = set(schema["x-retry"]["timeout_enrich"]["excluded_methods"])
    assert actual == expected, f"TIMEOUT enrich 排除方法不符：實際 {actual} != 期望 {expected}"


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
    assert scanned >= {
        "SERIALWRAP_DAEMON_ENV_FILE",
        "SERIALWRAP_STATE_DIR",
        "XDG_STATE_HOME",
        "SERIALWRAP_RUN_DIR",
    }, f"掃描器未掃到已知環境變數（掃描器失效？）：{scanned}"
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


def _declared_timeout_ranges(schema: dict) -> list[tuple[str, int, int]]:
    """x-fixed-timeouts[].source 與 x-timeouts.sources 宣告的 (檔案, 起行, 迄行) 清單。"""
    sources = [item["source"] for item in schema.get("x-fixed-timeouts", []) if "source" in item]
    sources += schema.get("x-timeouts", {}).get("sources", [])
    ranges = []
    for src in sources:
        file_part, line_part = src.rsplit(":", 1)
        start_s, _, end_s = line_part.partition("-")
        ranges.append((file_part, int(start_s), int(end_s or start_s)))
    return ranges


def _timeout_literal_sites(rel: str) -> list[tuple[int, float]]:
    """以 AST 找出逾時相關的數值字面值：名稱含 timeout 的關鍵字參數與指派、settimeout／sleep 的位置參數。"""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))

    def _num(n) -> bool:
        return isinstance(n, ast.Constant) and type(n.value) in (int, float)

    sites: list[tuple[int, float]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword):
            if node.arg and "timeout" in node.arg.lower() and _num(node.value):
                sites.append((node.value.lineno, node.value.value))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is None or not _num(node.value):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                name = t.id if isinstance(t, ast.Name) else getattr(t, "attr", "")
                if "timeout" in name.lower():
                    sites.append((node.value.lineno, node.value.value))
        elif isinstance(node, ast.Call):
            fn = node.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if fname in ("settimeout", "sleep"):
                sites.extend((a.lineno, a.value) for a in node.args if _num(a))
    return sites


def test_timeout_literals_declared():
    """逾時完整性：cli.py／client.py 內逾時相關的數值字面值都必須落在 x-fixed-timeouts 或 x-timeouts.sources 宣告的行範圍內。"""
    ranges = _declared_timeout_ranges(_schema())
    for rel in ("sw_core/cli.py", "sw_core/client.py"):
        sites = _timeout_literal_sites(rel)
        assert sites, f"{rel} 未掃到任何逾時字面值（掃描器失效？）"
        for line, value in sites:
            assert any(f == rel and a <= line <= b for f, a, b in ranges), (
                f"{rel}:{line} 的逾時字面值 {value} 未登錄於 x-fixed-timeouts／x-timeouts.sources"
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

    from tools.contracts.catalog import CLI_CONTEXTS

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

    # 目錄 exit_codes 為 Mapping[str, int]（plan 01-stage0 §catalog 資料模型；鍵 ∈ CLI_CONTEXTS），
    # 非 {context, exit} 清單；本測試舊版以清單迭代，與規格及 T3 目錄格式不符，故依規格改為映射。
    catalog_codes = {}
    for item in cat_data.get("codes", []):
        code_name = item["code"]
        catalog_codes[code_name] = item.get("exit_codes") or {}

    for code_name, exit_map in catalog_codes.items():
        for ctx, exit_val in exit_map.items():
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
            matched = catalog_codes[code_name].get(ctx) == exit_val
            assert matched, (
                f"x-exit-codes 項 ({ctx}, {code_name}, {exit_val}) 未在目錄 error_codes.yaml 登記"
            )


def test_pending_ids_exist():
    """驗證 x-pending 每筆的 change 存在於 behavior_changes.yaml（實作後逐筆刪除，故允許為空）。"""
    import yaml

    schema = _schema()
    bc_path = ROOT / "contracts" / "behavior_changes.yaml"
    assert bc_path.is_file(), "contracts/behavior_changes.yaml 不存在"
    bc_data = yaml.safe_load(bc_path.read_text(encoding="utf-8")) or {}
    known_changes = {c["id"] for c in bc_data.get("changes", [])}
    pending = schema.get("x-pending", [])
    assert isinstance(pending, list), "x-pending 必須為陣列"
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
