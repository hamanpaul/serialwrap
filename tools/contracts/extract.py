"""以宣告式 AST 規則抽取產品實際輸出的錯誤碼與 Session 診斷值。"""
from __future__ import annotations

import ast
import datetime as _dt
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Iterator

import yaml

from .catalog import INTERFACES


@dataclass(frozen=True)
class SinkSpec:
    kind: str
    key: str | None = None
    keyword: str | None = None
    param: str | None = None
    callee: str | None = None
    index: int | None = None
    attr: str | None = None
    value_pattern: str | None = None
    implies_interface: str | None = None


@dataclass(frozen=True)
class RelaySpec:
    kind: str
    method: str | None = None
    arg: str | None = None
    key: str | None = None
    attr: str | None = None


@dataclass(frozen=True)
class PassthroughSpec:
    kind: str
    emits: str
    method: str | None = None
    arg: str | None = None
    key: str | None = None
    attr: str | None = None


@dataclass(frozen=True)
class NamespaceSpec:
    name: str
    sinks: tuple[SinkSpec, ...]
    relays: tuple[RelaySpec, ...]
    passthrough: tuple[PassthroughSpec, ...]


@dataclass(frozen=True)
class AllowUnresolved:
    namespace: str
    function: str
    expr: str
    unresolved_reason: str
    reason: str


@dataclass(frozen=True)
class ExcludeValue:
    namespace: str
    value: str
    category: str
    reason: str


@dataclass(frozen=True)
class ExtractionConfig:
    roots: tuple[str, ...]
    exclude: tuple[str, ...]
    code_pattern: str
    dynamic_prefix_pattern: str
    max_depth: int
    namespaces: tuple[NamespaceSpec, ...]
    exclude_functions: tuple[str, ...]
    allow_unresolved: tuple[AllowUnresolved, ...]
    exclude_values: tuple[ExcludeValue, ...]


@dataclass(frozen=True)
class Emission:
    namespace: str
    value: str
    kind: str
    file: str
    line: int
    function: str
    sink: str
    implies_interface: str | None


@dataclass(frozen=True)
class Unresolved:
    namespace: str
    file: str
    line: int
    function: str
    expr: str
    reason: str


@dataclass(frozen=True)
class ExtractionResult:
    emissions: tuple[Emission, ...]
    unresolved: tuple[Unresolved, ...]
    stale_allow_unresolved: tuple[AllowUnresolved, ...]
    stale_exclude_values: tuple[ExcludeValue, ...]
    applied_exclusions: tuple[tuple[ExcludeValue, int], ...]

    def values(self, namespace: str, *, include_passthrough: bool = False) -> frozenset[str]:
        """回傳命名空間的值；passthrough 預設不計入一般錯誤碼集合。"""
        return frozenset(
            emission.value
            for emission in self.emissions
            if emission.namespace == namespace
            and (include_passthrough or emission.kind != "passthrough")
        )


class ExtractionConfigError(Exception):
    """抽取設定無效或無法解析。"""


class ExtractionError(Exception):
    """掃描來源無法解析或讀取。"""


@dataclass(frozen=True)
class _Value:
    kind: str
    value: str | None = None
    reason: str | None = None


@dataclass
class _ResolutionState:
    namespace: NamespaceSpec
    max_depth: int
    expanded: dict[tuple[str, int, int, int | None, str], int] = field(default_factory=dict)
    stack: set[tuple[str, int, int, int | None, str]] = field(default_factory=set)
    truncated: set[tuple[str, int, int, int | None, str]] = field(default_factory=set)


@dataclass
class _Module:
    rel: str
    tree: ast.Module
    package: str
    top_assigns: dict[str, list[ast.expr]] = field(default_factory=dict)
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)


@dataclass
class _Class:
    name: str
    qualname: str
    module: _Module
    node: ast.ClassDef
    assigns: dict[str, list[ast.expr]] = field(default_factory=dict)
    instance_assigns: dict[str, list[_AttributeBinding]] = field(default_factory=dict)


@dataclass(frozen=True)
class _AttributeBinding:
    value: ast.expr
    context: _Context


@dataclass
class _Func:
    qualname: str
    name: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    module: _Module
    cls: _Class | None
    positional_params: tuple[str, ...]
    kwonly_params: tuple[str, ...]
    defaults: dict[str, ast.expr]
    default_context: _Context
    local_assigns: dict[str, list[tuple[ast.expr, int | None]]] = field(default_factory=dict)
    returns: tuple[ast.expr, ...] = ()
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    exception_types: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def params(self) -> tuple[str, ...]:
        return self.positional_params + self.kwonly_params


@dataclass(frozen=True)
class _Context:
    module: _Module
    func: _Func | None
    cls: _Class | None


@dataclass(frozen=True)
class _CallSite:
    context: _Context
    node: ast.Call


@dataclass
class _Index:
    modules: dict[str, _Module]
    modules_by_name: dict[str, _Module]
    classes_by_node: dict[int, _Class]
    funcs_by_node: dict[int, _Func]
    funcs_by_name: dict[str, list[_Func]]
    calls_by_name: dict[str, list[_CallSite]]
    alias_call_sites: dict[str, list[_CallSite]]
    thread_call_sites: dict[str, list[_CallSite]]
    contexts: dict[int, _Context]
    call_sites: list[_CallSite]
    classes_by_name: dict[str, list[_Class]]
    max_depth: int
    code_re: re.Pattern[str]
    dynamic_re: re.Pattern[str]


@dataclass(frozen=True)
class _Sink:
    module: _Module
    func: _Func | None
    cls: _Class | None
    value_expr: ast.expr
    location: ast.AST
    spec: SinkSpec

    @property
    def function(self) -> str:
        if self.func is not None:
            return self.func.qualname
        return f"{self.module.rel}::<module>"


def _canon_value(value: Any) -> str:
    if isinstance(value, ast.AST):
        return canon_expr(value)
    if isinstance(value, list):
        return "[" + ",".join(_canon_value(item) for item in value) + "]"
    return repr(value)


def canon_expr(node: ast.AST) -> str:
    """產生不依賴 ast.dump／ast.unparse 版本格式的節點表示。"""
    fields: list[str] = []
    for name in node._fields:
        if name in {"ctx", "type_comment", "kind"}:
            continue
        value = getattr(node, name)
        if value is None or value == []:
            continue
        fields.append(f"{name}={_canon_value(value)}")
    return f"{type(node).__name__}({','.join(fields)})"


class _UniqueKeyLoader(yaml.SafeLoader):
    """拒絕重複鍵，避免後出現的設定靜默覆蓋前值。"""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ExtractionConfigError(f"{label} 必須是字串鍵 mapping")
    return value


def _closed_mapping(
    value: object, label: str, required: set[str], optional: set[str] | None = None
) -> dict[str, Any]:
    mapping = _mapping(value, label)
    optional = optional or set()
    missing = required - mapping.keys()
    unknown = mapping.keys() - required - optional
    if missing:
        raise ExtractionConfigError(f"{label} 缺少必要欄位：{', '.join(sorted(missing))}")
    if unknown:
        raise ExtractionConfigError(f"{label} 含未知欄位：{', '.join(sorted(unknown))}")
    return mapping


