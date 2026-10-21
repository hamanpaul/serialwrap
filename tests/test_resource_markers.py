from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# 本檔混合 memory_only（AST 稽核）與 process_rpc（subprocess），依規則 2 模組層只放平台 marker
pytestmark = [pytest.mark.core_portable]

# 稽核函式本身含 "tmux" 字串常數（比對字串、從不啟動 tmux），豁免 interactive_desktop 的偵測要求
RESOURCE_AUDIT_WAIVERS = {"interactive_desktop": "只比對含 tmux 字樣的設定字串、從不啟動 tmux（稽核函式自身的字串常數）"}

TESTS = Path(__file__).resolve().parent
PLATFORM = {"core_portable", "posix_only", "windows_native"}
RESOURCE = {"memory_only", "process_rpc", "posix_pty", "windows_com", "physical_uart",
            "supervisor_runtime", "interactive_desktop"}
BUILTIN = {"parametrize", "skipif", "skip", "xfail", "usefixtures", "filterwarnings", "case"}
PLATFORM_COND = re.compile(r"sys\.platform|os\.name|platform\.system|AF_UNIX|openpty|geteuid|fcntl|msvcrt"
                           r"|win32|_IS_WIN|IS_WINDOWS|posix")


def _mark_names(node: ast.AST) -> set[str]:
    out = set()
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Attribute)
                and sub.value.attr == "mark" and isinstance(sub.value.value, ast.Name)
                and sub.value.value.id == "pytest"):
            out.add(sub.attr)
    return out


def _pytestmark(body: list[ast.stmt]) -> set[str]:
    out = set()
    for stmt in body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets):
            out |= _mark_names(stmt.value)
    return out


def _platform_skip(decorators: list[ast.expr], src: str) -> bool:
    for d in decorators:
        text = ast.get_source_segment(src, d) or ""
        if re.search(r"skip(if|If|Unless)\(", text) and PLATFORM_COND.search(text):
            return True
    return False


@dataclass
class Item:
    file: str
    name: str
    marks: set[str] = field(default_factory=set)
    platform_skip: bool = False
    scope: ast.AST | None = None


def _decor_marks(decorators: list[ast.expr]) -> set[str]:
    return set().union(*(_mark_names(d) for d in decorators))


def _module_skip(tree: ast.Module, src: str) -> bool:
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets):
            seg = ast.get_source_segment(src, stmt) or ""
            if "skip" in seg and PLATFORM_COND.search(seg):
                return True
    return False


def _items(path: Path) -> list[Item]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    module_marks = _pytestmark(tree.body)
    module_skip = _module_skip(tree, src)
    items: list[Item] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            items.append(Item(path.name, node.name,
                              module_marks | _decor_marks(node.decorator_list),
                              module_skip or _platform_skip(node.decorator_list, src), node))
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            cls_marks = _pytestmark(node.body) | _decor_marks(node.decorator_list)
            cls_skip = _platform_skip(node.decorator_list, src)
            for fn in node.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                    items.append(Item(path.name, f"{node.name}.{fn.name}",
                                      module_marks | cls_marks | _decor_marks(fn.decorator_list),
                                      module_skip or cls_skip or _platform_skip(fn.decorator_list, src), node))
    return items


def _all_items() -> list[Item]:
    return [it for p in sorted(TESTS.glob("test_*.py")) for it in _items(p)]


