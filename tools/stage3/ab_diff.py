"""L1.5 A/B 差分工具（P9-T7B）。

只讀取 A、B 兩側 func-test 報告目錄（`<case>/bridge/<n>/` 樹），不 import P2 模組。
比較前的正規化只依 `contracts/ab_normalization.yaml`（arch §9.3），不得另寫寬鬆比對。

判定分三類：
- 完整性（每側獨立）：必要案例恰出現；必要案例每個 run 的 cli.ndjson 存在且每筆紀錄含 argv 清單；
  每筆紀錄的 rc 必須為整數（func-test 逾時寫入 rc: null，屬缺漏，對所有案例都算完整性問題）；
  comparison.json 的 verdict 為 PASS／FAIL、每個 invariant 的 verdict 為 PASS／FAIL；
  bridge-manifest.json 的 closed_cleanly 為 True 且 failure 為 null；manifest.json 存在。
  未知、缺漏、非字串或 INSUFFICIENT_EVIDENCE 一律不視為通過（§11.3、§11.4）。
- 安全：任一側任一 run 的 comparison.json invariants 有 verdict 為 FAIL。
- 差異：兩側 comparison.json verdict；各次 CLI 呼叫的 argv 尾端（去除 CLI 路徑、endpoint 旗標與值、
  `--cmd-id` 的值）、rc 與正規化後的 stdout JSON 是否一致。連續且 argv 相同、回應成功且正規化結果
  相同的輪詢呼叫（contracts 的 poll_commands）才收斂；錯誤回應與狀態轉換皆保留。
  實例根目錄（func-test 以 `sw-func-*` 暫存目錄建立，不在報告目錄之下）取自各 run 自身的證據
  （socket／endpoint 旗標、manifest 的 daemon_side、daemon status 的 wal_dir／mirror_path），
  不假設位於報告目錄之下；Windows 以 tcp endpoint 與 COM 裝置名執行時，仍由 wal 路徑取得根目錄。
"""

from __future__ import annotations

import itertools
import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tools.stage3.reason_codes import HarnessError

DIFF_SCHEMA = "stage3-ab-diff.v1"

_MODE = "bridge"
_MANIFEST = "manifest.json"
_COMPARISON = "comparison.json"
_BRIDGE_MANIFEST = "bridge-manifest.json"
_CLI_LOG = "cli.ndjson"
_PASS = "PASS"
_FAIL = "FAIL"
_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
_EXCEPTION = "EXCEPTION"
_DEFAULT_ROOT_PLACEHOLDER = "<ROOT>"
_DEFAULT_PTY_PLACEHOLDER = "<PTY>"
_DECIDED_VERDICTS = frozenset({_PASS, _FAIL})
_ENDPOINT_FLAGS = ("--socket", "--endpoint")
_CMD_ID_FLAG = "--cmd-id"
_CMD_ID_PLACEHOLDER = "<ID>"
# host /proc 掃描快照：成功的 daemon status（CLI `daemon status` 對應 RPC `health.status`）回應最上層的 foreign_holders
# 每次掃描都可能不同，僅忽略該欄位；其他子命令、錯誤回應與巢狀同名欄位仍嚴格比較。
_SCAN_SNAPSHOT_COMMAND = ("daemon", "status")
_PTY_PATH = re.compile(r"/dev/pts/[0-9]+")
# 路徑片段字元：根目錄取代時，前一字元不得屬於此集合（避免 `/tmp/x` 誤中 `/tmp/xy`）。
_PATH_SEGMENT_CHARS = "A-Za-z0-9._-"
# contracts/ab_normalization.yaml 認得的頂層鍵；未知鍵一律拒絕，避免拼錯或未併入的規則被靜默忽略。
_RULE_KEYS = frozenset({
    "approved_revision",
    "reviewed_by",
    "g1_exception_message",
    "g2_set_paths",
    "pid_fields",
    "volatile_keys",
    "instance_root_placeholder",
    "pty_path_placeholder",
    "poll_commands",
})


# ---------------------------------------------------------------------------
# 報告樹讀取
# ---------------------------------------------------------------------------


def _is_decided(value: Any) -> bool:
    """verdict 必須是字串且為 PASS／FAIL；非字串（list、dict、None）一律不是已判定。"""
    return isinstance(value, str) and value in _DECIDED_VERDICTS


def _run_key(name: str) -> tuple[int, int, str]:
    """run 目錄排序鍵：ASCII 十進位名稱依數值排序，其餘（含 Unicode 數字）依字串排在後面。"""
    if name.isascii() and name.isdecimal():
        return (0, int(name), name)
    return (1, 0, name)


