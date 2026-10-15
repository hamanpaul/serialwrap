"""契約驗證器（Rust 重構 P0-T6）。

比對程式抽出的錯誤碼與契約目錄（error_codes.yaml、session_last_errors.yaml）、
檢查契約測試（解析到 test node）、行為變更交叉參照與狀態一致性、
x-pending 併入協定、命名空間分離、排除項稽核與 SKILL 文件覆蓋。

只使用標準函式庫與 PyYAML，不得 import `sw_core`（§3.1、§11.3 黑箱獨立性）。
所有計數皆由資料產生，不得硬編碼任何計數常數（OD-06）。
"""

from __future__ import annotations

import ast
import dataclasses
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any, Final, Iterable, Mapping, Sequence

import yaml

from .catalog import (
    Catalogs,
    CatalogError,
    CODE_STATUS,
    CLI_CONTEXTS,
    CHANGE_STATUS,
    EFFECTIVE_STATUS,
    ErrorCodeEntry,
    INTERFACES,
    LastErrorEntry,
    load_catalogs,
    UART_TX,
)
from .extract import (
    extract,
    ExtractionResult,
    load_extraction_config,
)

CHECK_CODES: Final[tuple[str, ...]] = (
    "C01",
    "C02",
    "C03",
    "C04",
    "C06",
    "C07",
    "C08",
    "C09",
    "C10",
    "C11",
    "C12",
    "C13",
    "C14",
    "C15",
    "C16",
    "C17",
    "C18",
    "C19",
)


@dataclass(frozen=True)
class Finding:
    """契約檢查結果項目。"""

    check: str  # "C01".."C19"
    severity: str  # "FAIL" | "WARN"
    subject: str  # 碼值、檔案或 method
    message: str


def check_catalog_against_emissions(cat: Catalogs, res: ExtractionResult) -> list[Finding]:
    """比對契約目錄與程式碼抽出值（C02 C03 C04 C06）。"""
    findings: list[Finding] = []

    # error_code 命名空間目錄對照表
    ec_map: dict[str, ErrorCodeEntry] = {e.code: e for e in cat.error_codes}
    passthrough_ids = {pt.id for pt in cat.passthrough}

    # session_last_error 命名空間目錄對照表
    le_named = {e.value: e for e in cat.last_errors if e.kind == "named"}
    le_dynamic = [e for e in cat.last_errors if e.kind == "dynamic_prefix"]

    seen_c02: set[tuple[str, str]] = set()
    seen_c03: set[str] = set()
    seen_c06: set[str] = set()

    for em in res.emissions:
        if em.namespace == "error_code":
            if em.kind == "passthrough":
                if em.value not in passthrough_ids and (em.namespace, em.value) not in seen_c02:
                    seen_c02.add((em.namespace, em.value))
                    findings.append(Finding("C02", "FAIL", em.value, f"實作輸出未登錄的 passthrough ID: {em.value}"))
            else:
                entry = ec_map.get(em.value)
                if entry is None:
                    if (em.namespace, em.value) not in seen_c02:
                        seen_c02.add((em.namespace, em.value))
                        findings.append(Finding("C02", "FAIL", em.value, f"實作輸出未登錄的錯誤碼: {em.value}"))
                elif entry.status == "APPROVED":
                    if em.value not in seen_c03:
                        seen_c03.add(em.value)
                        findings.append(Finding("C03", "FAIL", em.value, f"已核准未實作的碼不得生效: {em.value}"))

                # C06: emission implies_interface == "proc" 而 entry scope.interfaces 不含 proc
                if em.implies_interface == "proc" and entry is not None:
                    if "proc" not in entry.scope.interfaces and em.value not in seen_c06:
                        seen_c06.add(em.value)
                        findings.append(
                            Finding("C06", "FAIL", em.value, f"emission implies_interface == 'proc' 但 entry scope.interfaces 不含 proc: {em.value}")
                        )

        elif em.namespace == "session_last_error":
            val = em.value
            matched = False
            if val in le_named:
                matched = True
            else:
                for dyn_entry in le_dynamic:
                    prefix = dyn_entry.value[:-1] if dyn_entry.value.endswith("*") else dyn_entry.value
                    if val == f"{prefix}*" or val == dyn_entry.value or val.startswith(prefix):
                        matched = True
                        break
            if not matched and (em.namespace, val) not in seen_c02:
                seen_c02.add((em.namespace, val))
                findings.append(Finding("C02", "FAIL", val, f"實作輸出未登錄的 last_error 碼: {val}"))

    # C04: entry reachable: true 且 status ∈ EFFECTIVE_STATUS 但未被抽出 → 「目錄過期」
    emitted_codes = res.values("error_code", include_passthrough=False)
    for entry in cat.error_codes:
        if entry.reachable and entry.status in EFFECTIVE_STATUS:
            if entry.code not in emitted_codes:
                findings.append(Finding("C04", "FAIL", entry.code, f"目錄過期（宣告為 reachable 且生效，但未被抽出）: {entry.code}"))

    return findings


