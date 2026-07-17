from __future__ import annotations
from importlib import resources

def _names(subdir: str) -> list[str]:
    root = resources.files(__package__) / subdir
    return sorted(p.name for p in root.iterdir() if p.is_file())

def list_profile_files() -> list[str]:
    return _names("profiles")

def list_tool_files() -> list[str]:
    return _names("tools")

def read_text(relpath: str) -> str:
    """讀取套件內資產檔的文字內容（如 ``"skill/SKILL_WINDOWS.md"``，#131）。

    以 importlib.resources 取檔，pipx 安裝與 PyInstaller onefile（datas 內嵌）皆可用。
    """
    node = resources.files(__package__)
    for part in relpath.split("/"):
        node = node / part
    return node.read_text(encoding="utf-8")


def copy_tree(subdir: str, dest) -> None:
    """把套件內某子目錄遞迴複製到 dest（materialize 用）。"""
    import shutil, pathlib
    src = resources.files(__package__) / subdir
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dest / item.name
        with resources.as_file(item) as real:
            if real.is_dir():
                shutil.copytree(real, target, dirs_exist_ok=True)
            else:
                shutil.copy2(real, target)