def load_case_runs(report_dir: Path) -> dict[str, list[Path]]:
    """回傳 case_id → 該案 `bridge` 模式下的 run 目錄（`<case>/bridge/<n>/`，依 run 序號排序）。

    只取 mode 為 `bridge` 的目錄；其他模式與檔案項目忽略。報告根目錄不存在時回傳空 dict。
    """
    root = Path(report_dir)
    if not root.is_dir():
        return {}
    runs: dict[str, list[Path]] = {}
    for case_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        bridge_dir = case_dir / _MODE
        if not bridge_dir.is_dir():
            continue
        run_dirs = sorted((path for path in bridge_dir.iterdir() if path.is_dir()),
                          key=lambda path: _run_key(path.name))
        if run_dirs:
            runs[case_dir.name] = run_dirs
    return runs


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """讀取 JSON 物件；檔案缺失、無法解碼或頂層非物件時回傳 None。"""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _read_calls(run_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """讀取 cli.ndjson 的每次 CLI 呼叫紀錄；回傳 (紀錄列表, 解析問題)。

    檔案不存在時回傳空列表且不報問題；缺檔是否構成完整性問題由必要案例的檢查決定（見 `_run_problems`）。
    無法解析的行略過並回報問題。
    """
    path = run_dir / _CLI_LOG
    if not path.is_file():
        return [], []
    try:
        # NDJSON 只以 "\n" 切行；不得用 splitlines()，它會把 JSON 字串內未跳脫的 U+2028 等字元也切開。
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError):
        return [], [f"無法讀取 {_CLI_LOG}"]
    calls: list[dict[str, Any]] = []
    problems: list[str] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            problems.append(f"{_CLI_LOG} 第 {line_no} 行非 JSON")
            continue
        if isinstance(record, dict):
            calls.append(record)
        else:
            problems.append(f"{_CLI_LOG} 第 {line_no} 行非 JSON 物件")
    return calls, problems


def _verdict_of(run_dir: Path) -> str | None:
    comparison = _read_json_object(run_dir / _COMPARISON)
    if comparison is None:
        return None
    verdict = comparison.get("verdict")
    return verdict if isinstance(verdict, str) else None


def _aggregate_verdict(verdicts: Sequence[str | None]) -> str:
    """單一案例多次 run 的彙總判定：缺漏優先標出，其次 FAIL、INSUFFICIENT_EVIDENCE，全 PASS 才為 PASS。"""
    values = {verdict if verdict is not None else "MISSING" for verdict in verdicts}
    if not values:
        return "MISSING"
    for token in ("FAIL", _INSUFFICIENT, "MISSING"):
        if token in values:
            return token
    if values == {_PASS}:
        return _PASS
    return "|".join(sorted(values))


# ---------------------------------------------------------------------------
# 完整性與安全
# ---------------------------------------------------------------------------


def _run_problems(case: str, run_dir: Path, *, strict: bool) -> list[str]:
    """單一 run 的完整性問題。

    所有案例：cli.ndjson 每筆紀錄的 rc 必須為整數（逾時的 rc: null 屬缺漏），且 cli.ndjson 須可解析。
    strict 為必要案例的完整檢查：cli.ndjson 必須存在（缺檔不得視為零次呼叫）、每筆紀錄含 argv 清單；
    comparison.json 的 verdict 與每個 invariant 的 verdict 必須為 PASS／FAIL（缺漏、未知與
    INSUFFICIENT_EVIDENCE 都不是通過）；bridge-manifest.json 須 closed_cleanly 且 failure 為 null。
    """
    label = f"{case}/{_MODE}/{run_dir.name}"
    problems: list[str] = []
    calls, cli_problems = _read_calls(run_dir)
    problems.extend(f"{label}: {problem}" for problem in cli_problems)
    for index, call in enumerate(calls, start=1):
        if not isinstance(call.get("rc"), int):
            problems.append(f"{label}: {_CLI_LOG} 第 {index} 筆紀錄 rc 非整數（逾時或缺漏）")
    if not strict:
        return problems
    if not (run_dir / _CLI_LOG).is_file():
        problems.append(f"{label}: {_CLI_LOG} 缺失（必要案例的 CLI 證據不得視為零次呼叫）")
    for index, call in enumerate(calls, start=1):
        if not isinstance(call.get("argv"), list):
            problems.append(f"{label}: {_CLI_LOG} 第 {index} 筆紀錄缺少 argv 清單")
    if _read_json_object(run_dir / _MANIFEST) is None:
        problems.append(f"{label}: manifest.json 缺失或非 JSON 物件")
    comparison = _read_json_object(run_dir / _COMPARISON)
    if comparison is None:
        problems.append(f"{label}: comparison.json 缺失或非 JSON 物件")
    else:
        verdict = comparison.get("verdict")
        if not _is_decided(verdict):
            problems.append(f"{label}: comparison.json verdict 缺失、未知或非 PASS／FAIL（{verdict!r}）")
        invariants = comparison.get("invariants")
        if not isinstance(invariants, list):
            problems.append(f"{label}: comparison.json invariants 缺失或非清單")
        else:
            for invariant in invariants:
                if not isinstance(invariant, Mapping):
                    problems.append(f"{label}: comparison.json invariants 含非物件項目")
                elif not _is_decided(invariant.get("verdict")):
                    problems.append(
                        f"{label}: invariant {invariant.get('id')!r} verdict 缺失、未知或非 PASS／FAIL"
                        f"（{invariant.get('verdict')!r}）"
                    )
    bridge = _read_json_object(run_dir / _BRIDGE_MANIFEST)
    if bridge is None or bridge.get("closed_cleanly") is not True or bridge.get("failure") is not None:
        problems.append(f"{label}: bridge-manifest.json 未確認 closed_cleanly 為 True 且 failure 為 null")
    return problems