def check_extraction_hygiene(cat: Catalogs, res: ExtractionResult) -> list[Finding]:
    """檢查抽取衛生狀態（C07 C08 C19）。"""
    findings: list[Finding] = []

    # C07: res.unresolved 每筆；res.stale_allow_unresolved 每筆
    for u in res.unresolved:
        findings.append(Finding("C07", "FAIL", f"{u.file}:{u.line}", f"未解析表達式: {u.expr} ({u.reason})"))
    for s in res.stale_allow_unresolved:
        findings.append(Finding("C07", "FAIL", f"{s.function}:{s.expr}", f"過期之 allow_unresolved 規則: {s.reason}"))

    # C08: res.stale_exclude_values 每筆
    for e in res.stale_exclude_values:
        findings.append(Finding("C08", "FAIL", e.value, f"過期之 exclude_values 規則: {e.value} ({e.reason})"))

    # C19: 每筆 applied_exclusions（WARN，subject 為值，message 含 category 與 reason）；被排除的值同時存在於任一目錄 → FAIL
    all_catalog_values: set[str] = set()
    for ec in cat.error_codes:
        all_catalog_values.add(ec.code)
    for pt in cat.passthrough:
        all_catalog_values.add(pt.id)
    for le in cat.last_errors:
        all_catalog_values.add(le.value)
        if le.value.endswith(":*"):
            all_catalog_values.add(le.value[:-2])
            all_catalog_values.add(le.value[:-1])

    for ex, count in res.applied_exclusions:
        if ex.value in all_catalog_values:
            findings.append(
                Finding("C19", "FAIL", ex.value, f"被排除的值存在於契約目錄中: category={ex.category}, reason={ex.reason}")
            )
        else:
            findings.append(
                Finding("C19", "WARN", ex.value, f"已套用排除值: category={ex.category}, reason={ex.reason}, count={count}")
            )

    return findings


def _collect_ast_tokens(target_node: ast.AST | list[ast.stmt]) -> list[str]:
    """收集 AST 節點內的字串常數與識別字（排除 docstring、註解、裝飾器與型別註解）。"""
    nodes_to_walk = target_node if isinstance(target_node, list) else [target_node]
    ignored_node_ids: set[int] = set()

    # 若 target_node 本身為陳述句序列（函式本體），首句若為字串常數表達式即為 docstring
    if isinstance(target_node, list) and target_node:
        if isinstance(target_node[0], ast.Expr) and isinstance(target_node[0].value, ast.Constant):
            ignored_node_ids.add(id(target_node[0].value))

    # 找出所有函式與類別的 docstring、裝飾器、引數與回傳註解節點 ID
    for root in nodes_to_walk:
        for n in ast.walk(root):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                if n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
                    ignored_node_ids.add(id(n.body[0].value))
                for dec in n.decorator_list:
                    for dec_child in ast.walk(dec):
                        ignored_node_ids.add(id(dec_child))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg_child in ast.walk(n.args):
                    ignored_node_ids.add(id(arg_child))
                if n.returns:
                    for ret_child in ast.walk(n.returns):
                        ignored_node_ids.add(id(ret_child))

    tokens: list[str] = []
    for root in nodes_to_walk:
        for n in ast.walk(root):
            if id(n) in ignored_node_ids:
                continue
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                tokens.append(n.value)
            elif isinstance(n, ast.Name):
                tokens.append(n.id)
            elif isinstance(n, ast.Attribute):
                tokens.append(n.attr)
            elif isinstance(n, ast.alias):
                tokens.append(n.name)
                if n.asname:
                    tokens.append(n.asname)
            elif isinstance(n, ast.keyword):
                if n.arg:
                    tokens.append(n.arg)

    return tokens


def _node_has_code(target: ast.AST | list[ast.stmt], code_pattern: re.Pattern[str]) -> bool:
    """檢查 AST 節點或陳述句序列收集到的 token 是否有符合碼之正規表示式。"""
    tokens = _collect_ast_tokens(target)
    return any(code_pattern.search(tok) for tok in tokens)