def _import_maps(tree: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
    modules: dict[str, str] = {}
    symbols: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                modules[bound] = alias.name if alias.asname else bound
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                symbols[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return modules, symbols


def _canonical_name(node: ast.AST, modules: dict[str, str], symbols: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return symbols.get(node.id, modules.get(node.id, node.id))
    if isinstance(node, ast.Attribute):
        parent = _canonical_name(node.value, modules, symbols)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _scoped_nodes(scope: ast.AST, tree: ast.Module) -> list[ast.AST]:
    helpers = {node.name: node for node in tree.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    names = {node.id for node in ast.walk(scope) if isinstance(node, ast.Name)}
    return [scope, *(helpers[name] for name in sorted(names & helpers.keys()) if helpers[name] is not scope)]


def _detected_resources(scope: ast.AST | None, tree: ast.Module) -> tuple[set[str], set[str]]:
    if scope is None:
        return set(), set()

    modules, symbols = _import_maps(tree)
    resources: set[str] = set()
    platform_features: set[str] = set()
    process_calls = {
        "subprocess.Popen", "subprocess.run", "subprocess.check_output", "subprocess.call",
        "socket.socket", "asyncio.start_server", "asyncio.start_unix_server",
        "asyncio.open_connection", "asyncio.open_unix_connection",
        "JsonRpcUnixServer", "TcpRpcServer",
    }

    for scoped in _scoped_nodes(scope, tree):
        for node in ast.walk(scoped):
            if isinstance(node, ast.Name):
                name = _canonical_name(node, modules, symbols)
                if name in {"pty", "termios"}:
                    resources.add("posix_pty")
                if name == "socketserver" or name.startswith("socketserver."):
                    resources.add("process_rpc")
                if name == "fcntl" or name.startswith("fcntl."):
                    platform_features.add("fcntl")
                if name == "msvcrt" or name.startswith("msvcrt."):
                    platform_features.add("msvcrt")
                if name.endswith(".AF_UNIX") or name == "AF_UNIX":
                    platform_features.add("AF_UNIX")
            elif isinstance(node, ast.Attribute):
                name = _canonical_name(node, modules, symbols)
                if name in {"os.openpty", "pty.openpty"}:
                    resources.add("posix_pty")
                if name == "socketserver" or name.startswith("socketserver."):
                    resources.add("process_rpc")
                if name.endswith(".AF_UNIX"):
                    platform_features.add("AF_UNIX")
                if name == "fcntl" or name.startswith("fcntl."):
                    platform_features.add("fcntl")
                if name == "msvcrt" or name.startswith("msvcrt."):
                    platform_features.add("msvcrt")
            elif isinstance(node, ast.Call):
                name = _canonical_name(node.func, modules, symbols)
                if name in process_calls:
                    resources.add("process_rpc")
                if name in {"os.openpty", "pty.openpty"}:
                    resources.add("posix_pty")
            elif isinstance(node, ast.Constant) and node.value == "tmux":
                resources.add("interactive_desktop")

    return resources, platform_features


def _resource_audit_waivers(tree: ast.Module) -> set[str]:
    for stmt in tree.body:
        targets = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
            value = stmt.value
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == "RESOURCE_AUDIT_WAIVERS" for target in targets):
            continue
        try:
            waivers = ast.literal_eval(value) if value is not None else None
        except (ValueError, TypeError):
            return set()
        if isinstance(waivers, dict):
            return {key for key, reason in waivers.items()
                    if isinstance(key, str) and isinstance(reason, str) and reason.strip()}
    return set()


def _synthetic_rule_findings(paths: list[Path]) -> dict[str, list[tuple[str, str]]]:
    findings: dict[str, list[tuple[str, str]]] = {
        "unregistered": [], "core_portable_skip": [], "unsupported_claim": [], "mixed_scope": [],
    }
    known = PLATFORM | RESOURCE | BUILTIN
    for path in paths:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        module_marks = _pytestmark(tree.body)
        for item in _items(path):
            if item.marks - known:
                findings["unregistered"].append((item.file, item.name))
            if "core_portable" in item.marks and item.platform_skip:
                findings["core_portable_skip"].append((item.file, item.name))
            if "physical_uart" in item.marks:
                findings["unsupported_claim"].append((item.file, item.name))
            elif "windows_com" in item.marks and "windows_native" not in item.marks:
                findings["unsupported_claim"].append((item.file, item.name))
            child = item.marks - module_marks
            if ((module_marks & RESOURCE and child & RESOURCE)
                    or (module_marks & PLATFORM and child & PLATFORM)):
                findings["mixed_scope"].append((item.file, item.name))
    return findings


@pytest.mark.memory_only
def test_every_item_has_exactly_one_platform_marker():
    bad = [(i.file, i.name, sorted(i.marks & PLATFORM)) for i in _all_items() if len(i.marks & PLATFORM) != 1]
    assert bad == []


@pytest.mark.memory_only
def test_every_item_has_resource_marker_and_memory_only_is_exclusive():
    bad = [(i.file, i.name) for i in _all_items()
           if not (i.marks & RESOURCE) or ("memory_only" in i.marks and len(i.marks & RESOURCE) > 1)]
    assert bad == []


@pytest.mark.memory_only
def test_only_registered_marker_names_used():
    bad = [(i.file, i.name, sorted(i.marks - PLATFORM - RESOURCE - BUILTIN)) for i in _all_items()
           if i.marks - PLATFORM - RESOURCE - BUILTIN]
    assert bad == []


@pytest.mark.memory_only
def test_detected_resources_are_marked():
    bad = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        waivers = _resource_audit_waivers(tree)
        for item in _items(path):
            resources, platform_features = _detected_resources(item.scope, tree)
            missing = (resources - waivers) - (item.marks & RESOURCE)
            if ("posix_pty" in resources or platform_features & {"AF_UNIX", "fcntl"}) and "posix_only" not in item.marks:
                missing.add("posix_only")
            if "msvcrt" in platform_features and "windows_native" not in item.marks:
                missing.add("windows_native")
            if missing:
                bad.append((item.file, item.name, sorted(missing)))
    assert bad == []


@pytest.mark.memory_only
def test_core_portable_items_have_no_platform_skip():
    bad = [(i.file, i.name) for i in _all_items() if "core_portable" in i.marks and i.platform_skip]
    assert bad == []


@pytest.mark.memory_only
def test_no_windows_com_or_physical_uart_claims_in_p0():
    # physical_uart 永遠禁止宣稱（使用數必須為 0）
    bad: list[tuple[str, str, str]] = [
        (i.file, i.name, "physical_uart_claimed") for i in _all_items() if "physical_uart" in i.marks
    ]

    # windows_com 只允許出現在模組層或 class 層 pytestmark 含 windows_native 的項目，且每個使用者同時帶 windows_native
    for i in _all_items():
        if "windows_com" in i.marks and "windows_native" not in i.marks:
            bad.append((i.file, i.name, "windows_com_missing_windows_native"))

    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mod_pm = _pytestmark(tree.body)
        if "windows_com" in mod_pm and "windows_native" not in mod_pm:
            bad.append((path.name, "<module>", "windows_com_in_module_pytestmark_without_windows_native"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if "windows_com" in _decor_marks(node.decorator_list):
                    bad.append((path.name, node.name, "windows_com_in_function_decorator"))
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                cls_pm = _pytestmark(node.body)
                cls_dec = _decor_marks(node.decorator_list)
                if "windows_com" in cls_dec:
                    bad.append((path.name, node.name, "windows_com_in_class_decorator"))
                if "windows_com" in cls_pm and "windows_native" not in cls_pm:
                    bad.append((path.name, node.name, "windows_com_in_class_pytestmark_without_windows_native"))
                for fn in node.body:
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if "windows_com" in _decor_marks(fn.decorator_list):
                            bad.append((path.name, f"{node.name}.{fn.name}", "windows_com_in_method_decorator"))

    assert bad == []


@pytest.mark.memory_only
def test_module_resource_marker_not_mixed():
    bad = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_marks = _pytestmark(tree.body)
        for item in _items(path):
            child = item.marks - module_marks
            if (module_marks & RESOURCE) and (child & RESOURCE):
                bad.append((item.file, item.name, "resource", sorted(child & RESOURCE)))
            if (module_marks & PLATFORM) and (child & PLATFORM):
                bad.append((item.file, item.name, "platform", sorted(child & PLATFORM)))
    assert bad == []


@pytest.mark.process_rpc
def test_markers_registered_in_conftest():
    # 帶 "tests" 參數，pytest 才會把 tests/conftest.py 當 initial conftest 載入
    out = subprocess.run([sys.executable, "-m", "pytest", "--markers", "-p", "no:cacheprovider", "tests"],
                         cwd=TESTS.parent, capture_output=True, text=True, timeout=300).stdout
    for name in PLATFORM | RESOURCE:
        assert f"@pytest.mark.{name}:" in out, name


@pytest.mark.memory_only
def test_audit_rules_detect_synthetic_violations(tmp_path: Path):
    fixtures = {
        "test_unregistered.py": """
import pytest
pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only, pytest.mark.not_registered]
def test_sample():
    pass
""",
        "test_core_skip.py": """
import sys
import pytest
pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]
@pytest.mark.skipif(sys.platform == 'win32', reason='synthetic platform skip')
def test_sample():
    pass
""",
        "test_unsupported_claim.py": """
import pytest
pytestmark = [pytest.mark.core_portable, pytest.mark.windows_com]
def test_sample():
    pass
""",
        "test_mixed_scope.py": """
import pytest
pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]
@pytest.mark.process_rpc
def test_sample():
    pass
""",
    }
    paths = []
    for name, source in fixtures.items():
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        paths.append(path)

    findings = _synthetic_rule_findings(paths)
    assert [file for file, _ in findings["unregistered"]] == ["test_unregistered.py"]
    assert [file for file, _ in findings["core_portable_skip"]] == ["test_core_skip.py"]
    assert [file for file, _ in findings["unsupported_claim"]] == ["test_unsupported_claim.py"]
    assert [file for file, _ in findings["mixed_scope"]] == ["test_mixed_scope.py"]
