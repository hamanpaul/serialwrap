"""Windows setup 靜態資產的使用者家目錄快照。"""
from __future__ import annotations

import os
import stat


def snapshot_windows_user_home(profile: str) -> dict[str, object]:
    """快照 Windows 使用者家目錄中 setup 可能寫入的 serialwrap 靜態資產。"""
    if not profile or not os.path.isabs(profile) or not os.path.isdir(profile):
        raise RuntimeError("Windows USERPROFILE 不存在或不是絕對路徑，無法保護真實家目錄")

    def _metadata(path: str):
        info = os.lstat(path)
        return (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _is_reparse_point(info) -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & reparse_flag
        )

    def _snapshot_path(root: str):
        try:
            root_info = os.lstat(root)
        except FileNotFoundError:
            return None
        entries = [(".", _metadata(root))]
        if stat.S_ISDIR(root_info.st_mode) and not _is_reparse_point(root_info):
            def _raise_walk_error(error):
                raise error

            for current, directories, files in os.walk(
                root, topdown=True, followlinks=False, onerror=_raise_walk_error
            ):
                directories.sort()
                files.sort()
                for name in directories + files:
                    path = os.path.join(current, name)
                    info = os.lstat(path)
                    entries.append((os.path.relpath(path, root), _metadata(path)))
                    if name in directories and _is_reparse_point(info):
                        directories.remove(name)
        return tuple(entries)

    roots = {
        ".agents/skills/serialwrap": os.path.join(profile, ".agents", "skills", "serialwrap"),
        ".local/share/serialwrap": os.path.join(profile, ".local", "share", "serialwrap"),
    }
    snapshot = {label: _snapshot_path(path) for label, path in roots.items()}

    bin_dir = os.path.join(profile, ".local", "bin")
    try:
        serialwrap_names = sorted(name for name in os.listdir(bin_dir) if name.startswith("serialwrap"))
    except FileNotFoundError:
        serialwrap_names = []
    snapshot[".local/bin/serialwrap*"] = tuple(
        (name, _snapshot_path(os.path.join(bin_dir, name))) for name in serialwrap_names
    )
    return snapshot