def check_contract_tests(cat: Catalogs, repo_root: Path) -> list[Finding]:
    """檢查契約測試參照與 AST 本體（C09）。"""
    findings: list[Finding] = []
    ast_cache: dict[Path, ast.Module] = {}

    def _get_tree(file_path: Path) -> ast.Module | None:
        if file_path not in ast_cache:
            if not file_path.is_file():
                return None
            try:
                ast_cache[file_path] = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            except Exception:
                return None
        return ast_cache[file_path]

    # 彙整需要檢查契約測試的 entries
    # ErrorCodeEntry: reachable 且 status ∈ EFFECTIVE_STATUS
    # LastErrorEntry: reachable
    entries_to_check: list[tuple[str, tuple[str, ...], str]] = []
    for ec in cat.error_codes:
        if ec.reachable and ec.status in EFFECTIVE_STATUS:
            entries_to_check.append((ec.code, ec.contract_tests, ec.code))

    for le in cat.last_errors:
        if le.reachable:
            entries_to_check.append((le.value, le.contract_tests, le.value))

    for subject, contract_tests, target_code in entries_to_check:
        if not contract_tests:
            findings.append(Finding("C09", "FAIL", subject, f"contract_tests 為空: {subject}"))
            continue

        # 動態前綴取 * 前字面（保留冒號）
        if target_code.endswith("*"):
            prefix = target_code[:-1]
            pattern = re.compile(rf"\b{re.escape(prefix)}")
        else:
            pattern = re.compile(rf"\b{re.escape(target_code)}\b")

        for test_ref in contract_tests:
            parts = test_ref.split("::")
            rel_file = parts[0]
            target_file = repo_root / rel_file
            if not target_file.is_file():
                findings.append(Finding("C09", "FAIL", subject, f"contract_tests 指向的檔案不存在: {rel_file}"))
                break

            tree = _get_tree(target_file)
            if tree is None:
                findings.append(Finding("C09", "FAIL", subject, f"無法解析測試檔 AST: {rel_file}"))
                break

            if len(parts) == 1:
                # 只寫檔名、不帶 node 時，要求檔內至少一個 test* 函式本體含該碼
                test_funcs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
                for n in ast.walk(tree):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
                        test_funcs.append(n)
                if not any(_node_has_code(fn.body, pattern) for fn in test_funcs):
                    findings.append(Finding("C09", "FAIL", subject, f"檔內至少一個 test* 函式本體須含該碼: {rel_file}"))
                    break
            else:
                # 解析 node 路徑（Class::func 或 func，參數化 id [...] 先去掉）
                node_names = [re.sub(r"\[.*\]$", "", p) for p in parts[1:]]
                current_stmts: list[ast.stmt] = tree.body
                target_node: ast.AST | None = None
                found = True

                for name in node_names:
                    next_node = None
                    for stmt in current_stmts:
                        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and stmt.name == name:
                            next_node = stmt
                            break
                    if next_node is None:
                        found = False
                        break
                    target_node = next_node
                    current_stmts = next_node.body

                if not found or target_node is None:
                    findings.append(Finding("C09", "FAIL", subject, f"在 AST 中找不到對應的 def/class node: {test_ref}"))
                    break

                body_to_check = target_node.body if hasattr(target_node, "body") else target_node
                if not _node_has_code(body_to_check, pattern):
                    findings.append(Finding("C09", "FAIL", subject, f"node 本體內找不到該碼: {test_ref}"))
                    break

    return findings


