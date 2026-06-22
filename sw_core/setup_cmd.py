"""sw_core.setup_cmd — serialwrap setup 的資產物化層。

提供 :func:`materialize_assets`，將套件內嵌資產複製到使用者可寫位置：
- profiles  → XDG config（不覆蓋使用者修改，除非 ``force=True``）
- agent skill → XDG data + ``~/.agents/skills/serialwrap`` 符號連結
- minicom wrappers → ``~/.local/bin``（設可執行權限）

.. note::
    此模組**僅負責物化**，不實作 reconciler 邏輯（Task 11）。
    目標目錄在**呼叫時**從環境變數解析，而非從模組匯入時快取的
    ``constants.CONFIG_DIR``，以確保測試可正確 monkeypatch。
"""

from __future__ import annotations

import importlib.resources
import os
import shutil
from pathlib import Path

from sw_core import assets as _assets

# ────────────────────────────── 私有工具 ──────────────────────────────


def _user_dirs(home: Path | str | None) -> dict[str, Path]:
    """根據目前環境變數解析使用者目錄（呼叫時解析，非匯入時快取）。

    Args:
        home: 使用者家目錄；``None`` 時自動展開 ``~``。

    Returns:
        含 ``config``、``data``、``agents_skill_link``、``bin`` 的路徑字典。
    """
    home_path = Path(home) if home else Path(os.path.expanduser("~"))
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(home_path / ".config")
    data_home = os.environ.get("XDG_DATA_HOME") or str(home_path / ".local" / "share")
    return {
        "config": Path(config_home) / "serialwrap",
        "data": Path(data_home) / "serialwrap",
        "agents_skill_link": home_path / ".agents" / "skills" / "serialwrap",
        "bin": home_path / ".local" / "bin",
    }


def _force_symlink(link: Path, target: Path) -> None:
    """建立或取代符號連結（冪等）。

    若 *link* 已存在（符號連結、一般檔案或真實目錄）則先移除再建立；
    真實目錄需用 rmtree（unlink 無法移除目錄，否則丟 IsADirectoryError）。

    Args:
        link:   符號連結路徑。
        target: 符號連結指向的目標路徑。
    """
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()
    elif link.is_dir():
        shutil.rmtree(link)
    elif link.exists():
        link.unlink()
    link.symlink_to(target)


def _copy_profile_file(src_item: importlib.resources.abc.Traversable, dest: Path, *, force: bool) -> None:
    """複製單一 profile 檔案，尊重 *force* 旗標。

    Args:
        src_item: importlib.resources Traversable 資源物件。
        dest:     目標路徑。
        force:    ``True`` 時強制覆蓋現有檔案。
    """
    if dest.exists() and not force:
        return
    with importlib.resources.as_file(src_item) as real:
        shutil.copy2(real, dest)


# ────────────────────────────── 公開 API ──────────────────────────────


def materialize_assets(
    home: Path | str | None = None,
    *,
    force: bool = False,
) -> dict[str, str]:
    """將套件內嵌資產物化到使用者可寫位置。

    目標目錄在呼叫時從環境變數（``XDG_CONFIG_HOME``、``XDG_DATA_HOME``）
    解析，確保測試 monkeypatch 可正確生效。

    Args:
        home:  使用者家目錄；``None`` 時自動展開 ``~``。
        force: ``True`` 時強制覆蓋現有 profiles（不影響 skill/wrappers，
               它們永遠更新）。

    Returns:
        小型摘要字典::

            {
                "profiles": str(profiles 目的地目錄),
                "skill_link": str(agent skill 符號連結路徑),
                "bin": str(bin 目的地目錄),
            }
    """
    dirs = _user_dirs(home)

    # ── 1. Profiles（不覆蓋使用者修改，除非 force）──────────────────────
    profiles_dest = dirs["config"] / "profiles"
    profiles_dest.mkdir(parents=True, exist_ok=True)

    src_profiles = importlib.resources.files("sw_core.assets") / "profiles"
    for item in src_profiles.iterdir():
        if item.is_file():
            _copy_profile_file(item, profiles_dest / item.name, force=force)

    # ── 2. Agent skill（永遠刷新；建立/取代符號連結）────────────────────
    skill_dest = dirs["data"] / "skill"
    _assets.copy_tree("skill", skill_dest)
    _force_symlink(dirs["agents_skill_link"], skill_dest)

    # ── 3. Minicom wrappers → ~/.local/bin（設可執行權限）───────────────
    bin_dest = dirs["bin"]
    bin_dest.mkdir(parents=True, exist_ok=True)

    _wrapper_map = {
        "minicom_router.sh": "serialwrap-minicom",
        "minicom-broker.sh": "serialwrap-minicom-broker",
        "minicom-raw.sh":    "serialwrap-minicom-raw",
    }
    for src_name, dest_name in _wrapper_map.items():
        src_item = importlib.resources.files("sw_core.assets") / "tools" / src_name
        dest_file = bin_dest / dest_name
        with importlib.resources.as_file(src_item) as real:
            shutil.copy2(real, dest_file)
        os.chmod(dest_file, 0o755)

    return {
        "profiles": str(profiles_dest),
        "skill_link": str(dirs["agents_skill_link"]),
        "bin": str(bin_dest),
    }