def _string(value: object, label: str, *, nonempty: bool = True) -> str:
    if isinstance(value, (bool, _dt.date)) or not isinstance(value, str):
        raise ExtractionConfigError(f"{label} 必須是加引號的字串")
    if nonempty and not value.strip():
        raise ExtractionConfigError(f"{label} 不得為空字串")
    return value


def _string_list(value: object, label: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ExtractionConfigError(f"{label} 必須是{'非空 ' if nonempty else ''}list")
    return tuple(_string(item, f"{label}[{index}]") for index, item in enumerate(value))


def _compile_pattern(value: str, label: str) -> re.Pattern[str]:
    try:
        return re.compile(value)
    except re.error as exc:
        raise ExtractionConfigError(f"{label} 無效：{exc}") from exc


def _validate_glob_patterns(patterns: tuple[str, ...], label: str) -> None:
    for index, pattern in enumerate(patterns):
        normalized = pattern.replace("\\", "/")
        posix_path = PurePosixPath(normalized)
        windows_path = PureWindowsPath(pattern)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or ".." in posix_path.parts
        ):
            raise ExtractionConfigError(
                f"{label}[{index}] 必須是 repo 內的相對 glob，不得包含絕對路徑或 ..：{pattern}"
            )


_SINK_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "dict_key": ({"key"}, set()),
    "subscript_store": ({"key"}, set()),
    "call_keyword": ({"keyword"}, set()),
    "param_default": ({"param"}, set()),
    "call_arg": ({"callee"}, {"index", "keyword", "value_pattern", "implies_interface"}),
    "attribute_store": ({"attr"}, set()),
}
_RELAY_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "call_method_arg": ({"method", "arg"}, set()),
    "subscript_load": ({"key"}, set()),
    "attribute_load": ({"attr"}, set()),
}
_UNRESOLVED_REASONS = frozenset({
    "malformed_code", "unsupported_expr", "unknown_name", "unknown_callee",
    "no_return_value", "cycle", "max_depth",
})


def _parse_sink(value: object, label: str) -> SinkSpec:
    mapping = _mapping(value, label)
    kind = _string(mapping.get("kind"), f"{label}.kind")
    if kind not in _SINK_FIELDS:
        raise ExtractionConfigError(f"{label}.kind 未知：{kind}")
    required, optional = _SINK_FIELDS[kind]
    mapping = _closed_mapping(mapping, label, {"kind", *required}, optional)
    if kind == "call_arg" and (("index" in mapping) == ("keyword" in mapping)):
        raise ExtractionConfigError(f"{label} 的 call_arg 必須且只能設定 index 或 keyword")
    fields: dict[str, Any] = {"kind": kind}
    for name in required | optional:
        if name not in mapping:
            continue
        value = mapping[name]
        if name == "index":
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ExtractionConfigError(f"{label}.index 必須是非負整數")
            fields[name] = value
        elif name == "value_pattern":
            pattern = _string(value, f"{label}.value_pattern")
            _compile_pattern(pattern, f"{label}.value_pattern")
            fields[name] = pattern
        elif name == "implies_interface":
            interface = _string(value, f"{label}.{name}")
            if interface not in INTERFACES:
                raise ExtractionConfigError(
                    f"{label}.implies_interface 不在 catalog.INTERFACES：{interface}"
                )
            fields[name] = interface
        else:
            fields[name] = _string(value, f"{label}.{name}")
    return SinkSpec(**fields)


def _parse_relay(value: object, label: str) -> RelaySpec:
    mapping = _mapping(value, label)
    kind = _string(mapping.get("kind"), f"{label}.kind")
    if kind not in _RELAY_FIELDS:
        raise ExtractionConfigError(f"{label}.kind 未知：{kind}")
    required, optional = _RELAY_FIELDS[kind]
    mapping = _closed_mapping(mapping, label, {"kind", *required}, optional)
    fields = {"kind": kind}
    fields.update({name: _string(mapping[name], f"{label}.{name}") for name in required | optional if name in mapping})
    return RelaySpec(**fields)


def _parse_passthrough(value: object, label: str) -> PassthroughSpec:
    mapping = _mapping(value, label)
    kind = _string(mapping.get("kind"), f"{label}.kind")
    if kind not in _RELAY_FIELDS:
        raise ExtractionConfigError(f"{label}.kind 未知：{kind}")
    required, optional = _RELAY_FIELDS[kind]
    mapping = _closed_mapping(mapping, label, {"kind", "emits", *required}, optional)
    fields: dict[str, Any] = {
        "kind": kind,
        "emits": _string(mapping["emits"], f"{label}.emits"),
    }
    fields.update({name: _string(mapping[name], f"{label}.{name}") for name in required | optional if name in mapping})
    return PassthroughSpec(**fields)