def check_cross_refs(cat: Catalogs) -> list[Finding]:
    """檢查交叉參照與狀態一致性（C10 C11 C12 C16）。"""
    findings: list[Finding] = []
    change_map = {c.id: c for c in cat.behavior_changes}

    # C10 & C16
    for entry in cat.error_codes:
        for bc_id in entry.behavior_change:
            if bc_id not in change_map:
                findings.append(Finding("C10", "FAIL", entry.code, f"未知的 behavior_change ID: {bc_id}"))

        if entry.status == "APPROVED":
            valid_changes = [change_map[cid] for cid in entry.behavior_change if cid in change_map]
            if not any(c.status in {"APPROVED", "IMPLEMENTED", "VERIFIED"} for c in valid_changes):
                findings.append(
                    Finding("C10", "FAIL", entry.code, "APPROVED 碼所引用的變更沒有任何一筆為 APPROVED/IMPLEMENTED/VERIFIED")
                )

        # C16: 狀態一致性（只對 status != "EXISTING" 的碼套用）
        if entry.status != "EXISTING" and entry.behavior_change:
            ref_changes = [change_map[cid] for cid in entry.behavior_change if cid in change_map]
            if ref_changes:
                all_prop_or_appr = all(c.status in {"PROPOSED", "APPROVED"} for c in ref_changes)
                any_impl_or_ver = any(c.status in {"IMPLEMENTED", "VERIFIED"} for c in ref_changes)
                if all_prop_or_appr and entry.status != "APPROVED":
                    findings.append(
                        Finding("C16", "FAIL", entry.code, f"引用變更全為 PROPOSED/APPROVED，碼必須為 APPROVED (現為 {entry.status})")
                    )
                elif any_impl_or_ver:
                    if entry.status not in {"IMPLEMENTED", "VERIFIED"} or not entry.reachable or not entry.contract_tests:
                        findings.append(
                            Finding("C16", "FAIL", entry.code, f"引用變更含 IMPLEMENTED/VERIFIED，碼必須為 IMPLEMENTED/VERIFIED、reachable 且 contract_tests 非空")
                        )

    # C11: error_code_namespace == "forbidden" 的 last_error 值出現在 error_codes.yaml
    ec_codes = {ec.code for ec in cat.error_codes}
    for le in cat.last_errors:
        if le.error_code_namespace == "forbidden" and le.value in ec_codes:
            findings.append(Finding("C11", "FAIL", le.value, f"last_error '{le.value}' 為 forbidden 但出現在 error_codes 目錄"))

    # C12: 任一目錄值以 HARNESS_ 開頭
    all_values: list[str] = []
    all_values.extend(ec.code for ec in cat.error_codes)
    all_values.extend(le.value for le in cat.last_errors)
    all_values.extend(pt.id for pt in cat.passthrough)
    all_values.extend(bc.id for bc in cat.behavior_changes)
    for pair in cat.aux.warning_codes:
        all_values.append(pair[0])
    for triple in cat.aux.error_details:
        all_values.append(triple[0])
    for item in cat.aux.classifications:
        all_values.append(item[0])
    for pair in cat.aux.recommended_actions:
        all_values.append(pair[0])
    for pair in cat.aux.recovery_actions:
        all_values.append(pair[0])
    for pair in cat.aux.recover_actions:
        all_values.append(pair[0])

    for val in all_values:
        if isinstance(val, str) and val.startswith("HARNESS_"):
            findings.append(Finding("C12", "FAIL", val, f"目錄值不得以 HARNESS_ 開頭: {val}"))

    return findings


def check_doc_coverage(
    values: Iterable[str], docs: Sequence[Path], entry_pattern: str, require_in: str = "all"
) -> list[Finding]:
    """檢查文件說明覆蓋度（C13，R-27 等價）。"""
    findings: list[Finding] = []

    if require_in not in {"all", "any"}:
        findings.append(Finding("C13", "FAIL", require_in, f"未知的 require_in 設定: {require_in} (須為 all 或 any)"))
        return findings

    for doc in docs:
        if not doc.is_file():
            findings.append(Finding("C13", "FAIL", str(doc), f"文件不存在: {doc}"))

    pattern = re.compile(entry_pattern, re.MULTILINE)

    # 規格：strip 後忽略空行與 `#` 開頭行，依出現順序去重
    clean_values: list[str] = []
    for raw in values:
        s = str(raw).strip()
        if not s or s.startswith("#"):
            continue
        if s not in clean_values:
            clean_values.append(s)

    docs_covered: list[set[str]] = []
    for doc in docs:
        covered_in_doc: set[str] = set()
        if doc.is_file():
            text = doc.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                gd = match.groupdict()
                code = gd.get("code") or ""
                meaning = (gd.get("meaning") or "").strip()
                action = (gd.get("action") or "").strip()
                if meaning and action:
                    covered_in_doc.add(code)
        docs_covered.append(covered_in_doc)

    for code in clean_values:
        if require_in == "any":
            is_ok = any(code in d for d in docs_covered)
        else:
            is_ok = len(docs_covered) > 0 and all(code in d for d in docs_covered)
        if not is_ok:
            findings.append(Finding("C13", "FAIL", code, f"文件未完整記載其意義或處置: {code}"))

    return findings


