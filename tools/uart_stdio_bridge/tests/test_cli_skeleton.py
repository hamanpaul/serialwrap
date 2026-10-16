"""uart_stdio_bridge 命令列骨架與 identity 演算法的失敗測試（P2-T1）。

依 Task 1 Interfaces 撰寫；所有對 `uart_stdio_bridge` 的 import 皆在函式內進行，
紅燈為語意失敗（缺少行為），不是收集錯誤。
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

TOOL_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = TOOL_ROOT.parents[1]
SRC = TOOL_ROOT / "src"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GIT_REV_RE = re.compile(r"[0-9a-f]{40}")


def _run_bridge(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    return subprocess.run(
        [sys.executable, "-m", "uart_stdio_bridge", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(TOOL_ROOT),
        timeout=60,
    )


@contextlib.contextmanager
def _scratch(path: pathlib.Path, content: bytes):
    """寫入暫存內容；結束時還原原檔（或刪除新建檔，以及因此新建的目錄）。"""
    missing_dirs: list[pathlib.Path] = []
    probe = path.parent
    while not probe.exists():
        missing_dirs.append(probe)
        probe = probe.parent  # 由深到淺記錄，清理時依序 rmdir
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_bytes() if path.exists() else None
    path.write_bytes(content)
    try:
        yield
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)
        for d in missing_dirs:
            with contextlib.suppress(OSError):
                d.rmdir()


def test_version_json():
    r = _run_bridge("version")
    assert r.returncode == 0, r.stderr
    assert r.stdout.endswith("\n") and r.stdout.count("\n") == 1
    obj = json.loads(r.stdout)
    assert set(obj) == {"name", "tree_sha256", "version", "wire_schema"}
    assert obj["name"] == "uart-stdio-bridge"
    assert obj["version"] == "0.1.0"
    assert obj["wire_schema"] == "bridge.v1"
    assert SHA256_RE.fullmatch(obj["tree_sha256"])
    assert r.stdout == json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


def test_unknown_subcommand_exit2():
    r = _run_bridge("nosuch")
    assert r.returncode == 2
    assert "usage:" in r.stderr


def test_tree_sha256_stable_and_excludes_tests():
    from uart_stdio_bridge.identity import tool_tree_sha256

    first = tool_tree_sha256()
    assert tool_tree_sha256() == first
    with contextlib.ExitStack() as stack:
        # 排除範圍：tests/、spikes/、scripts/ 與 README.md 不得改變值
        stack.enter_context(_scratch(TOOL_ROOT / "tests" / "_p2t1_scratch.txt", b"scratch"))
        stack.enter_context(_scratch(TOOL_ROOT / "spikes" / "_p2t1_scratch.txt", b"scratch"))
        stack.enter_context(_scratch(TOOL_ROOT / "scripts" / "_p2t1_scratch.txt", b"scratch"))
        stack.enter_context(_scratch(TOOL_ROOT / "README.md", b"# changed by P2-T1 test\n"))
        assert tool_tree_sha256() == first
    # 非空守衛（陽性對照）：src/ 內的行為檔變動必須改變值，否則上面的穩定性斷言可能空轉
    with _scratch(SRC / "uart_stdio_bridge" / "_p2t1_probe.py", b"PROBE = 1\n"):
        assert tool_tree_sha256() != first


def test_tree_sha256_algorithm(tmp_path):
    from uart_stdio_bridge.identity import tree_sha256

    (tmp_path / "a.txt").write_bytes(b"x")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "b.txt").write_bytes(b"y")
    entries = f"a.txt\0{hashlib.sha256(b'x').hexdigest()}\n" + f"d/b.txt\0{hashlib.sha256(b'y').hexdigest()}\n"
    expected = hashlib.sha256(entries.encode()).hexdigest()
    assert tree_sha256(tmp_path, include=lambda p: True) == expected


def test_tree_sha256_matches_p1_golden(tmp_path):
    from uart_stdio_bridge.identity import tree_sha256

    golden = REPO_ROOT / "contracts" / "golden" / "tree_sha256.ndjson"  # P1 T18 產出；不存在即紅燈
    lines = golden.read_text(encoding="utf-8").splitlines()
    # P1 golden 規格：至少 3 筆（單檔、巢狀目錄、含非 ASCII 檔名）
    assert len(lines) >= 3, "golden 向量不足 3 筆"
    cases = [json.loads(line) for line in lines]
    assert any("/" in rel for case in cases for rel in case["files"]), "golden 缺巢狀目錄向量"
    assert any(not rel.isascii() for case in cases for rel in case["files"]), "golden 缺非 ASCII 檔名向量"
    for i, case in enumerate(cases):
        root = tmp_path / f"case{i}"
        for rel, data in case["files"].items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data.encode("utf-8"))
        assert tree_sha256(root, include=lambda p: True) == case["tree_sha256"], f"golden 第 {i} 筆不符"


def test_package_tree_scope(tmp_path):
    from uart_stdio_bridge.identity import package_tree_sha256, repo_tree_sha256

    repo = tmp_path / "repo"

    def write(rel: str, data: bytes) -> None:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    write("sw_core/__init__.py", b"")
    write("sw_core/mod.py", b"print(1)\n")
    write("sw_core/assets/a.txt", b"A")
    write("serialwrapd.py", b"# shim\n")
    write("VERSION", b"0.4.0\n")
    write("docs/readme.md", b"doc\n")
    pkg0 = package_tree_sha256(repo)
    rep0 = repo_tree_sha256(repo)
    assert pkg0 != rep0
    # __pycache__ 與 *.pyc 排除（與 P1 同範圍）
    write("sw_core/__pycache__/mod.cpython-312.pyc", b"\0")
    assert package_tree_sha256(repo) == pkg0
    # 範圍外檔案：package 與 repo 皆不變
    write("docs/readme.md", b"doc changed\n")
    assert package_tree_sha256(repo) == pkg0
    assert repo_tree_sha256(repo) == rep0
    # repo 範圍但非 package 範圍：serialwrapd.py、VERSION
    write("serialwrapd.py", b"# changed\n")
    assert package_tree_sha256(repo) == pkg0
    rep1 = repo_tree_sha256(repo)
    assert rep1 != rep0
    write("VERSION", b"0.4.1\n")
    assert package_tree_sha256(repo) == pkg0
    assert repo_tree_sha256(repo) != rep1
    # package 範圍：sw_core/ 子樹內容變動必須改變 package 值
    write("sw_core/mod.py", b"print(2)\n")
    assert package_tree_sha256(repo) != pkg0
    # 真實 repo：兩值不同
    assert package_tree_sha256(REPO_ROOT) != repo_tree_sha256(REPO_ROOT)


def test_contract_revision_shape():
    from uart_stdio_bridge.identity import contract_revision

    rev, dirty = contract_revision(REPO_ROOT)
    assert rev is None or GIT_REV_RE.fullmatch(rev)
    assert isinstance(dirty, bool)


def _is_main_guard(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def test_main_module_guarded():
    pkg = SRC / "uart_stdio_bridge"
    targets = [pkg / "__main__.py"]
    dut_main = pkg / "dut" / "__main__.py"  # Task 4 才建立；存在時才檢查
    if dut_main.exists():
        targets.append(dut_main)
    for path in targets:
        assert path.is_file(), f"缺少 {path.relative_to(TOOL_ROOT)}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders: list[str] = []
        for stmt in tree.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(stmt, ast.If) and _is_main_guard(stmt.test):
                continue
            for node in ast.walk(stmt):
                if isinstance(node, ast.Call):
                    offenders.append(f"{path.relative_to(TOOL_ROOT)}:{node.lineno}")
        assert offenders == [], f"頂層呼叫未包在 __main__ 守衛內：{offenders}"
