"""uart_stdio_bridge 產品邊界與依賴守衛的失敗測試（P2-T1，OD-07、§15.4.2）。

所有對 `uart_stdio_bridge` 的 import 皆在函式內進行（P0 慣例），
套件尚未存在時整個檔仍可收集，紅燈為語意失敗（缺少行為），不是收集錯誤。
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

TOOL_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = TOOL_ROOT.parents[1]
FORBIDDEN_TOP = {"sw_core", "serialwrapd", "serialwrap", "realhw", "serialwrap_regression"}


def _src_files() -> list[pathlib.Path]:
    return sorted((TOOL_ROOT / "src").rglob("*.py"))


def test_src_exists():
    assert (TOOL_ROOT / "src" / "uart_stdio_bridge" / "__init__.py").is_file()


def test_no_product_import_ast():
    # 非空守衛：src 不存在或為空時不得空轉通過（否則邊界測試永遠綠燈）
    files = _src_files()
    assert files, "src/ 下沒有任何 .py 檔，無法檢查產品邊界"
    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Call):
                fn = node.func
                fname = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if fname in {"import_module", "__import__"}:
                    names = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            for name in names:
                if name.split(".")[0] in FORBIDDEN_TOP:
                    offenders.append(f"{path.relative_to(TOOL_ROOT)}:{node.lineno}:{name}")
    assert offenders == []


def test_import_all_modules_without_product():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import pkgutil, importlib, uart_stdio_bridge as p;"
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(p.__path__, 'uart_stdio_bridge.')"
        " if not m.name.endswith('.__main__') and (not m.name.endswith('.win_serial') or sys.platform == 'win32')];"
        "bad=[m for m in sys.modules if m=='sw_core' or m.startswith('sw_core.')];"
        "print(bad); raise SystemExit(1 if bad else 0)"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run(
        [sys.executable, "-I", "-c", code, str(TOOL_ROOT / "src")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_runtime_deps_pinned_and_minimal():
    import tomllib  # 延後 import：Python 3.11+ 才有；開發測試需 3.11+（CI 3.12）

    data = tomllib.loads((TOOL_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "serialwrap-uart-stdio-bridge"
    assert data["project"]["requires-python"] == ">=3.10"
    assert data["project"]["dependencies"] == ['pyserial==3.5; sys_platform == "win32"']
    assert sorted(data["project"]["optional-dependencies"]["dev"]) == sorted(
        ["pytest==9.0.3", "PyYAML==6.0.3", "jsonschema==4.10.3"]
    )


def test_harness_codes_disjoint_from_product_codes():
    from uart_stdio_bridge.harness_codes import HARNESS_CODES

    assert len(HARNESS_CODES) == len(set(HARNESS_CODES))
    assert all(c.startswith("HARNESS_") for c in HARNESS_CODES)
    contract = REPO_ROOT / "contracts" / "error_codes.yaml"
    text = contract.read_text(encoding="utf-8")  # P0 產出；不存在即紅燈（不得 SKIP）
    assert "HARNESS_" not in text