def check_generated_facts(repo_root: Path, policy: Mapping[str, Any]) -> list[Finding]:
    """檢查受管區段漂移（C14，R-26 等價）。"""
    findings: list[Finding] = []
    generated_facts = policy.get("generated_facts") or []
    if not isinstance(generated_facts, list):
        findings.append(Finding("C14", "FAIL", "generated_facts", "generated_facts 必須為 list"))
        return findings

    for item in generated_facts:
        if not isinstance(item, dict):
            findings.append(Finding("C14", "FAIL", "generated_facts", f"generated_facts 項目非 dict: {item}"))
            continue
        marker = item.get("marker", "")
        if marker not in {"error-codes", "session-last-errors"}:
            continue

        command = item.get("command", "")
        rel_path = item.get("reflected_in", "")
        if not isinstance(rel_path, str) or not rel_path:
            findings.append(Finding("C14", "FAIL", marker, f"受管區段 reflected_in 非法: {rel_path}"))
            continue
        target_file = repo_root / rel_path
        if not target_file.is_file():
            findings.append(Finding("C14", "FAIL", marker, f"受管區段參照之檔案不存在: {rel_path}"))
            continue

        file_text = target_file.read_text(encoding="utf-8")
        block_pattern = re.compile(
            rf'<!-- BEGIN: generated-fact marker="{re.escape(marker)}" -->\s*\n?(.*?)\n?\s*<!-- END: generated-fact marker="{re.escape(marker)}" -->',
            re.DOTALL,
        )
        match = block_pattern.search(file_text)
        if not match:
            findings.append(Finding("C14", "FAIL", marker, f"未找到 marker=\"{marker}\" 的受管區塊標記: {rel_path}"))
            continue

        env = {**os.environ, "LC_ALL": "C"}
        if str(repo_root) not in sys.path:
            env["PYTHONPATH"] = f"{repo_root}:{env.get('PYTHONPATH', '')}".rstrip(":")

        try:
            proc = subprocess.run(
                shlex.split(command),
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=300,
            )
        except Exception as exc:
            findings.append(Finding("C14", "FAIL", marker, f"受管區段命令執行失敗: {command} ({exc})"))
            continue

        if proc.returncode != 0:
            findings.append(Finding("C14", "FAIL", marker, f"受管區段命令 exit={proc.returncode}: {command}"))
            continue

        cmd_output = proc.stdout.strip()
        actual_content = match.group(1).strip()
        if actual_content != cmd_output:
            findings.append(Finding("C14", "FAIL", marker, f"受管區段內容與命令輸出不一致: marker={marker}, file={rel_path}"))

    return findings