def load_extraction_config(path: Path) -> ExtractionConfig:
    """載入封閉欄位設定，並拒絕重複鍵、未知節點及 YAML 型別陷阱。"""
    try:
        raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ExtractionConfigError(f"無法載入抽取設定 {path}: {exc}") from exc

    top = _closed_mapping(
        raw,
        "設定",
        {
            "schema_version", "roots", "exclude", "code_pattern", "dynamic_prefix_pattern",
            "max_depth", "exclude_functions", "namespaces", "allow_unresolved", "exclude_values",
        },
    )
    version = top["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ExtractionConfigError("schema_version 必須是整數 1")
    roots = _string_list(top["roots"], "roots", nonempty=True)
    exclude = _string_list(top["exclude"], "exclude")
    _validate_glob_patterns(roots, "roots")
    _validate_glob_patterns(exclude, "exclude")
    code_pattern = _string(top["code_pattern"], "code_pattern")
    dynamic_pattern = _string(top["dynamic_prefix_pattern"], "dynamic_prefix_pattern")
    _compile_pattern(code_pattern, "code_pattern")
    _compile_pattern(dynamic_pattern, "dynamic_prefix_pattern")
    max_depth = top["max_depth"]
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 1:
        raise ExtractionConfigError("max_depth 必須是正整數")
    exclude_functions = _string_list(top["exclude_functions"], "exclude_functions")

    raw_namespaces = _mapping(top["namespaces"], "namespaces")
    if not raw_namespaces:
        raise ExtractionConfigError("namespaces 不得為空")
    namespaces: list[NamespaceSpec] = []
    for name, raw_spec in raw_namespaces.items():
        _string(name, "namespace")
        spec = _closed_mapping(raw_spec, f"namespaces.{name}", {"sinks", "relays", "passthrough"})
        sinks_raw = spec["sinks"]
        relays_raw = spec["relays"]
        passthrough_raw = spec["passthrough"]
        if not isinstance(sinks_raw, list) or not isinstance(relays_raw, list) or not isinstance(passthrough_raw, list):
            raise ExtractionConfigError(f"namespaces.{name} 的 sinks／relays／passthrough 必須是 list")
        namespaces.append(NamespaceSpec(
            name=name,
            sinks=tuple(_parse_sink(item, f"namespaces.{name}.sinks[{i}]") for i, item in enumerate(sinks_raw)),
            relays=tuple(_parse_relay(item, f"namespaces.{name}.relays[{i}]") for i, item in enumerate(relays_raw)),
            passthrough=tuple(_parse_passthrough(item, f"namespaces.{name}.passthrough[{i}]") for i, item in enumerate(passthrough_raw)),
        ))
    namespace_names = {item.name for item in namespaces}

    allow_raw = top["allow_unresolved"]
    exclude_values_raw = top["exclude_values"]
    if not isinstance(allow_raw, list) or not isinstance(exclude_values_raw, list):
        raise ExtractionConfigError("allow_unresolved 與 exclude_values 必須是 list")
    allow: list[AllowUnresolved] = []
    for index, item in enumerate(allow_raw):
        label = f"allow_unresolved[{index}]"
        entry = _closed_mapping(
            item, label,
            {"namespace", "function", "expr", "unresolved_reason", "reason"},
        )
        namespace = _string(entry["namespace"], f"{label}.namespace")
        if namespace not in namespace_names:
            raise ExtractionConfigError(f"{label}.namespace 不存在：{namespace}")
        unresolved_reason = _string(entry["unresolved_reason"], f"{label}.unresolved_reason")
        if unresolved_reason not in _UNRESOLVED_REASONS:
            raise ExtractionConfigError(
                f"{label}.unresolved_reason 未知：{unresolved_reason}"
            )
        allow.append(AllowUnresolved(
            namespace, _string(entry["function"], f"{label}.function"),
            _string(entry["expr"], f"{label}.expr"), unresolved_reason,
            _string(entry["reason"], f"{label}.reason"),
        ))

    exclusions: list[ExcludeValue] = []
    for index, item in enumerate(exclude_values_raw):
        label = f"exclude_values[{index}]"
        entry = _closed_mapping(item, label, {"namespace", "value", "category", "reason"})
        namespace = _string(entry["namespace"], f"{label}.namespace")
        if namespace not in namespace_names:
            raise ExtractionConfigError(f"{label}.namespace 不存在：{namespace}")
        category = _string(entry["category"], f"{label}.category")
        if category not in {"not_a_code", "relay_artifact"}:
            raise ExtractionConfigError(f"{label}.category 必須是 not_a_code 或 relay_artifact")
        exclusions.append(ExcludeValue(
            namespace, _string(entry["value"], f"{label}.value"), category,
            _string(entry["reason"], f"{label}.reason"),
        ))

    return ExtractionConfig(
        roots, exclude, code_pattern, dynamic_pattern, max_depth, tuple(namespaces),
        exclude_functions, tuple(allow), tuple(exclusions),
    )


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _module_name(rel: str) -> str:
    without_suffix = rel[:-3] if rel.endswith(".py") else rel
    if without_suffix.endswith("/__init__"):
        without_suffix = without_suffix[:-9]
    return without_suffix.replace("/", ".")


def _module_package(rel: str) -> str:
    path = Path(rel)
    directory = path.parent if path.name != "__init__.py" else path.parent
    return ".".join(part for part in directory.parts if part not in {"", "."})


def _find_module(dotted: str, by_name: dict[str, _Module]) -> _Module | None:
    return by_name.get(dotted)


def _resolve_relative_name(module: _Module, level: int, imported_module: str | None) -> str | None:
    parts = module.package.split(".") if module.package else []
    up = level - 1
    if up > len(parts):
        return None
    if up:
        parts = parts[:-up]
    if imported_module:
        parts.extend(imported_module.split("."))
    return ".".join(parts)


def _bind_target(
    target: ast.expr,
    value: ast.expr,
    output: dict[str, list[tuple[ast.expr, int | None]]],
    index: int | None = None,
) -> None:
    if isinstance(target, ast.Name):
        output.setdefault(target.id, []).append((value, index))
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        for item_index, item in enumerate(target.elts):
            _bind_target(item, value, output, item_index if index is None else index)
        return
    if isinstance(target, ast.Starred):
        _bind_target(target.value, value, output, index)


class _AssignmentCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.assigns: dict[str, list[tuple[ast.expr, int | None]]] = defaultdict(list)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            _bind_target(target, node.value, self.assigns)
            self.visit(target)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            _bind_target(node.target, node.value, self.assigns)
            self.visit(node.value)
        self.visit(node.target)
        if node.annotation is not None:
            self.visit(node.annotation)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        _bind_target(node.target, node.value, self.assigns)
        self.visit(node.target)
        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        _bind_target(node.target, node.value, self.assigns)
        self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        if isinstance(node.iter, (ast.Tuple, ast.List)):
            for item in node.iter.elts:
                _bind_target(node.target, item, self.assigns)
        else:
            _bind_target(node.target, node.iter, self.assigns)
        self.visit(node.iter)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)  # 型別相同，沿用 for 目標的值傳遞。

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                _bind_target(item.optional_vars, item.context_expr, self.assigns)
            self.visit(item.context_expr)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)


def _scope_assignments(statements: Iterable[ast.stmt]) -> dict[str, list[ast.expr]]:
    collector = _AssignmentCollector()
    for statement in statements:
        collector.visit(statement)
    output: dict[str, list[ast.expr]] = {}
    for name, values in collector.assigns.items():
        output[name] = [expr for expr, index in values if index is None]
        indexed = [(expr, index) for expr, index in values if index is not None]
        if indexed:
            output[name].extend(expr for expr, _ in indexed)
    return output


def _function_assignments(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, list[tuple[ast.expr, int | None]]]:
    collector = _AssignmentCollector()
    for statement in node.body:
        collector.visit(statement)
    return dict(collector.assigns)


class _ReturnCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.values: list[ast.expr] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None:
            self.values.append(node.value)


def _function_returns(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ast.expr, ...]:
    collector = _ReturnCollector()
    for statement in node.body:
        collector.visit(statement)
    return tuple(collector.values)


class _ExceptionBindingCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.bindings: dict[str, list[str]] = defaultdict(list)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name and node.type is not None:
            exception_name = _callee_name(node.type)
            if exception_name:
                self.bindings[node.name].append(exception_name)
        self.generic_visit(node)


def _function_exception_bindings(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, tuple[str, ...]]:
    collector = _ExceptionBindingCollector()
    for statement in node.body:
        collector.visit(statement)
    return {name: tuple(dict.fromkeys(names)) for name, names in collector.bindings.items()}


def _function_instance_assigns(
    node: ast.FunctionDef | ast.AsyncFunctionDef, func: _Func,
) -> dict[str, list[_AttributeBinding]]:
    output: dict[str, list[_AttributeBinding]] = defaultdict(list)

    def bind(target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                bind(element, value)
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in {"self", "cls"}
        ):
            output[target.attr].append(
                _AttributeBinding(value, _Context(func.module, func, func.cls))
            )

    class Collector(ast.NodeVisitor):
        def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, child: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, child: ast.ClassDef) -> None:
            return

        def visit_Assign(self, child: ast.Assign) -> None:
            for target in child.targets:
                bind(target, child.value)
            self.generic_visit(child.value)

        def visit_AnnAssign(self, child: ast.AnnAssign) -> None:
            if child.value is not None:
                bind(child.target, child.value)
                self.generic_visit(child.value)

        def visit_AugAssign(self, child: ast.AugAssign) -> None:
            bind(child.target, child.value)
            self.generic_visit(child.value)

    collector = Collector()
    for statement in node.body:
        collector.visit(statement)
    return dict(output)