def _safety_tokens(label: str, case: str, run_dir: Path) -> list[str]:
    """回傳此 run 中 verdict 為 FAIL 的 invariant，格式 `<A|B>:<case>:<invariant_id>`。

    comparison.v1 的 invariant 狀態欄位為 `verdict`（tests/acceptance/comparison.schema.json）。
    """
    comparison = _read_json_object(run_dir / _COMPARISON)
    if comparison is None or not isinstance(comparison.get("invariants"), list):
        return []
    tokens: list[str] = []
    for invariant in comparison["invariants"]:
        if isinstance(invariant, Mapping) and invariant.get("verdict") == _FAIL:
            tokens.append(f"{label}:{case}:{invariant.get('id')}")
    return tokens


def safety_tokens(report_dir: Path, label: str) -> list[str]:
    """回傳報告目錄內所有 bridge run 的安全違規記號 `<label>:<case>:<invariant_id>`（verdict 為 FAIL 的 invariant）。

    供 A/B 編排（ab_runner，Task 6）與本模組共用，避免各自解析 comparison.json。
    """
    tokens: list[str] = []
    for case, runs in sorted(load_case_runs(Path(report_dir)).items()):
        for run_dir in runs:
            for token in _safety_tokens(label, case, run_dir):
                if token not in tokens:
                    tokens.append(token)
    return tokens


# ---------------------------------------------------------------------------
# 正規化（只依 contracts/ab_normalization.yaml 的具名規則）
# ---------------------------------------------------------------------------


def _str_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"A/B 正規化規則 {name} 必須為字串清單")
    return list(value)


def _g2_entries(rules: Mapping[str, Any]) -> list[tuple[tuple[str, ...], str]]:
    """解析 g2_set_paths：每項為 {command: [子命令片段], json_path: 點路徑}。"""
    entries: list[tuple[tuple[str, ...], str]] = []
    for item in rules.get("g2_set_paths") or []:
        if not isinstance(item, Mapping):
            raise ValueError(f"g2_set_paths 項目必須為對映：{item!r}")
        command = item.get("command")
        json_path = item.get("json_path")
        if (not isinstance(command, list) or not command
                or not all(isinstance(token, str) for token in command)):
            raise ValueError(f"g2_set_paths.command 必須為非空字串清單：{item!r}")
        if not isinstance(json_path, str) or not json_path:
            raise ValueError(f"g2_set_paths.json_path 必須為非空字串：{item!r}")
        entries.append((tuple(command), json_path))
    return entries