def check_schema_refs(cat: Catalogs, repo_root: Path) -> list[Finding]:
    """檢查 RPC 與 CLI Schema 參照（C15 C17 C18）。"""
    findings: list[Finding] = []
    change_map = {c.id: c for c in cat.behavior_changes}

    rpc_path = repo_root / "contracts" / "rpc_schema.json"
    cli_path = repo_root / "contracts" / "cli_schema.json"

    rpc_data: Any = None
    cli_data: Any = None

    if not rpc_path.is_file():
        findings.append(Finding("C15", "FAIL", "contracts/rpc_schema.json", "contracts/rpc_schema.json 檔案不存在"))
    else:
        try:
            rpc_data = json.loads(rpc_path.read_text(encoding="utf-8"))
        except Exception as exc:
            findings.append(Finding("C15", "FAIL", "contracts/rpc_schema.json", f"JSON 解析失敗: {exc}"))

    if not cli_path.is_file():
        findings.append(Finding("C15", "FAIL", "contracts/cli_schema.json", "contracts/cli_schema.json 檔案不存在"))
    else:
        try:
            cli_data = json.loads(cli_path.read_text(encoding="utf-8"))
        except Exception as exc:
            findings.append(Finding("C15", "FAIL", "contracts/cli_schema.json", f"JSON 解析失敗: {exc}"))

    if rpc_data is not None:
        if not isinstance(rpc_data, dict):
            findings.append(Finding("C18", "FAIL", "contracts/rpc_schema.json", "rpc_schema.json 頂層結構必須為 JSON 物件"))
        else:
            valid_rpc_errors = {e.code for e in cat.error_codes} | {f"@{pt.id}" for pt in cat.passthrough}
            x_methods = rpc_data.get("x-methods")
            if x_methods is None or not isinstance(x_methods, dict):
                findings.append(Finding("C18", "FAIL", "contracts/rpc_schema.json", "rpc_schema.json 缺 x-methods 區段或格式錯誤"))
                x_methods = {}

            for m_name, m_spec in x_methods.items():
                if isinstance(m_spec, dict):
                    errors_list = m_spec.get("errors", [])
                    if isinstance(errors_list, list):
                        for err in errors_list:
                            if err not in valid_rpc_errors:
                                findings.append(Finding("C15", "FAIL", str(err), f"x-methods.{m_name}.errors 含不在目錄中的碼: {err}"))
                    else:
                        findings.append(Finding("C15", "FAIL", m_name, f"x-methods.{m_name}.errors 必須為 list"))

                    error_order_list = m_spec.get("error_order", [])
                    if isinstance(error_order_list, list):
                        for err in error_order_list:
                            if err not in valid_rpc_errors:
                                findings.append(Finding("C15", "FAIL", str(err), f"x-methods.{m_name}.error_order 含不在目錄中的碼: {err}"))
                    else:
                        findings.append(Finding("C15", "FAIL", m_name, f"x-methods.{m_name}.error_order 必須為 list"))
                else:
                    findings.append(Finding("C18", "FAIL", m_name, f"x-methods.{m_name} 必須為 dict"))

            # C18: rpc_schema.json 任一 x-methods.*.result 或 $defs.* 為空物件 {}、缺 type、type: object 卻無 properties；或 method 缺 side_effects／error_order
            for m_name, m_spec in x_methods.items():
                if isinstance(m_spec, dict):
                    if "side_effects" not in m_spec:
                        findings.append(Finding("C18", "FAIL", m_name, f"x-methods.{m_name} 缺 side_effects"))
                    if "error_order" not in m_spec:
                        findings.append(Finding("C18", "FAIL", m_name, f"x-methods.{m_name} 缺 error_order"))
                    res_obj = m_spec.get("result")
                    if res_obj is None or res_obj == {}:
                        findings.append(Finding("C18", "FAIL", m_name, f"x-methods.{m_name}.result 為空物件或缺漏"))
                    elif isinstance(res_obj, dict):
                        if "type" not in res_obj and "$ref" not in res_obj:
                            findings.append(Finding("C18", "FAIL", m_name, f"x-methods.{m_name}.result 缺 type 欄位"))
                        elif res_obj.get("type") == "object":
                            props = res_obj.get("properties")
                            if props is None:
                                findings.append(Finding("C18", "FAIL", m_name, f"x-methods.{m_name}.result 為 object 卻無 properties"))
                            elif props == {} and "additionalProperties" not in res_obj:
                                findings.append(Finding("C18", "FAIL", m_name, f"x-methods.{m_name}.result 為空 properties 佔位符"))

            defs = rpc_data.get("$defs", {})
            if isinstance(defs, dict):
                for def_name, def_obj in defs.items():
                    if def_obj == {}:
                        findings.append(Finding("C18", "FAIL", f"$defs.{def_name}", f"$defs.{def_name} 為空物件"))
                    elif isinstance(def_obj, dict):
                        if "type" not in def_obj and "$ref" not in def_obj:
                            findings.append(Finding("C18", "FAIL", f"$defs.{def_name}", f"$defs.{def_name} 缺 type 欄位"))
                        elif def_obj.get("type") == "object":
                            props = def_obj.get("properties")
                            if props is None:
                                findings.append(Finding("C18", "FAIL", f"$defs.{def_name}", f"$defs.{def_name} 為 object 卻無 properties"))
                            elif props == {} and "additionalProperties" not in def_obj:
                                findings.append(Finding("C18", "FAIL", f"$defs.{def_name}", f"$defs.{def_name} 為空 properties 佔位符"))
            else:
                findings.append(Finding("C18", "FAIL", "$defs", "$defs 必須為 dict"))

    if cli_data is not None:
        if not isinstance(cli_data, dict):
            findings.append(Finding("C15", "FAIL", "contracts/cli_schema.json", "cli_schema.json 頂層結構必須為 JSON 物件"))
        else:
            # C15: contracts/cli_schema.json 的 x-exit-codes[*].codes 與目錄 exit_codes 雙向不一致（目錄有、schema 無，或 schema 有、目錄無；context 不在 CLI_CONTEXTS）
            x_exit_codes = cli_data.get("x-exit-codes")
            if not isinstance(x_exit_codes, list):
                findings.append(Finding("C15", "FAIL", "x-exit-codes", "cli_schema.json 缺 x-exit-codes 區段或非 list"))
                x_exit_codes = []
            schema_exit_map: dict[str, dict[str, int]] = {}
            for item in x_exit_codes:
                if isinstance(item, dict):
                    ctx = item.get("context", "")
                    if ctx not in CLI_CONTEXTS:
                        findings.append(Finding("C15", "FAIL", str(ctx), f"CLI context '{ctx}' 不在 CLI_CONTEXTS 封閉集合中"))
                    if ctx in schema_exit_map:
                        findings.append(Finding("C15", "FAIL", str(ctx), f"cli_schema.json x-exit-codes 重複之 context: '{ctx}'"))
                    codes_dict = item.get("codes")
                    if isinstance(codes_dict, dict):
                        for code, exit_code in codes_dict.items():
                            if ctx not in schema_exit_map:
                                schema_exit_map[ctx] = {}
                            schema_exit_map[ctx][code] = exit_code
                    else:
                        findings.append(Finding("C15", "FAIL", str(ctx), f"x-exit-codes[{ctx}].codes 必須為 dict"))
                else:
                    findings.append(Finding("C15", "FAIL", "x-exit-codes", f"x-exit-codes 項目非 dict: {item}"))

            for ec in cat.error_codes:
                for ctx, exit_code in ec.exit_codes.items():
                    if ctx not in schema_exit_map or schema_exit_map[ctx].get(ec.code) != exit_code:
                        findings.append(Finding("C15", "FAIL", ec.code, f"目錄 exit_codes ({ctx}: {exit_code}) 與 cli_schema.json 不一致: {ec.code}"))

            cat_ec_map = {ec.code: ec for ec in cat.error_codes}
            for ctx, codes_dict in schema_exit_map.items():
                for code, exit_code in codes_dict.items():
                    ec = cat_ec_map.get(code)
                    if ec is None or ec.exit_codes.get(ctx) != exit_code:
                        findings.append(Finding("C15", "FAIL", code, f"cli_schema.json x-exit-codes ({ctx}: {code}={exit_code}) 與目錄 exit_codes 不一致"))

    # C17: x-pending 併入協定
    for s_name, s_data in [("rpc_schema.json", rpc_data), ("cli_schema.json", cli_data)]:
        if s_data and isinstance(s_data, dict):
            pending_list = s_data.get("x-pending", [])
            if isinstance(pending_list, list):
                for item in pending_list:
                    if not isinstance(item, dict) or "change" not in item:
                        item_id = str(item.get("id", item) if isinstance(item, dict) else item)
                        findings.append(Finding("C17", "FAIL", item_id, f"{s_name} x-pending 項缺 change 欄位或非 dict"))
                        continue
                    cid = item["change"]
                    if not isinstance(cid, str):
                        findings.append(Finding("C17", "FAIL", str(cid), f"{s_name} x-pending 的 change 必須為 string"))
                        continue
                    ch = change_map.get(cid)
                    if ch is None:
                        findings.append(Finding("C17", "FAIL", cid, f"x-pending 的 change '{cid}' 不存在於 behavior_changes.yaml"))
                    elif ch.status in {"IMPLEMENTED", "VERIFIED"}:
                        findings.append(Finding("C17", "FAIL", cid, "須併入正式欄位並刪除 x-pending 項"))
            else:
                findings.append(Finding("C17", "FAIL", f"{s_name}.x-pending", f"{s_name} 的 x-pending 必須為 list"))

    return findings