def _positional_and_defaults(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, ast.expr]]:
    args = node.args
    positional = tuple(arg.arg for arg in (*args.posonlyargs, *args.args))
    kwonly = tuple(arg.arg for arg in args.kwonlyargs)
    defaults: dict[str, ast.expr] = {}
    first_default = len(positional) - len(args.defaults)
    for name, value in zip(positional[first_default:], args.defaults):
        defaults[name] = value
    for name, value in zip(kwonly, args.kw_defaults):
        if value is not None:
            defaults[name] = value
    return positional, kwonly, defaults


def _collect_classes(module: _Module, index: _Index) -> list[_Class]:
    found: list[_Class] = []

    def visit(node: ast.AST, parents: tuple[str, ...]) -> None:
        if isinstance(node, ast.ClassDef):
            qualname = ".".join((*parents, node.name))
            info = _Class(node.name, qualname, module, node)
            info.assigns = _scope_assignments(node.body)
            index.classes_by_node[id(node)] = info
            index.classes_by_name.setdefault(node.name, []).append(info)
            found.append(info)
            for statement in node.body:
                visit(statement, (*parents, node.name))
            return
        for child in ast.iter_child_nodes(node):
            visit(child, parents)

    visit(module.tree, ())
    return found


class _IndexBuilder(ast.NodeVisitor):
    def __init__(self, index: _Index, module: _Module) -> None:
        self.index = index
        self.module = module
        self.func: _Func | None = None
        self.cls: _Class | None = None

    def visit(self, node: ast.AST) -> Any:
        self.index.contexts[id(node)] = _Context(self.module, self.func, self.cls)
        return super().visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        previous = self.cls
        self.cls = self.index.classes_by_node.get(id(node))
        for statement in node.body:
            self.visit(statement)
        self.cls = previous

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        default_context = _Context(self.module, self.func, self.cls)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)
        positional, kwonly, defaults = _positional_and_defaults(node)
        cls = self.cls
        if self.func is not None:
            parent_name = self.func.qualname.split("::", 1)[1]
            nested_prefix = f"{parent_name}.<locals>."
        elif cls is not None:
            nested_prefix = f"{cls.qualname}."
        else:
            nested_prefix = ""
        qualname = f"{self.module.rel}::{nested_prefix}{node.name}"
        func = _Func(
            qualname=qualname,
            name=node.name,
            node=node,
            module=self.module,
            cls=cls,
            positional_params=positional,
            kwonly_params=kwonly,
            defaults=defaults,
            default_context=default_context,
            local_assigns=_function_assignments(node),
            returns=_function_returns(node),
            exception_types=_function_exception_bindings(node),
        )
        func.imports = _top_level_imports(self.module, self.index, node.body)
        if cls is not None:
            for attr, bindings in _function_instance_assigns(node, func).items():
                cls.instance_assigns.setdefault(attr, []).extend(bindings)
        self.index.funcs_by_node[id(node)] = func
        self.index.funcs_by_name.setdefault(node.name, []).append(func)
        previous = self.func
        self.func = func
        for statement in node.body:
            self.visit(statement)
        self.func = previous

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _callee_name(node.func)
        site = _CallSite(_Context(self.module, self.func, self.cls), node)
        self.index.call_sites.append(site)
        if name:
            self.index.calls_by_name.setdefault(name, []).append(site)
        self.generic_visit(node)


def _top_level_imports(
    module: _Module, index: _Index, statements: Iterable[ast.stmt] | None = None,
) -> dict[str, tuple[str, str | None]]:
    imports: dict[str, tuple[str, str | None]] = {}

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                target_name = alias.name if alias.asname else alias.name.split(".", 1)[0]
                target = _find_module(target_name, index.modules_by_name)
                if target is not None:
                    imports[alias.asname or target_name.split(".", 1)[0]] = (target.rel, None)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.level:
                base_name = _resolve_relative_name(module, node.level, node.module)
            else:
                base_name = node.module or ""
            base = _find_module(base_name, index.modules_by_name)
            for alias in node.names:
                if alias.name == "*":
                    continue
                local_name = alias.asname or alias.name
                candidate = _find_module(
                    f"{base_name}.{alias.name}" if base_name else alias.name,
                    index.modules_by_name,
                )
                if candidate is not None:
                    imports[local_name] = (candidate.rel, None)
                elif base is not None:
                    imports[local_name] = (base.rel, alias.name)

    visitor = Visitor()
    if statements is None:
        visitor.visit(module.tree)
    else:
        for statement in statements:
            visitor.visit(statement)
    return imports


def _build_index(repo_root: Path, config: ExtractionConfig) -> _Index:
    root = Path(repo_root)
    selected: set[Path] = set()
    for pattern in config.roots:
        try:
            selected.update(path for path in root.glob(pattern) if path.is_file())
        except (NotImplementedError, OSError, ValueError) as exc:
            raise ExtractionConfigError(f"roots glob 無法展開 {pattern}：{exc}") from exc
    excluded: set[Path] = set()
    for pattern in config.exclude:
        try:
            for path in root.glob(pattern):
                if path.is_file():
                    excluded.add(path)
                elif path.is_dir():
                    excluded.update(child for child in path.rglob("*") if child.is_file())
        except (NotImplementedError, OSError, ValueError) as exc:
            raise ExtractionConfigError(f"exclude glob 無法展開 {pattern}：{exc}") from exc
    selected.difference_update(excluded)

    modules: dict[str, _Module] = {}
    modules_by_name: dict[str, _Module] = {}
    for path in sorted(selected, key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rel)
        except (OSError, UnicodeError, SyntaxError) as exc:
            location = f":{exc.lineno}" if isinstance(exc, SyntaxError) and exc.lineno else ""
            raise ExtractionError(f"無法解析 Python 檔案 {rel}{location}：{exc}") from exc
        module = _Module(rel, tree, _module_package(rel))
        modules[rel] = module
        modules_by_name[_module_name(rel)] = module

    index = _Index(
        modules=modules,
        modules_by_name=modules_by_name,
        classes_by_node={},
        funcs_by_node={},
        funcs_by_name={},
        calls_by_name={},
        alias_call_sites={},
        thread_call_sites={},
        contexts={},
        call_sites=[],
        classes_by_name={},
        max_depth=config.max_depth,
        code_re=re.compile(config.code_pattern),
        dynamic_re=re.compile(config.dynamic_prefix_pattern),
    )
    for module in modules.values():
        module.top_assigns = _scope_assignments(module.tree.body)
    for module in modules.values():
        _collect_classes(module, index)
    for module in modules.values():
        module.imports = _top_level_imports(module, index)
    for module in modules.values():
        _IndexBuilder(index, module).visit(module.tree)
    for site in index.call_sites:
        call = site.node
        if _callee_name(call.func) == "Thread":
            target = next((item.value for item in call.keywords if item.arg == "target"), None)
            if target is not None:
                for name in _callable_names(target, site.context, index):
                    index.thread_call_sites.setdefault(name, []).append(site)
            continue
        direct_name = _callee_name(call.func)
        for name in _callable_names(call.func, site.context, index):
            if name != direct_name:
                index.alias_call_sites.setdefault(name, []).append(site)
    return index


