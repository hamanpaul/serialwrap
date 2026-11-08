"""檔案與程式碼樹 SHA-256 摘要計算演算法與版本身分（P2-T1）。

演算法依據：
- docs/rust-refactor/plans/rust-architecture.md §10.1
- docs/rust-refactor/serialwrap-rust-spec-final.md §9.5
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess
from typing import Callable

_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


def file_sha256(path: pathlib.Path | str) -> str:
    """計算指定檔案的 SHA-256 摘要（小寫 64 位十六進位字串）。"""
    p = pathlib.Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def tree_sha256(root: pathlib.Path | str, *, include: Callable[[pathlib.Path], bool]) -> str:
    """依 rust-architecture.md §10.1 演算法計算目錄樹的 SHA-256 摘要。

    演算法：
    1. 遞迴收集符合 include(p) 且為一般檔案之項目。
    2. 將相對於 root 的 POSIX 分隔路徑依 UTF-8 位元組序排序。
    3. 逐檔串接 f"{relpath}\\0{file_sha256}\\n"，最後計算整段字串（UTF-8 編碼）的 SHA-256。
    """
    root_path = pathlib.Path(root).resolve()
    entries: list[tuple[str, str]] = []

    for p in root_path.rglob("*"):
        if not p.is_file():
            continue
        if not include(p):
            continue
        rel = p.relative_to(root_path).as_posix()
        entries.append((rel, file_sha256(p)))

    entries.sort(key=lambda kv: kv[0].encode("utf-8"))
    payload = "".join(f"{rel}\0{fhash}\n" for rel, fhash in entries).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


_EMPTY_TREE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def tool_tree_sha256() -> str:
    """計算 uart_stdio_bridge 工具影響行為之檔案樹摘要。

    root 為 tools/uart_stdio_bridge。
    只涵蓋：
    - src/**（排除 __pycache__/ 與 *.pyc 以及 *.egg-info）
    - pyproject.toml
    - docker/Dockerfile.dut
    其餘 README、scripts/、spikes/、tests/、APPROVED_REVISION.json 均不計入。
    """
    tool_root = pathlib.Path(__file__).resolve().parents[2]
    if not (tool_root / "pyproject.toml").is_file() or not (tool_root / "src" / "uart_stdio_bridge" / "__init__.py").is_file():
        raise RuntimeError("HARNESS_INSUFFICIENT_EVIDENCE: tool root missing required files")

    def _include(p: pathlib.Path) -> bool:
        try:
            rel = p.relative_to(tool_root).as_posix()
        except ValueError:
            return False
        if any(part.endswith(".egg-info") for part in p.parts):
            return False
        if rel.startswith("src/"):
            if "__pycache__" in p.parts or p.name.endswith(".pyc"):
                return False
            return True
        if rel in {"pyproject.toml", "docker/Dockerfile.dut"}:
            return True
        return False

    digest = tree_sha256(tool_root, include=_include)
    if digest == _EMPTY_TREE_SHA256:
        raise RuntimeError("HARNESS_INSUFFICIENT_EVIDENCE: tool tree is empty")
    return digest


def package_tree_sha256(repo_root: pathlib.Path | str) -> str:
    """計算 sw_core 套件子樹之 tree_sha256 摘要（獨立實作，不 import 產品）。

    範圍與 P1 T18 sw_core.release_manifest.package_tree_sha256() 相同：
    sw_core/ 子樹、relpath 以 "sw_core/" 開頭（排除 __pycache__/ 與 *.pyc）。
    """
    repo_path = pathlib.Path(repo_root).resolve()
    if not (repo_path / "sw_core").is_dir():
        raise RuntimeError("HARNESS_INSUFFICIENT_EVIDENCE: repo missing sw_core directory")

    def _include(p: pathlib.Path) -> bool:
        try:
            rel = p.relative_to(repo_path).as_posix()
        except ValueError:
            return False
        if not rel.startswith("sw_core/"):
            return False
        if "__pycache__" in p.parts or p.name.endswith(".pyc"):
            return False
        return True

    digest = tree_sha256(repo_path, include=_include)
    if digest == _EMPTY_TREE_SHA256:
        raise RuntimeError("HARNESS_INSUFFICIENT_EVIDENCE: package tree is empty")
    return digest


def repo_tree_sha256(repo_root: pathlib.Path | str) -> str:
    """計算 serialwrap 部署程式碼較大範圍之 tree_sha256 摘要。

    範圍：
    - sw_core/**/*.py（排除 __pycache__/ 與 *.pyc）
    - sw_core/assets/**
    - serialwrapd.py
    - serialwrap
    - serialwrap.py
    - VERSION
    """
    repo_path = pathlib.Path(repo_root).resolve()
    if not (repo_path / "sw_core").is_dir():
        raise RuntimeError("HARNESS_INSUFFICIENT_EVIDENCE: repo missing sw_core directory")

    def _include(p: pathlib.Path) -> bool:
        try:
            rel = p.relative_to(repo_path).as_posix()
        except ValueError:
            return False
        if rel in {"serialwrapd.py", "serialwrap", "serialwrap.py", "VERSION"}:
            return True
        if rel.startswith("sw_core/"):
            if "__pycache__" in p.parts or p.name.endswith(".pyc"):
                return False
            return rel.startswith("sw_core/assets/") or p.name.endswith(".py")
        return False

    digest = tree_sha256(repo_path, include=_include)
    if digest == _EMPTY_TREE_SHA256:
        raise RuntimeError("HARNESS_INSUFFICIENT_EVIDENCE: repo tree is empty")
    return digest


def contract_revision(repo_root: pathlib.Path | str) -> tuple[str | None, bool]:
    """取得 contracts/ 目錄的 Git revision 與 dirty 狀態。

    回傳：
        (40 位小寫十六進位 revision 或 None, contracts/ 是否有未 commit 變更)。
        若 git 不可用或執行失敗，回傳 (None, True)。
    """
    repo_path = pathlib.Path(repo_root).resolve()
    try:
        r_rev = subprocess.run(
            ["git", "rev-parse", "HEAD:contracts"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(repo_path),
            timeout=10,
        )
        if r_rev.returncode != 0:
            return (None, True)
        rev = r_rev.stdout.strip()
        if not _HEX40_RE.fullmatch(rev):
            return (None, True)

        r_status = subprocess.run(
            ["git", "--no-optional-locks", "status", "--porcelain", "--", "contracts"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(repo_path),
            timeout=10,
        )
        if r_status.returncode != 0:
            return (rev, True)
        dirty = bool(r_status.stdout.strip())
        return (rev, dirty)
    except Exception:
        return (None, True)
