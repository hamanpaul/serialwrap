"""L1.5 daemon artifact 的實際實作與候選身分判定。"""
from __future__ import annotations

import hashlib
import pathlib
import re


def daemon_implementation(path: pathlib.Path) -> str | None:
    """辨認 Rust ELF 與 Python 執行入口；未知格式一律不採信。"""
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            return None
        with resolved.open("rb") as stream:
            data = stream.read(262_144)
    except OSError:
        return None

    if data.startswith(b"\x7fELF"):
        return "rust"
    first_line = data.splitlines()[0].lower() if data else b""
    if b"python" in first_line:
        return "python"
    shell_entry = first_line.startswith(b"#!") and any(
        shell in first_line for shell in (b"/sh", b"bash", b"dash")
    )
    if shell_entry and re.search(
        rb"(?m)^\s*exec\s+python(?:\d+(?:\.\d+)?)?\s+[^\n]*serialwrapd\.py\b",
        data,
    ):
        return "python"
    return None


def l15_artifact_problem(
    implementation: str,
    artifact_path: pathlib.Path,
    *,
    candidate_path: pathlib.Path | None = None,
    candidate_sha256: str | None = None,
) -> str | None:
    """驗證 L1.5 列的標籤及 Rust 候選路徑／建置雜湊。"""
    actual = daemon_implementation(artifact_path)
    if actual != implementation:
        return f"L1.5 {implementation} 列的實際 artifact 身分為 {actual or 'unknown'}"
    if implementation != "rust":
        return None
    if candidate_path is None:
        return "L1.5 Rust 列缺少 --daemon-path 候選綁定"
    try:
        artifact = artifact_path.resolve(strict=True)
        candidate = candidate_path.resolve(strict=True)
    except OSError as exc:
        return f"L1.5 Rust artifact／--daemon-path 無法解析：{exc}"
    if artifact != candidate:
        return "L1.5 Rust artifact 與 --daemon-path 候選不同"
    if candidate_sha256:
        try:
            actual_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError as exc:
            return f"L1.5 Rust 候選 artifact 無法計算 SHA-256：{exc}"
        if actual_sha256 != candidate_sha256:
            return "L1.5 Rust 候選 artifact 與建置時 SHA-256 不符"
    return None