def _target_nodes(target: ast.expr) -> Iterator[ast.expr]:
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _target_nodes(element)
    elif isinstance(target, ast.Starred):
        yield from _target_nodes(target.value)
    else:
        yield target


def _matches_callee(call: ast.Call, wanted: str) -> bool:
    if isinstance(call.func, ast.Name):
        return call.func.id == wanted
    if isinstance(call.func, ast.Attribute):
        return call.func.attr == wanted
    return False


def _iter_sinks(index: _Index, namespace: NamespaceSpec, excluded: frozenset[str]) -> Iterator[_Sink]:
    for module in index.modules.values():
        for node in ast.walk(module.tree):
            context = index.contexts.get(id(node), _Context(module, None, None))
            func, cls = context.func, context.cls
            if func is not None and func.qualname in excluded:
                continue
            if isinstance(node, ast.Dict):
                for spec in namespace.sinks:
                    if spec.kind != "dict_key":
                        continue
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value == spec.key:
                            yield _Sink(module, func, cls, value, value, spec)
            if isinstance(node, ast.Call):
                for spec in namespace.sinks:
                    if spec.kind == "dict_key" and isinstance(node.func, ast.Name) and node.func.id == "dict":
                        for keyword in node.keywords:
                            if keyword.arg == spec.key:
                                yield _Sink(module, func, cls, keyword.value, keyword.value, spec)
                    elif spec.kind == "call_keyword":
                        for keyword in node.keywords:
                            if keyword.arg == spec.keyword:
                                yield _Sink(module, func, cls, keyword.value, keyword.value, spec)
                    elif spec.kind == "call_arg" and spec.callee and _matches_callee(node, spec.callee):
                        if spec.index is not None:
                            if spec.index >= len(node.args):
                                continue
                            value_expr = node.args[spec.index]
                        elif spec.keyword is not None:
                            keyword = next((item for item in node.keywords if item.arg == spec.keyword), None)
                            if keyword is None:
                                continue
                            value_expr = keyword.value
                        else:
                            continue
                        if spec.value_pattern is not None:
                            if not isinstance(value_expr, ast.Constant) or not isinstance(value_expr.value, str):
                                continue
                            if not _compile_pattern(spec.value_pattern, "value_pattern").fullmatch(value_expr.value):
                                continue
                        yield _Sink(module, func, cls, value_expr, value_expr, spec)
                    elif spec.kind == "subscript_store" and isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault":
                        if len(node.args) >= 2 and _constant_key(node.args[0], spec.key or ""):
                            yield _Sink(module, func, cls, node.args[1], node, spec)
                    elif spec.kind == "attribute_store" and isinstance(node.func, ast.Name) and node.func.id == "setattr":
                        if (
                            len(node.args) >= 3
                            and isinstance(node.args[1], ast.Constant)
                            and node.args[1].value == spec.attr
                        ):
                            yield _Sink(module, func, cls, node.args[2], node, spec)
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                if isinstance(node, ast.Assign):
                    targets = node.targets
                else:
                    targets = [node.target]
                value_expr = node.value
                if value_expr is None:
                    continue
                for target in targets:
                    for target_node in _target_nodes(target):
                        if isinstance(target_node, ast.Subscript) and isinstance(target_node.ctx, ast.Store):
                            for spec in namespace.sinks:
                                if spec.kind == "subscript_store" and isinstance(target_node.slice, ast.Constant) and target_node.slice.value == spec.key:
                                    yield _Sink(module, func, cls, value_expr, target_node, spec)
                        if isinstance(target_node, ast.Attribute) and isinstance(target_node.ctx, ast.Store):
                            for spec in namespace.sinks:
                                if spec.kind == "attribute_store" and target_node.attr == spec.attr:
                                    yield _Sink(module, func, cls, value_expr, target_node, spec)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                function = index.funcs_by_node.get(id(node))
                if function is None or function.qualname in excluded:
                    continue
                for spec in namespace.sinks:
                    if spec.kind != "param_default" or spec.param not in function.defaults:
                        continue
                    default = function.defaults[spec.param]
                    yield _Sink(module, function, function.cls, default, default, spec)


def _merge(values: Iterable[_Value]) -> set[_Value]:
    result = set(values)
    if any(value.kind != "unresolved" for value in result):
        result = {value for value in result if not (value.kind == "unresolved" and value.reason == "cycle")}
    return result


def _constant_key(node: ast.expr | None, expected: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == expected


def _call_method_relay(
    node: ast.Call, specs: Iterable[RelaySpec | PassthroughSpec],
) -> tuple[bool, tuple[ast.expr, ...], tuple[str, ...]]:
    if not isinstance(node.func, ast.Attribute):
        return False, (), ()
    for spec in specs:
        if spec.kind != "call_method_arg" or node.func.attr != spec.method or not node.args:
            continue
        if not _constant_key(node.args[0], spec.arg or ""):
            continue
        rest = list(node.args[1:])
        rest.extend(keyword.value for keyword in node.keywords if keyword.arg == "default")
        emits = (spec.emits,) if isinstance(spec, PassthroughSpec) else ()
        return True, tuple(rest), emits
    return False, (), ()


def _relay_value(
    node: ast.expr, namespace: NamespaceSpec
) -> tuple[bool, tuple[ast.expr, ...], tuple[str, ...]]:
    specs: tuple[RelaySpec | PassthroughSpec, ...] = (*namespace.relays, *namespace.passthrough)
    if isinstance(node, ast.Call):
        return _call_method_relay(node, specs)
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and isinstance(node.slice, ast.Constant):
        for spec in specs:
            if spec.kind == "subscript_load" and node.slice.value == spec.key:
                emits = (spec.emits,) if isinstance(spec, PassthroughSpec) else ()
                return True, (), emits
    if isinstance(node, ast.Attribute):
        for spec in specs:
            if spec.kind == "attribute_load" and node.attr == spec.attr:
                emits = (spec.emits,) if isinstance(spec, PassthroughSpec) else ()
                return True, (), emits
    return False, (), ()


def _callable_names(
    node: ast.expr, context: _Context, index: _Index,
    seen: set[tuple[str, str, str]] | None = None,
) -> set[str]:
    """解析可靜態辨識的函式名稱與同模組／函式內別名。"""
    seen = set() if seen is None else seen
    if isinstance(node, ast.Name):
        marker = (context.module.rel, context.func.qualname if context.func else "", node.id)
        if marker in seen:
            return set()
        seen.add(marker)
        if context.func is not None and node.id in context.func.local_assigns:
            return {
                name
                for expr, _ in context.func.local_assigns[node.id]
                for name in _callable_names(expr, context, index, seen)
            }
        imported = context.func.imports.get(node.id) if context.func is not None else None
        imported = imported or context.module.imports.get(node.id)
        if imported is not None:
            _, original = imported
            return {original} if original and index.funcs_by_name.get(original) else set()
        if node.id in context.module.top_assigns:
            return {
                name
                for expr in context.module.top_assigns[node.id]
                for name in _callable_names(
                    expr, _Context(context.module, None, None), index, seen,
                )
            }
        return {node.id} if index.funcs_by_name.get(node.id) else set()
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name):
            imported = context.func.imports.get(node.value.id) if context.func is not None else None
            imported = imported or context.module.imports.get(node.value.id)
            if imported is not None and imported[1] is None:
                target = index.modules.get(imported[0])
                if target is not None and any(
                    function.module is target and function.name == node.attr
                    for function in index.funcs_by_name.get(node.attr, ())
                ):
                    return {node.attr}
            if node.value.id in {"self", "cls"} and context.cls is not None:
                return {node.attr} if any(
                    function.cls is context.cls and function.name == node.attr
                    for function in index.funcs_by_name.get(node.attr, ())
                ) else set()
        return {node.attr} if index.funcs_by_name.get(node.attr) else set()
    return set()