def _poll_commands(rules: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """解析 poll_commands：每項為子命令前綴（例如 [session, list]），命中者視為輪詢型呼叫。"""
    entries: list[tuple[str, ...]] = []
    for item in rules.get("poll_commands") or []:
        if (not isinstance(item, list) or not item
                or not all(isinstance(token, str) and token for token in item)):
            raise ValueError(f"poll_commands 項目必須為非空字串清單：{item!r}")
        entries.append(tuple(item))
    return entries


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sort_at(node: Any, parts: Sequence[str]) -> Any:
    """把 parts 指向的清單排序（集合語意）；路徑不存在時原樣返回。"""
    if not parts:
        return sorted(node, key=_canonical) if isinstance(node, list) else node
    if isinstance(node, dict) and parts[0] in node:
        node[parts[0]] = _sort_at(node[parts[0]], parts[1:])
    return node


def _usable_root(root: str) -> bool:
    """實例根目錄必須為絕對路徑且至少兩個路徑片段；單一片段（例如 `/tmp`）範圍過寬，不取代。"""
    path = Path(root)
    return path.is_absolute() and len(path.parts) >= 3


def _normalize_roots(roots: Iterable[str]) -> tuple[str, ...]:
    """去除尾端分隔符與空值、去重、排除不可用根目錄，並依長度由長到短排序，避免較短根目錄先取代而截斷較長路徑。"""
    cleaned = {root.rstrip("/\\") for root in roots}
    usable = {root for root in cleaned if root and _usable_root(root)}
    return tuple(sorted(usable, key=lambda root: (-len(root), root)))


def _root_patterns(roots: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    """每個根目錄的有邊界比對：前一字元不得為路徑片段字元，後一字元須為字串結尾或路徑分隔符。"""
    return tuple(
        re.compile(rf"(?<![{_PATH_SEGMENT_CHARS}]){re.escape(root)}(?=[/\\]|\Z)")
        for root in _normalize_roots(roots)
    )


@dataclass(frozen=True)
class _NormCtx:
    volatile: frozenset[str]
    pid_fields: frozenset[str]
    pid_aliases: dict[int, str]
    g1: bool
    roots: tuple[re.Pattern[str], ...]
    root_placeholder: str
    pty_placeholder: str


def _text(value: str, ctx: _NormCtx) -> str:
    """字串內的實例根目錄與 PTY 路徑換成佔位字串（值與鍵共用）。"""
    for pattern in ctx.roots:
        value = pattern.sub(lambda _match: ctx.root_placeholder, value)
    return _PTY_PATH.sub(lambda _match: ctx.pty_placeholder, value)


def _collect_pid_paths(
    value: Any,
    ctx: _NormCtx,
    occurrences: dict[int, list[tuple[str, ...]]],
    *,
    path: tuple[str, ...] = (),
    pid_field: bool = False,
) -> None:
    """收集 PID 的正規化路徑；清單索引統一視為同一位置，避免清單順序影響 PID 標籤。"""
    if pid_field and isinstance(value, int) and not isinstance(value, bool):
        occurrences.setdefault(value, []).append(path)
        return
    if isinstance(value, Mapping):
        exception_message = ctx.g1 and value.get("error_code") == _EXCEPTION
        for key, child in sorted(value.items(), key=lambda item: str(item[0])):
            if key in ctx.volatile or (exception_message and key == "message"):
                continue
            new_key = _text(key, ctx) if isinstance(key, str) else key
            child_path = path + (str(new_key),)
            is_pid_field = pid_field or (isinstance(key, str) and key in ctx.pid_fields)
            _collect_pid_paths(
                child,
                ctx,
                occurrences,
                path=child_path,
                pid_field=is_pid_field,
            )
        return
    if isinstance(value, list):
        unordered_pid_list = pid_field and all(
            isinstance(item, int) and not isinstance(item, bool) for item in value
        )
        for index, item in enumerate(value):
            index_path = "*" if unordered_pid_list else str(index)
            _collect_pid_paths(item, ctx, occurrences, path=path + (index_path,), pid_field=pid_field)


# 所有同簽章群組的排列數乘積上限。超過時退回排序後的單一標籤，不再保證與走訪順序及 PID 數值無關；
# 此為理論邊界，實際 L1.5 報告中單一 holder 的 pids 清單皆僅 1 筆。
_MAX_TIE_LABELINGS = 720


def _pid_tie_classes(obj: Any, ctx: _NormCtx) -> list[list[int]]:
    """依各 PID 的正規化路徑多重集合分群：群組依簽章排序，群內 PID 依數值排序，結果與走訪順序無關。"""
    occurrences: dict[int, list[tuple[str, ...]]] = {}
    _collect_pid_paths(obj, ctx, occurrences)
    groups: dict[tuple[tuple[str, ...], ...], list[int]] = {}
    for pid, paths in occurrences.items():
        groups.setdefault(tuple(sorted(paths)), []).append(pid)
    return [sorted(groups[signature]) for signature in sorted(groups)]


def _assign_labels(ordered_classes: Iterable[Iterable[int]]) -> dict[int, str]:
    """依群組順序與群內順序編號為 `<PID:n>`。"""
    labels: dict[int, str] = {}
    for members in ordered_classes:
        for pid in members:
            labels[pid] = f"<PID:{len(labels)}>"
    return labels


def _pid_labelings(classes: Sequence[Sequence[int]]) -> Iterator[dict[int, str]]:
    """列舉同簽章群內的所有標籤排列。

    同簽章 PID 的正規化路徑完全相同，無法僅由路徑區分；它們之間的關聯（是否同屬一個 holder 群組）
    只能由渲染後的結構體現，故由 `_normalize` 逐一渲染並取最小者。排列數超過上限時只產生單一標籤。
    """
    if math.prod(math.factorial(len(members)) for members in classes) > _MAX_TIE_LABELINGS:
        yield _assign_labels(classes)
        return
    for orderings in itertools.product(*(itertools.permutations(members) for members in classes)):
        yield _assign_labels(orderings)


def _render_with_labels(obj: Any, ctx: _NormCtx, labels: Mapping[int, str]) -> Any:
    ctx.pid_aliases.clear()
    ctx.pid_aliases.update(labels)
    return _strip(obj, ctx)


def _strip(value: Any, ctx: _NormCtx, *, pid_field: bool = False) -> Any:
    """遞迴複製並套用 volatile 刪除、G1 刪除 message、實例根目錄與 PTY 路徑佔位替換，PID 以 ctx.pid_aliases 取代。

    PTY 路徑改名後若與同層其他鍵碰撞（例如兩個 `/dev/pts/N` 持有者），保留全部值（排序清單），
    使持有者數量與 PID 關係差異不致被靜默吞掉。
    """
    if pid_field and isinstance(value, int) and not isinstance(value, bool):
        return ctx.pid_aliases[value]
    if isinstance(value, Mapping):
        exception_message = ctx.g1 and value.get("error_code") == _EXCEPTION
        grouped: dict[Any, list[Any]] = {}
        for key, child in sorted(value.items(), key=lambda item: str(item[0])):
            if key in ctx.volatile:
                continue
            if exception_message and key == "message":
                continue
            new_key = _text(key, ctx) if isinstance(key, str) else key
            # pid_field 沿子樹傳遞：foreign_holders 的值為 {tty 路徑: 持有者 PID}，PID 位於其直接子值。
            # 子樹內只有 PID 欄位（schema 對 holder 物件 additionalProperties:false），故不會誤佔位非 PID 整數。
            is_pid_field = pid_field or (isinstance(key, str) and key in ctx.pid_fields)
            grouped.setdefault(new_key, []).append(_strip(child, ctx, pid_field=is_pid_field))
        return {
            key: children[0] if len(children) == 1 else sorted(children, key=_canonical)
            for key, children in grouped.items()
        }
    if isinstance(value, list):
        items = [_strip(item, ctx, pid_field=pid_field) for item in value]
        if pid_field and all(isinstance(item, int) and not isinstance(item, bool) for item in value):
            return sorted(items, key=_canonical)
        return items
    if isinstance(value, str):
        return _text(value, ctx)
    return value


def _normalize(obj: Any, *, rules: Mapping[str, Any], roots: Iterable[str]) -> Any:
    ctx = _NormCtx(
        volatile=frozenset(_str_list(rules.get("volatile_keys"), "volatile_keys")),
        pid_fields=frozenset(_str_list(rules.get("pid_fields"), "pid_fields")),
        pid_aliases={},
        g1=bool(rules.get("g1_exception_message")),
        roots=_root_patterns(roots),
        root_placeholder=str(rules.get("instance_root_placeholder") or _DEFAULT_ROOT_PLACEHOLDER),
        pty_placeholder=str(rules.get("pty_path_placeholder") or _DEFAULT_PTY_PLACEHOLDER),
    )
    classes = _pid_tie_classes(obj, ctx)
    candidates = (_render_with_labels(obj, ctx, labels) for labels in _pid_labelings(classes))
    normalized = min(candidates, key=_canonical)
    for _, json_path in _g2_entries(rules):
        normalized = _sort_at(normalized, json_path.split("."))
    return normalized


def normalize_response(obj: Any, *, rules: Mapping[str, Any], instance_root: str) -> Any:
    """依規則正規化單一回應。

    - G1：error_code 為 EXCEPTION 時刪除 message（rules.g1_exception_message）。
    - G2：rules.g2_set_paths 指向的清單排序後比較；呼叫端須先依子命令過濾 rules（見 `_rules_for_call`）。
    - volatile_keys 任意巢狀層級刪除；pid_fields 中的整數 PID 依正規化路徑多重集合分群，同群內取各排列的最小渲染結果編號，
      保留數量與跨欄位關聯，與走訪順序及 PID 數值無關。
    - 字串中的 instance_root 換成 rules.instance_root_placeholder；`/dev/pts/N` 換成 pty_path_placeholder。
    """
    return _normalize(obj, rules=rules, roots=(instance_root,))


def load_rules(rules_path: Path) -> dict[str, Any]:
    """讀取並驗證 contracts/ab_normalization.yaml；讀取、格式或未知鍵錯誤一律轉為 HARNESS_COMPARATOR_ERROR。"""
    try:
        document = yaml.safe_load(Path(rules_path).read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            raise ValueError(f"A/B 正規化規則必須為對映：{rules_path}")
        unknown = sorted(str(key) for key in document if key not in _RULE_KEYS)
        if unknown:
            raise ValueError(f"未知的 A/B 正規化規則鍵 {unknown}（新增鍵須經獨立審閱並同步本白名單）")
        if not isinstance(document.get("g1_exception_message", False), bool):
            raise ValueError("g1_exception_message 必須為布林值")
        _str_list(document.get("pid_fields"), "pid_fields")
        _str_list(document.get("volatile_keys"), "volatile_keys")
        _g2_entries(document)
        _poll_commands(document)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        raise HarnessError("HARNESS_COMPARATOR_ERROR", f"A/B 正規化規則無法載入：{exc}") from exc
    return dict(document)


def _contains_run(argv: Sequence[str], command: Sequence[str]) -> bool:
    size = len(command)
    return any(list(argv[index:index + size]) == list(command) for index in range(len(argv) - size + 1))


def _argv_of(record: Mapping[str, Any]) -> list[str]:
    argv = record.get("argv")
    return [str(token) for token in argv] if isinstance(argv, list) else []


def _argv_tail(record: Mapping[str, Any]) -> tuple[str, ...]:
    """比對用的 argv 尾端：去除 CLI 路徑與 endpoint 旗標及其值，`--cmd-id` 的值換成佔位字串。

    func-test 的完整 argv 為 `[CLI 路徑…, --socket|--endpoint, <值>, 子命令…]`；
    逾時紀錄的 argv 只有子命令、沒有 endpoint 旗標，此時整個 argv 即為尾端。
    """
    argv = _argv_of(record)
    for index, token in enumerate(argv):
        if token in _ENDPOINT_FLAGS:
            argv = argv[index + 2:]
            break
    tail = list(argv)
    for index in range(len(tail) - 1):
        if tail[index] == _CMD_ID_FLAG:
            tail[index + 1] = _CMD_ID_PLACEHOLDER
    return tuple(tail)


def _is_poll(tail: tuple[str, ...], poll_commands: Sequence[tuple[str, ...]]) -> bool:
    return any(len(tail) >= len(command) and tail[:len(command)] == command for command in poll_commands)


def _collapse_polls(calls: Sequence[Mapping[str, Any]],
                    poll_commands: Sequence[tuple[str, ...]], *, rules: Mapping[str, Any],
                    roots: Iterable[str]) -> list[Mapping[str, Any]]:
    """只收斂正規化後相同的連續成功輪詢；錯誤與語意狀態轉換原樣保留。"""
    collapsed: list[Mapping[str, Any]] = []
    for call in calls:
        tail = _argv_tail(call)
        if collapsed:
            previous = collapsed[-1]
            previous_tail = _argv_tail(previous)
            if (
                tail == previous_tail
                and _is_poll(tail, poll_commands)
                and _poll_succeeded(previous)
                and _poll_succeeded(call)
                and previous.get("rc") == call.get("rc")
            ):
                call_rules = _rules_for_call(rules, (_argv_of(previous), _argv_of(call)))
                previous_value = _normalized_payload(previous, rules=call_rules, roots=roots)
                call_value = _normalized_payload(call, rules=call_rules, roots=roots)
                if _first_difference(previous_value, call_value, "poll") is None:
                    collapsed[-1] = call
                    continue
        collapsed.append(call)
    return collapsed


def _poll_succeeded(call: Mapping[str, Any]) -> bool:
    """只把 CLI 正常退出且回應未明示 RPC 錯誤的輪詢視為成功。"""
    rc = call.get("rc")
    if not isinstance(rc, int) or isinstance(rc, bool) or rc != 0:
        return False
    payload = _payload_of(call)
    return not isinstance(payload, Mapping) or (
        payload.get("ok") is not False and not payload.get("error_code")
    )


def _rules_for_call(rules: Mapping[str, Any], argvs: Sequence[Sequence[str]]) -> dict[str, Any]:
    """只保留命中此次 CLI 呼叫子命令的 G2 條目（兩側 argv 任一命中即套用）。"""
    selected = [
        item for item in rules.get("g2_set_paths") or []
        if any(_contains_run(argv, item["command"]) for argv in argvs)
    ]
    return {**rules, "g2_set_paths": selected}


def _payload_of(record: Mapping[str, Any]) -> Any:
    """取 stdout 解析後的 JSON；stdout 為字串但非 JSON 時回傳去除外圍空白的原文；stdout 非字串時取 response。"""
    stdout = record.get("stdout")
    if isinstance(stdout, str):
        try:
            return json.loads(stdout)
        except ValueError:
            return stdout.strip()
    return record.get("response")


def _instance_roots(run_dir: Path, report_root: str, calls: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """回傳此 run 的實例根目錄集合（加上報告目錄）。

    func-test 的 DaemonHarness 以 `sw-func-*` 暫存目錄建立隔離實例，不在報告目錄之下；
    來源皆為此 run 自身的證據：
    - cli.ndjson argv 中 `--socket`／`--endpoint` 指向的 `<root>/run/serialwrapd.sock`（POSIX）；
    - daemon status 回應的 `wal.wal_dir`（`<root>/wal`）與 `mirror_path`（`<root>/wal/raw.mirror.log`）；
      Windows 的 endpoint 為 tcp://，裝置為 COMx，只能由此取得根目錄；
    - manifest.json `transport.daemon_side` 的 `<root>/by-id/<name>`（POSIX）。
    """
    roots = {report_root}
    for call in calls:
        argv = _argv_of(call)
        for flag in _ENDPOINT_FLAGS:
            for index, token in enumerate(argv[:-1]):
                if token != flag:
                    continue
                socket_path = Path(argv[index + 1])
                if socket_path.name == "serialwrapd.sock" and socket_path.parent.name == "run":
                    roots.add(str(socket_path.parent.parent))
        payload = _payload_of(call)
        if isinstance(payload, Mapping):
            wal = payload.get("wal")
            wal_dir = wal.get("wal_dir") if isinstance(wal, Mapping) else None
            if isinstance(wal_dir, str) and Path(wal_dir).name == "wal":
                roots.add(str(Path(wal_dir).parent))
            mirror = payload.get("mirror_path")
            if isinstance(mirror, str) and Path(mirror).parent.name == "wal":
                roots.add(str(Path(mirror).parent.parent))
    manifest = _read_json_object(run_dir / _MANIFEST) or {}
    transport = manifest.get("transport")
    daemon_side = transport.get("daemon_side") if isinstance(transport, Mapping) else None
    if isinstance(daemon_side, str) and Path(daemon_side).parent.name == "by-id":
        roots.add(str(Path(daemon_side).parent.parent))
    return tuple(sorted(roots))


# ---------------------------------------------------------------------------
# 差異
# ---------------------------------------------------------------------------


def _leaf_token(value: Any) -> str:
    """純量或型別不一致之值的比對記號（JSON 文字）：1 與 1.0 不同（arch §9.3 不得寬鬆），NaN 與 NaN 相同。"""
    return json.dumps(value, ensure_ascii=False, allow_nan=True, separators=(",", ":"))


def _first_difference(a: Any, b: Any, path: str) -> str | None:
    """回傳第一個不同之 JSON 點路徑（鍵依字典序、清單依索引）；相同回傳 None。"""
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        for key in sorted(set(a) | set(b), key=str):
            child = f"{path}.{key}"
            if key not in a or key not in b:
                return child
            found = _first_difference(a[key], b[key], child)
            if found is not None:
                return found
        return None
    if isinstance(a, list) and isinstance(b, list):
        for index, (left, right) in enumerate(zip(a, b)):
            found = _first_difference(left, right, f"{path}.{index}")
            if found is not None:
                return found
        return None if len(a) == len(b) else path
    return None if _leaf_token(a) == _leaf_token(b) else path


def _normalized_payload(record: Mapping[str, Any], *, rules: Mapping[str, Any], roots: Iterable[str]) -> Any:
    payload = _payload_of(record)
    tail = _argv_tail(record)
    if (
        tail[:len(_SCAN_SNAPSHOT_COMMAND)] == _SCAN_SNAPSHOT_COMMAND
        and _poll_succeeded(record)
        and isinstance(payload, Mapping)
        and payload.get("ok") is True
    ):
        # health.status 的契約將 foreign_holders 放在回應最上層；只移除此 /proc 快照，
        # 不把其他巢狀位置或其他命令中的同名欄位擴大成易變欄位。
        payload = {key: value for key, value in payload.items() if key != "foreign_holders"}
    return _normalize(payload, rules=rules, roots=roots)


def _difference(case: str, path: str, kind: str, case_changes: Mapping[str, str]) -> dict[str, Any]:
    return {"case": case, "path": path, "kind": kind, "registered_change": case_changes.get(case)}


def _compare_run(case: str, a_run: Path, b_run: Path, *, rules: Mapping[str, Any],
                 poll_commands: Sequence[tuple[str, ...]], a_root: str, b_root: str,
                 case_changes: Mapping[str, str]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if _verdict_of(a_run) != _verdict_of(b_run):
        found.append(_difference(case, "comparison.verdict", "verdict", case_changes))
    a_calls, _ = _read_calls(a_run)
    b_calls, _ = _read_calls(b_run)
    a_roots = _instance_roots(a_run, a_root, a_calls)
    b_roots = _instance_roots(b_run, b_root, b_calls)
    a_seq = _collapse_polls(a_calls, poll_commands, rules=rules, roots=a_roots)
    b_seq = _collapse_polls(b_calls, poll_commands, rules=rules, roots=b_roots)
    if len(a_seq) != len(b_seq):
        found.append(_difference(case, "calls.length", "value", case_changes))
    for index, (a_call, b_call) in enumerate(zip(a_seq, b_seq)):
        prefix = f"call[{index}]"
        if _argv_tail(a_call) != _argv_tail(b_call):
            # 命令不同（例如 device release 對 device attach）時，後續的 rc 與回應比對已無意義。
            found.append(_difference(case, f"{prefix}.argv", "argv", case_changes))
            continue
        if a_call.get("rc") != b_call.get("rc"):
            found.append(_difference(case, f"{prefix}.rc", "rc", case_changes))
        call_rules = _rules_for_call(rules, (_argv_of(a_call), _argv_of(b_call)))
        a_value = _normalized_payload(a_call, rules=call_rules, roots=a_roots)
        b_value = _normalized_payload(b_call, rules=call_rules, roots=b_roots)
        value_path = _first_difference(a_value, b_value, prefix)
        if value_path is not None:
            found.append(_difference(case, value_path, "value", case_changes))
    return found


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def diff_l15(a_dir: Path, b_dir: Path, *, registered: Mapping[str, str], case_changes: Mapping[str, str],
             required_case_ids: set[str], rules_path: Path) -> dict[str, Any]:
    """比較 A、B 兩側 L1.5 報告目錄並回傳 `stage3-ab-diff.v1` 結果。

    registered 為已登記之 SC 變更與其裁決種類，本函式不以它改變 ok；任何差異都使 ok 為 False，
    是否接受已登記差異由上層（ab_verdict）依此結果判定。
    case_changes 為 case_id → 已登記 SC 編號（來自 behavior_changes.yaml 的 case_ids 反查）。
    required_case_ids 為空時兩側完整性皆為 False（無法判定完整性）。
    A、B 目錄解析後相同或互為祖先時拋出 HarnessError("HARNESS_COMPARATOR_ERROR")：兩側不得指向同一份證據。
    規則檔讀取或格式錯誤拋出 HarnessError("HARNESS_COMPARATOR_ERROR")（由 load_rules 轉換）。
    """
    a_root = Path(a_dir).resolve()
    b_root = Path(b_dir).resolve()
    if a_root == b_root or a_root in b_root.parents or b_root in a_root.parents:
        raise HarnessError(
            "HARNESS_COMPARATOR_ERROR",
            f"A、B 報告目錄相同或互為祖先，無法獨立比較：{a_root}、{b_root}",
        )
    rules = load_rules(Path(rules_path))
    poll_commands = _poll_commands(rules)
    a_runs = load_case_runs(a_root)
    b_runs = load_case_runs(b_root)
    required = set(required_case_ids)

    problems: dict[str, list[str]] = {"a": [], "b": []}
    if not required:
        for key in ("a", "b"):
            problems[key].append("required_case_ids 為空，無法判定完整性")
    for key, side_runs in (("a", a_runs), ("b", b_runs)):
        for case in sorted(required):
            runs = side_runs.get(case)
            if not runs:
                problems[key].append(f"{case}: 缺少必要案例的 {_MODE} 報告")
                continue
            for run_dir in runs:
                problems[key].extend(_run_problems(case, run_dir, strict=True))
        for case, runs in sorted(side_runs.items()):
            if case in required:
                continue
            for run_dir in runs:
                problems[key].extend(_run_problems(case, run_dir, strict=False))
    for key, own, other in (("a", a_runs, b_runs), ("b", b_runs, a_runs)):
        for case in sorted(set(other) - set(own) - required):
            problems[key].append(f"{case}: 另一側有此案例、本側缺少")

    differences: list[dict[str, Any]] = []
    for case in sorted(set(a_runs) & set(b_runs)):
        a_by_name = {run.name: run for run in a_runs[case]}
        b_by_name = {run.name: run for run in b_runs[case]}
        for name in sorted(set(a_by_name) | set(b_by_name), key=_run_key):
            if name not in b_by_name:
                problems["b"].append(f"{case}/{_MODE}/{name}: B 側缺少對應 run")
            elif name not in a_by_name:
                problems["a"].append(f"{case}/{_MODE}/{name}: A 側缺少對應 run")
            else:
                differences.extend(_compare_run(case, a_by_name[name], b_by_name[name], rules=rules,
                                                poll_commands=poll_commands, a_root=str(a_root),
                                                b_root=str(b_root), case_changes=case_changes))
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in differences:
        marker = (item["case"], item["path"], item["kind"])
        if marker not in seen:
            seen.add(marker)
            unique.append(item)

    safety: list[str] = []
    for label, root in (("A", a_root), ("B", b_root)):
        for token in safety_tokens(root, label):
            if token not in safety:
                safety.append(token)

    integrity = {"a": not problems["a"], "b": not problems["b"]}
    ok = integrity["a"] and integrity["b"] and not safety and not unique
    return {
        "schema_version": DIFF_SCHEMA,
        "ok": bool(ok),
        "integrity": integrity,
        "integrity_problems": {"a": problems["a"], "b": problems["b"]},
        "safety_violations": safety,
        "differences": unique,
        "a_cases": {case: _aggregate_verdict([_verdict_of(run) for run in runs])
                    for case, runs in sorted(a_runs.items())},
        "b_cases": {case: _aggregate_verdict([_verdict_of(run) for run in runs])
                    for case, runs in sorted(b_runs.items())},
    }


__all__ = [
    "DIFF_SCHEMA",
    "diff_l15",
    "load_case_runs",
    "load_rules",
    "normalize_response",
    "safety_tokens",
]