def summarize(cat: Catalogs, res: ExtractionResult) -> dict[str, Any]:
    """產出契約總覽統計摘要（所有計數由資料計算）。"""
    bc_by_status = {st: 0 for st in CHANGE_STATUS}
    for c in cat.behavior_changes:
        if c.status in bc_by_status:
            bc_by_status[c.status] += 1

    ec_by_interface = {iface: 0 for iface in INTERFACES}
    for e in cat.error_codes:
        for iface in e.scope.interfaces:
            if iface in ec_by_interface:
                ec_by_interface[iface] += 1

    ec_by_status = {st: 0 for st in CODE_STATUS}
    for e in cat.error_codes:
        if e.status in ec_by_status:
            ec_by_status[e.status] += 1

    ec_by_uart_tx = {tx: 0 for tx in UART_TX}
    for e in cat.error_codes:
        if e.uart_tx in ec_by_uart_tx:
            ec_by_uart_tx[e.uart_tx] += 1

    exclusions_list: list[dict[str, Any]] = []
    for ex, count in sorted(res.applied_exclusions, key=lambda pair: (pair[0].namespace, pair[0].value)):
        exclusions_list.append(
            {
                "category": ex.category,
                "count": count,
                "namespace": ex.namespace,
                "reason": ex.reason,
                "value": ex.value,
            }
        )

    safety_invariants_map: dict[str, Any] = {}
    for inv in cat.safety_invariants:
        safety_invariants_map[inv.id] = {
            "case_ids": len(inv.case_ids),
            "error_codes": sorted(inv.error_codes),
        }

    return {
        "behavior_changes": {
            "BC": sum(1 for c in cat.behavior_changes if c.kind == "BC"),
            "DF": sum(1 for c in cat.behavior_changes if c.kind == "DF"),
            "SC": sum(1 for c in cat.behavior_changes if c.kind == "SC"),
            "by_status": bc_by_status,
            "pending_owner_confirmations": len(cat.owner_confirmations),
        },
        "error_code": {
            "by_interface": ec_by_interface,
            "by_status": ec_by_status,
            "by_uart_tx": ec_by_uart_tx,
            "catalog_total": len(cat.error_codes),
            "effective": sum(1 for e in cat.error_codes if e.status in EFFECTIVE_STATUS),
            "emitted": len(res.values("error_code", include_passthrough=False)),
            "passthrough": len(cat.passthrough),
        },
        "exclusions": exclusions_list,
        "safety_invariants": safety_invariants_map,
        "session_last_error": {
            "catalog_total": len(cat.last_errors),
            "dynamic_prefix": sum(1 for e in cat.last_errors if e.kind == "dynamic_prefix"),
            "emitted": len(res.values("session_last_error")),
            "named": sum(1 for e in cat.last_errors if e.kind == "named"),
            "terminal": sum(1 for e in cat.last_errors if e.terminal),
        },
    }