def _function_call_sites(func: _Func, index: _Index) -> Iterator[tuple[_CallSite, bool]]:
    candidates: list[tuple[_CallSite, bool]] = [
        *((site, False) for site in index.calls_by_name.get(func.name, ())),
        *((site, False) for site in index.alias_call_sites.get(func.name, ())),
        *((site, True) for site in index.thread_call_sites.get(func.name, ())),
    ]
    if func.name == "__init__" and func.cls is not None:
        candidates.extend(
            (site, False)
            for site in index.calls_by_name.get(func.cls.name, ())
        )
    seen_nodes: set[int] = set()
    for site, thread_target in candidates:
        if id(site.node) in seen_nodes:
            continue
        seen_nodes.add(id(site.node))
        yield site, thread_target


def _function_argument(
    func: _Func,
    call: ast.Call,
    param: str,
    tuple_index: int | None,
    *,
    thread_target: bool = False,
) -> tuple[ast.expr | None, int | None]:
    if thread_target:
        try:
            param_index = func.positional_params.index(param)
        except ValueError:
            return None, None
        if func.positional_params and func.positional_params[0] in {"self", "cls"}:
            param_index -= 1
        args = next((item.value for item in call.keywords if item.arg == "args"), None)
        if args is None:
            return None, None
        if isinstance(args, (ast.Tuple, ast.List)):
            if not 0 <= param_index < len(args.elts):
                return None, None
            return args.elts[param_index], tuple_index
        return ast.Subscript(
            value=args,
            slice=ast.Constant(value=param_index),
            ctx=ast.Load(),
        ), tuple_index

    for keyword in call.keywords:
        if keyword.arg == param:
            return keyword.value, tuple_index
    if param in func.kwonly_params:
        return None, None
    try:
        param_index = func.positional_params.index(param)
    except ValueError:
        return None, None
    if func.positional_params and func.positional_params[0] in {"self", "cls"}:
        param_index -= 1
    if param_index < 0 or param_index >= len(call.args):
        return None, None
    return call.args[param_index], tuple_index


def _resolve_imported_name(
    imported: tuple[str, str | None], index: _Index, depth: int, strict: bool,
    state: _ResolutionState, tuple_index: int | None,
) -> set[_Value]:
    rel, original = imported
    module = index.modules.get(rel)
    if module is None or original is None:
        return {_Value("unresolved", reason="unknown_name")}
    values = module.top_assigns.get(original, ())
    if not values:
        return {_Value("unresolved", reason="unknown_name")}
    context = _Context(module, None, None)
    return _merge(
        value
        for expr in values
        for value in _resolve(expr, context, index, depth + 1, tuple_index, strict, state)
    )


def _resolve_name(
    node: ast.Name, context: _Context, index: _Index, depth: int, tuple_index: int | None,
    strict: bool, state: _ResolutionState,
) -> set[_Value]:
    name = node.id
    func = context.func
    candidates: list[tuple[ast.expr, _Context, int | None]] = []
    if func is not None and name in func.local_assigns:
        candidates.extend(
            (expr, context, assignment_index if assignment_index is not None else tuple_index)
            for expr, assignment_index in func.local_assigns[name]
        )
    if func is not None and name in func.params:
        default = func.defaults.get(name)
        if default is not None:
            candidates.append((default, func.default_context, tuple_index))
        for site, thread_target in _function_call_sites(func, index):
            argument, argument_index = _function_argument(
                func, site.node, name, tuple_index, thread_target=thread_target,
            )
            if argument is not None:
                candidates.append((argument, site.context, argument_index))
    if candidates:
        return _merge(
            value
            for expr, expr_context, expr_index in candidates
            for value in _resolve(expr, expr_context, index, depth + 1, expr_index, strict, state)
        )
    imported = func.imports.get(name) if func is not None else None
    if imported is not None:
        return _resolve_imported_name(imported, index, depth, strict, state, tuple_index)
    if func is None and context.cls is not None and name in context.cls.assigns:
        return _merge(
            value
            for expr in context.cls.assigns[name]
            for value in _resolve(
                expr, _Context(context.module, None, context.cls), index,
                depth + 1, tuple_index, strict, state,
            )
        )
    if name in context.module.top_assigns:
        values = context.module.top_assigns[name]
        return _merge(
            value
            for expr in values
            for value in _resolve(expr, _Context(context.module, None, None), index, depth + 1, tuple_index, strict, state)
        )
    imported = context.module.imports.get(name)
    if imported is not None:
        return _resolve_imported_name(imported, index, depth, strict, state, tuple_index)
    return {_Value("unresolved", reason="unknown_name")}


def _resolve_attribute(
    node: ast.Attribute, context: _Context, index: _Index, depth: int, tuple_index: int | None,
    strict: bool, state: _ResolutionState,
) -> set[_Value]:
    if isinstance(node.value, ast.Name):
        receiver = node.value.id
        imported = context.func.imports.get(receiver) if context.func is not None else None
        imported = imported or context.module.imports.get(receiver)
        if imported is not None:
            rel, original = imported
            target_module = index.modules.get(rel)
            if target_module is not None:
                target_name = node.attr if original is None else None
                if target_name is not None:
                    expressions = target_module.top_assigns.get(target_name, ())
                    if expressions:
                        target_context = _Context(target_module, None, None)
                        return _merge(
                            value
                            for expr in expressions
                            for value in _resolve(expr, target_context, index, depth + 1, tuple_index, strict, state)
                        )
        if receiver in {"self", "cls"} and context.cls is not None:
            candidates = [
                (expr, _Context(context.cls.module, None, context.cls))
                for expr in context.cls.assigns.get(node.attr, ())
            ]
            candidates.extend(
                (binding.value, binding.context)
                for binding in context.cls.instance_assigns.get(node.attr, ())
            )
            if candidates:
                return _merge(
                    value
                    for expr, target_context in candidates
                    for value in _resolve(expr, target_context, index, depth + 1, tuple_index, strict, state)
                )
    return {_Value("unresolved", reason="unknown_name")}


def _exception_code_values(
    node: ast.Attribute,
    context: _Context,
    index: _Index,
    depth: int,
    tuple_index: int | None,
    strict: bool,
    state: _ResolutionState,
) -> set[_Value] | None:
    if (
        node.attr != "code"
        or not isinstance(node.value, ast.Name)
        or context.func is None
        or node.value.id not in context.func.exception_types
    ):
        return None
    values: set[_Value] = set()
    found = False
    configured_callees = {
        sink.callee
        for sink in state.namespace.sinks
        if sink.kind == "call_arg" and sink.callee is not None
    }
    for exception_name in context.func.exception_types[node.value.id]:
        if exception_name in configured_callees:
            continue
        for cls in index.classes_by_name.get(exception_name, ()):
            for binding in cls.instance_assigns.get("code", ()):
                if binding.context.func is None or binding.context.func.name != "__init__":
                    continue
                found = True
                values.update(
                    _resolve(binding.value, binding.context, index, depth + 1, tuple_index, strict, state)
                )
    return _merge(values) if found else None


def _dictionary_sources(
    expr: ast.expr,
    context: _Context,
    index: _Index,
    seen: frozenset[tuple[str, str, str]],
) -> tuple[list[tuple[ast.Dict, _Context]], set[_Value]]:
    """回傳可確認的 Dict 與其定義 scope，並保留未能解析的來源分支。"""
    if isinstance(expr, ast.Dict):
        return [(expr, context)], set()
    if isinstance(expr, ast.Name):
        marker = (context.module.rel, context.func.qualname if context.func else "", expr.id)
        if marker in seen:
            return [], {_Value("unresolved", reason="cycle")}
        next_seen = seen | {marker}
        candidates: list[tuple[ast.expr, _Context]] = []
        func = context.func
        if func is not None and expr.id in func.local_assigns:
            candidates.extend((value, context) for value, _ in func.local_assigns[expr.id])
        if func is not None and expr.id in func.params:
            default = func.defaults.get(expr.id)
            if default is not None:
                candidates.append((default, func.default_context))
            for site, thread_target in _function_call_sites(func, index):
                argument, _ = _function_argument(func, site.node, expr.id, None, thread_target=thread_target)
                if argument is not None:
                    candidates.append((argument, site.context))
        if candidates:
            sources: list[tuple[ast.Dict, _Context]] = []
            unresolved: set[_Value] = set()
            for candidate, candidate_context in candidates:
                found, failed = _dictionary_sources(candidate, candidate_context, index, next_seen)
                sources.extend(found)
                unresolved.update(failed)
            return sources, unresolved

        imported = func.imports.get(expr.id) if func is not None else None
        imported = imported or context.module.imports.get(expr.id)
        if imported is not None:
            rel, original = imported
            module = index.modules.get(rel)
            if module is None or original is None:
                return [], {_Value("unresolved", reason="unknown_name")}
            candidates = [(value, _Context(module, None, None)) for value in module.top_assigns.get(original, ())]
            if not candidates:
                return [], {_Value("unresolved", reason="unknown_name")}
            return _dictionary_sources_from_candidates(candidates, index, next_seen)

        if expr.id in context.module.top_assigns:
            candidates = [
                (value, _Context(context.module, None, None))
                for value in context.module.top_assigns[expr.id]
            ]
            return _dictionary_sources_from_candidates(candidates, index, next_seen)
        return [], {_Value("unresolved", reason="unknown_name")}
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        receiver = expr.value.id
        imported = context.func.imports.get(receiver) if context.func is not None else None
        imported = imported or context.module.imports.get(receiver)
        if imported is not None and imported[1] is None:
            module = index.modules.get(imported[0])
            if module is not None:
                candidates = [
                    (value, _Context(module, None, None))
                    for value in module.top_assigns.get(expr.attr, ())
                ]
                if candidates:
                    return _dictionary_sources_from_candidates(candidates, index, seen)
                return [], {_Value("unresolved", reason="unknown_name")}
        if receiver in {"self", "cls"} and context.cls is not None:
            candidates = [
                (value, _Context(context.cls.module, None, context.cls))
                for value in context.cls.assigns.get(expr.attr, ())
            ]
            candidates.extend(
                (binding.value, binding.context)
                for binding in context.cls.instance_assigns.get(expr.attr, ())
            )
            if candidates:
                return _dictionary_sources_from_candidates(candidates, index, seen)
            return [], {_Value("unresolved", reason="unknown_name")}
    if isinstance(expr, ast.Call):
        callee = _callee_name(expr.func)
        functions = index.funcs_by_name.get(callee or "", ())
        if not functions:
            return [], {_Value("unresolved", reason="unknown_callee")}
        sources: list[tuple[ast.Dict, _Context]] = []
        unresolved: set[_Value] = set()
        for function in functions:
            returned = [
                expression for expression in function.returns
                if not (isinstance(expression, ast.Constant) and expression.value is None)
            ]
            if not returned:
                unresolved.add(_Value("unresolved", reason="no_return_value"))
                continue
            for expression in returned:
                found, failed = _dictionary_sources(
                    expression,
                    _Context(function.module, function, function.cls),
                    index,
                    seen,
                )
                sources.extend(found)
                unresolved.update(failed)
        return sources, unresolved
    return [], {_Value("unresolved", reason="unknown_name")}


def _dictionary_sources_from_candidates(
    candidates: Iterable[tuple[ast.expr, _Context]],
    index: _Index,
    seen: frozenset[tuple[str, str, str]],
) -> tuple[list[tuple[ast.Dict, _Context]], set[_Value]]:
    sources: list[tuple[ast.Dict, _Context]] = []
    unresolved: set[_Value] = set()
    for candidate, candidate_context in candidates:
        found, failed = _dictionary_sources(candidate, candidate_context, index, seen)
        sources.extend(found)
        unresolved.update(failed)
    return sources, unresolved


def _resolve(
    node: ast.expr,
    context: _Context,
    index: _Index,
    depth: int,
    tuple_index: int | None,
    strict: bool,
    state: _ResolutionState,
) -> set[_Value]:
    key = (
        context.module.rel,
        getattr(node, "lineno", 0),
        getattr(node, "col_offset", 0),
        tuple_index,
        type(node).__name__,
    )
    if depth > state.max_depth:
        state.truncated.add(key)
        return set()
    if key in state.stack:
        return {_Value("unresolved", reason="cycle")}
    previous_depth = state.expanded.get(key)
    if previous_depth is not None and previous_depth <= depth:
        return set()
    state.expanded[key] = depth
    state.stack.add(key)
    try:
        return _resolve_inner(node, context, index, depth, tuple_index, strict, state)
    finally:
        state.stack.remove(key)