def _run_extract_command(extract_cmd: str, repo_root: Path) -> list[str]:
    """執行 policy 宣告之 diagnostic_codes 抽取命令並回傳輸出之碼清單。"""
    env = {**os.environ, "LC_ALL": "C"}
    if str(repo_root) not in sys.path:
        env["PYTHONPATH"] = f"{repo_root}:{env.get('PYTHONPATH', '')}".rstrip(":")

    proc = subprocess.run(
        shlex.split(extract_cmd),
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"抽取命令執行失敗 (exit {proc.returncode}): {proc.stderr}")

    out: list[str] = []
    for raw in proc.stdout.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s not in out:
            out.append(s)
    return out


def run_all(repo_root: Path) -> tuple[list[Finding], dict[str, Any]]:
    """執行全套契約驗證檢查。"""
    findings: list[Finding] = []
    try:
        cat = load_catalogs(repo_root)
    except CatalogError as exc:
        return [Finding("C01", "FAIL", "contracts", p) for p in exc.problems], {}

    res = extract(repo_root, load_extraction_config(repo_root / "contracts" / "code_extraction.yaml"))
    findings += check_catalog_against_emissions(cat, res)
    findings += check_extraction_hygiene(cat, res)
    findings += check_contract_tests(cat, repo_root)
    findings += check_cross_refs(cat)

    policy_path = repo_root / ".project-policy.yml"
    policy: dict[str, Any] = {}
    if policy_path.is_file():
        policy = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}

    for ns in policy.get("diagnostic_codes") or []:
        if not isinstance(ns, dict):
            findings.append(Finding("C13", "FAIL", "diagnostic_codes", f"diagnostic_codes 項目型別錯誤 (非 dict): {ns}"))
            continue
        extract_cmd = ns.get("extract", "")
        ns_name = ns.get("namespace", "diagnostic_codes")
        try:
            values = _run_extract_command(extract_cmd, repo_root)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            findings.append(Finding("C13", "FAIL", ns_name, f"抽取命令執行失敗: {exc}"))
            continue
        doc_paths = [repo_root / d for d in ns.get("docs", []) if isinstance(d, str)]
        findings += check_doc_coverage(
            values,
            doc_paths,
            ns.get("entry_pattern", ""),
            ns.get("require_in", "all"),
        )

    findings += check_generated_facts(repo_root, policy)
    findings += check_schema_refs(cat, repo_root)

    # 排序並去重
    dedup: dict[tuple[str, str, str, str], Finding] = {}
    for f in findings:
        key = (f.check, f.severity, f.subject, f.message)
        if key not in dedup:
            dedup[key] = f

    sorted_findings = sorted(dedup.values(), key=lambda f: (f.check, f.subject, f.message))
    return sorted_findings, summarize(cat, res)