def _resolve_inner(
    node: ast.expr,
    context: _Context,
    index: _Index,
    depth: int,
    tuple_index: int | None,
    strict: bool,
    state: _ResolutionState,
) -> set[_Value]:
    relayed, defaults, passthrough_ids = _relay_value(node, state.namespace)
    if relayed:
        values = {_Value("relay")}
        values.update(_Value("passthrough", value=emits) for emits in passthrough_ids)
        if isinstance(node, ast.Attribute):
            exception_values = _exception_code_values(
                node, context, index, depth, tuple_index, strict, state,
            )
            if exception_values is not None:
                values.update(exception_values)
        values.update(
            value
            for expr in defaults
            for value in _resolve(expr, context, index, depth + 1, None, strict, state)
        )
        return _merge(values)

    if isinstance(node, ast.Constant):
        value = node.value
        if value is None or isinstance(value, (bool, int, float, bytes)):
            return set()
        if not isinstance(value, str) or value == "":
            return set()
        if index.code_re.fullmatch(value):
            return {_Value("code", value=value)}
        return {_Value("unresolved", reason="malformed_code")} if strict else set()

    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and index.dynamic_re.match(first.value):
            return {_Value("dynamic_prefix", value=f"{first.value}*")}
        return {_Value("unresolved", reason="unsupported_expr")}

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str) and index.dynamic_re.match(node.left.value):
            return {_Value("dynamic_prefix", value=f"{node.left.value}*")}
        return {_Value("unresolved", reason="unsupported_expr")}

    if isinstance(node, ast.BoolOp):
        return _merge(
            value
            for expression in node.values
            for value in _resolve(expression, context, index, depth, tuple_index, strict, state)
        )

    if isinstance(node, ast.IfExp):
        return _merge(
            value
            for expression in (node.body, node.orelse)
            for value in _resolve(expression, context, index, depth, tuple_index, strict, state)
        )

    if isinstance(node, ast.Name):
        return _resolve_name(node, context, index, depth, tuple_index, strict, state)

    if isinstance(node, ast.Attribute):
        return _resolve_attribute(node, context, index, depth, tuple_index, strict, state)

    if isinstance(node, ast.Dict):
        return {_Value("unresolved", reason="unsupported_expr")}

    if isinstance(node, (ast.Tuple, ast.List)):
        if tuple_index is not None:
            if 0 <= tuple_index < len(node.elts):
                return _resolve(node.elts[tuple_index], context, index, depth + 1, None, strict, state)
            return {_Value("unresolved", reason="unsupported_expr")}
        return {_Value("unresolved", reason="unsupported_expr")}

    if isinstance(node, ast.Subscript):
        if isinstance(node.value, ast.Dict) and isinstance(node.slice, ast.Constant):
            selected = [
                value for key_expr, value in zip(node.value.keys, node.value.values)
                if isinstance(key_expr, ast.Constant) and key_expr.value == node.slice.value and value is not None
            ]
            return _merge(
                item
                for expr in selected
                for item in _resolve(expr, context, index, depth + 1, None, strict, state)
            )
        if isinstance(node.value, (ast.Tuple, ast.List)) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
            item_index = node.slice.value
            if -len(node.value.elts) <= item_index < len(node.value.elts):
                return _resolve(node.value.elts[item_index], context, index, depth + 1, None, strict, state)
        return {_Value("unresolved", reason="unsupported_expr")}

    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "str" and len(node.args) == 1:
            return _resolve(node.args[0], context, index, depth + 1, None, strict, state)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            sources, source_unresolved = _dictionary_sources(
                node.func.value, context, index, frozenset(),
            )
            fallback = [argument for argument in node.args[1:]]
            fallback.extend(keyword.value for keyword in node.keywords if keyword.arg == "default")
            if sources:
                values = set(source_unresolved)
                values.update(
                    item
                    for dictionary, dictionary_context in sources
                    for expr in dictionary.values
                    if expr is not None
                    for item in _resolve(
                        expr, dictionary_context, index, depth + 1, None, strict, state,
                    )
                )
                values.update(
                    item
                    for expr in fallback
                    for item in _resolve(expr, context, index, depth + 1, None, strict, state)
                )
                return _merge(values)
        name = _callee_name(node.func)
        functions = index.funcs_by_name.get(name or "", ())
        if not functions:
            return {_Value("unresolved", reason="unknown_callee")}
        results: set[_Value] = set()
        for function in functions:
            returned = [
                expression for expression in function.returns
                if not (isinstance(expression, ast.Constant) and expression.value is None)
            ]
            if not returned:
                results.add(_Value("unresolved", reason="no_return_value"))
                continue
            func_context = _Context(function.module, function, function.cls)
            results.update(
                value
                for expression in returned
                for value in _resolve(expression, func_context, index, depth + 1, tuple_index, strict, state)
            )
        return _merge(results)

    return {_Value("unresolved", reason="unsupported_expr")}


def _apply_allow_and_exclude(
    emissions: list[Emission], unresolved: list[Unresolved], config: ExtractionConfig,
) -> ExtractionResult:
    remaining = list(unresolved)
    stale_allow: list[AllowUnresolved] = []
    for allowed in config.allow_unresolved:
        matches = [
            item for item in remaining
            if (item.namespace, item.function, item.expr, item.reason)
            == (allowed.namespace, allowed.function, allowed.expr, allowed.unresolved_reason)
        ]
        if not matches:
            stale_allow.append(allowed)
        else:
            matched = set(matches)
            remaining = [item for item in remaining if item not in matched]

    current = list(emissions)
    applied: list[tuple[ExcludeValue, int]] = []
    stale_exclusions: list[ExcludeValue] = []
    for exclusion in config.exclude_values:
        kept = [
            item for item in current
            if not (item.namespace == exclusion.namespace and item.value == exclusion.value)
        ]
        removed = len(current) - len(kept)
        if removed:
            applied.append((exclusion, removed))
            current = kept
        else:
            stale_exclusions.append(exclusion)

    current.sort(key=lambda item: (item.namespace, item.value, item.file, item.line, item.sink, item.kind))
    remaining.sort(key=lambda item: (item.namespace, item.file, item.line, item.function, item.expr, item.reason))
    return ExtractionResult(
        tuple(current), tuple(remaining), tuple(stale_allow), tuple(stale_exclusions), tuple(applied)
    )


def extract(repo_root: Path, config: ExtractionConfig) -> ExtractionResult:
    """依設定掃描 AST，傳遞可解析的字面值並保留所有未解析原因。"""
    index = _build_index(Path(repo_root), config)
    emissions: list[Emission] = []
    unresolved: list[Unresolved] = []
    emitted_keys: set[tuple[str, str, str, int, str]] = set()
    excluded = frozenset(config.exclude_functions)

    for namespace in config.namespaces:
        for sink in _iter_sinks(index, namespace, excluded):
            context = (
                sink.func.default_context
                if sink.spec.kind == "param_default" and sink.func is not None
                else _Context(sink.module, sink.func, sink.cls)
            )
            strict = sink.spec.value_pattern is None
            state = _ResolutionState(namespace, index.max_depth)
            resolved = _resolve(sink.value_expr, context, index, 0, None, strict, state)
            if any(key not in state.expanded for key in state.truncated):
                resolved.add(_Value("unresolved", reason="max_depth"))
            for value in resolved:
                if value.kind in {"relay", "unresolved"}:
                    if value.kind == "unresolved":
                        unresolved.append(Unresolved(
                            namespace.name,
                            sink.module.rel,
                            getattr(sink.location, "lineno", getattr(sink.value_expr, "lineno", 1)),
                            sink.function,
                            canon_expr(sink.value_expr),
                            value.reason or "unsupported_expr",
                        ))
                    continue
                if value.value is None:
                    continue
                line = getattr(sink.location, "lineno", getattr(sink.value_expr, "lineno", 1))
                dedupe_key = (namespace.name, value.value, sink.module.rel, line, sink.spec.kind)
                if dedupe_key in emitted_keys:
                    continue
                emitted_keys.add(dedupe_key)
                emissions.append(Emission(
                    namespace.name, value.value, value.kind, sink.module.rel, line,
                    sink.function, sink.spec.kind, sink.spec.implies_interface,
                ))

    return _apply_allow_and_exclude(emissions, unresolved, config)
