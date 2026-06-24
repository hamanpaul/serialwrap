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

import configparser
import getpass
import importlib.resources
import os
import shutil
from pathlib import Path

from sw_core import assets as _assets
from sw_core.runtime_config import RuntimeConfig
from sw_core.state_migrate import migrate_legacy_state
from sw_core.systemd_units import render_system_unit, render_user_unit

# ────────────────────────────── 私有工具 ──────────────────────────────


def _user_dirs(home: Path | str | None) -> dict[str, Path]:
    """根據目前環境變數解析使用者目錄（呼叫時解析，非匯入時快取）。

    Args:
        home: 使用者家目錄；``None`` 時自動展開 ``~``。

    Returns:
        含 ``config``、``data``、``agents_skill_link``、``bin`` 的路徑字典。
    """
    home_path = Path(home) if home else Path(os.path.expanduser("~"))
    # config/data 解析須與 constants.CONFIG_DIR/DATA_DIR 同優先序（SERIALWRAP_* 覆寫 > XDG > ~/…），
    # 否則只設 SERIALWRAP_CONFIG_DIR 時 setup 物化的 profiles 會與 daemon 讀的 PROFILE_DIR
    # （= CONFIG_DIR/profiles）分歧、daemon 讀不到 profiles（最終整合審查 I-B）。
    cfg_override = os.environ.get("SERIALWRAP_CONFIG_DIR")
    if cfg_override:
        config = Path(cfg_override)
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME") or str(home_path / ".config")
        config = Path(config_home) / "serialwrap"
    data_override = os.environ.get("SERIALWRAP_DATA_DIR")
    if data_override:
        data = Path(data_override)
    else:
        data_home = os.environ.get("XDG_DATA_HOME") or str(home_path / ".local" / "share")
        data = Path(data_home) / "serialwrap"
    return {
        "config": config,
        "data": data,
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


def detect_legacy_install(home: Path | str | None = None) -> dict | None:
    """偵測舊版 ``~/.paul_tools`` 安裝並回傳退役指引（不刪除任何東西）。

    舊版安裝特徵為 ``~/.paul_tools/serialwrap`` shadow 二進位存在。偵測到時
    一併回報 minicom 符號連結與 ``/tmp/serialwrap/state.json`` 是否殘留，並給
    出退役步驟提示，供 ``serialwrap setup`` 顯示。此函式**僅偵測 + 指引**，
    絕不自動刪除——交由使用者確認後手動清理。

    Args:
        home: 使用者家目錄；``None`` 時自動展開 ``~``。

    Returns:
        無 legacy 安裝時回 ``None``；否則回字典::

            {
                "path": str(~/.paul_tools),
                "serialwrap": bool,        # shadow 二進位是否存在
                "minicom_symlink": bool,   # minicom 符號連結是否存在
                "state_json": str | None,  # /tmp/serialwrap/state.json（存在才有）
                "hint": str,               # 退役步驟指引
            }
    """
    home_path = Path(home) if home else Path(os.path.expanduser("~"))
    paul_tools = home_path / ".paul_tools"
    serialwrap_bin = paul_tools / "serialwrap"

    # 偵測判準：~/.paul_tools/serialwrap 存在才算 legacy 安裝。
    if not serialwrap_bin.exists():
        return None

    # minicom 符號連結（舊版常見於 ~/.paul_tools/minicom 或 ~/.local/bin/minicom）。
    minicom_candidates = [
        paul_tools / "minicom",
        home_path / ".local" / "bin" / "minicom",
    ]
    minicom_symlink = any(p.is_symlink() for p in minicom_candidates)

    # 舊版 state.json 殘留於 /tmp（新版改走 XDG state home）。
    legacy_state = Path("/tmp/serialwrap/state.json")
    state_json = str(legacy_state) if legacy_state.exists() else None

    hint = (
        "偵測到舊版 ~/.paul_tools 安裝。建議退役步驟："
        "1) 停掉舊 daemon（若仍在跑）；"
        "2) 移除 shadow 二進位 ~/.paul_tools/serialwrap 與 minicom 符號連結；"
        "3) 從 shell rc 移除 ~/.paul_tools 的 PATH export。"
        "新版改以 pipx 安裝（~/.local/bin）並以 XDG 路徑運作，不需 ~/.paul_tools。"
        "本指令不會自動刪除任何檔案。"
    )

    return {
        "path": str(paul_tools),
        "serialwrap": serialwrap_bin.exists(),
        "minicom_symlink": minicom_symlink,
        "state_json": state_json,
        "hint": hint,
    }


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


# ═══════════════════════════ 監管模式 reconciler（Task 11）═══════════════════════════
#
# 核心不變式：模式 *改變* 時，必須「先停舊、再起新」。
# 這是為了避免 on-demand ↔ systemd 交接過程中，兩個程序同時開啟同一支
# /dev/ttyUSB*（two-reader）。SingletonLock 不足以擋住跨機制的競態——舊機制的
# daemon 必須先釋放 tty FD，新機制才可啟動。此順序是本任務的核心，務必正確。
#
# 另一護欄：flash 進行中（any_flashing）除非 force，否則拒絕轉換，避免在 MCU
# 韌體燒錄／檔案傳輸中途切斷。
#
# sudo 邊界：system scope 的特權動作（寫 /etc/systemd/system、sudo systemctl）
# 在未帶 with_sudo 時**絕不**靜默執行，改記入 result 的 pending_sudo 供呼叫端決定。


class FlashingBusy(Exception):
    """flash 進行中且未帶 force 時拋出，表示不可在此刻切換監管模式。"""


# systemd user/system scope 的 unit 安裝路徑、固定 socket/profile 與 ExecStart。
_USER_UNIT_REL = Path(".config") / "systemd" / "user" / "serialwrap.service"
_USER_EXEC_START = "%h/.local/bin/serialwrapd"
_SYSTEM_UNIT_PATH = "/etc/systemd/system/serialwrap.service"
# system scope 為跨使用者共用，路徑固定於系統位置（不隨呼叫者 XDG 變動）：
# socket 於 RuntimeDirectory、profiles 於 ConfigurationDirectory。供 cli 寫 config 的有效 socket
# 與 daemon --profile-dir 一致對齊（Codex #1a/#1b）。
SYSTEM_SOCKET = "/run/serialwrap/serialwrapd.sock"
SYSTEM_PROFILE_DIR = "/etc/serialwrap/profiles"


def _resolve_run_user() -> str:
    """解析 system unit 的執行身份（``User=``）。

    pipx 使用者安裝下 serialwrapd binary 落在安裝者 venv（家目錄內），dedicated
    ``serialwrap`` service account 讀不到該家目錄，故 system unit 改以「安裝者本人」
    執行。優先取 ``SUDO_USER``（``sudo serialwrap setup`` 時為原始使用者），否則取
    目前登入帳號。
    """
    return os.environ.get("SUDO_USER") or getpass.getuser()


def _resolve_serialwrapd_path(home: Path) -> str:
    """解析 system unit ExecStart 用的 serialwrapd 絕對路徑（pipx 使用者安裝）。

    ExecStart 不能用 ``%h``（system unit 無使用者 home 展開），故解析成絕對路徑：
    優先 ``which serialwrapd``（pipx shim，通常 ``~/.local/bin/serialwrapd``），
    否則退回 ``home/.local/bin/serialwrapd``。
    """
    return shutil.which("serialwrapd") or str(home / ".local" / "bin" / "serialwrapd")


def _system_exec_start(home: Path) -> str:
    """組出 system unit 的 ExecStart（絕對 serialwrapd + 固定 socket/profile-dir）。"""
    return f"{_resolve_serialwrapd_path(home)} --socket {SYSTEM_SOCKET} --profile-dir {SYSTEM_PROFILE_DIR}"


def _merge_wsl_conf_systemd(existing: str) -> str:
    """把現有 /etc/wsl.conf 內容合併出含 ``[boot] systemd=true`` 的版本（保留其他段落/鍵）。"""
    import io

    cp = configparser.ConfigParser()
    cp.optionxform = str  # 保留鍵大小寫（wsl.conf 鍵大小寫敏感）
    try:
        cp.read_string(existing)
    except configparser.Error:
        # 既有檔不合法時不沿用，改寫成只含 [boot] 的乾淨版本（不破壞性丟棄無法解析內容）。
        cp = configparser.ConfigParser()
        cp.optionxform = str
    if not cp.has_section("boot"):
        cp.add_section("boot")
    cp.set("boot", "systemd", "true")
    buf = io.StringIO()
    cp.write(buf)
    return buf.getvalue()


def ensure_wsl_systemd(fx, home) -> dict:
    """WSL 上若 systemd 尚未啟用，寫 ``/etc/wsl.conf`` ``[boot] systemd=true``（需 sudo）。

    - 非 WSL → no-op（``{"wsl": False}``）。
    - 已啟用 systemd（``has_systemd``）→ no-op（``already=True``）。
    - 需啟用 → staging + ``sudo install`` 寫 ``/etc/wsl.conf``，回報需於 Windows 端
      ``wsl --shutdown`` 重啟（systemd 須重進 WSL 才生效，當次 setup 無法直接起 systemd 服務）。

    以 Effects.run 經過 sudo install（非直接 sudo tee，避免空檔）。回傳結果字典供
    cli 決定是否早退並提示使用者重啟。
    """
    if not fx.is_wsl():
        return {"wsl": False, "already": False, "enabled_now": False, "needs_restart": False}
    if fx.has_systemd():
        return {"wsl": True, "already": True, "enabled_now": False, "needs_restart": False}
    home = Path(home)
    try:
        existing = Path("/etc/wsl.conf").read_text(encoding="utf-8")
    except OSError:
        existing = ""
    merged = _merge_wsl_conf_systemd(existing)
    staging = home / ".local" / "share" / "serialwrap" / "wsl.conf"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(merged, encoding="utf-8")
    rc, _out, _err = fx.run(["sudo", "install", "-m", "0644", str(staging), "/etc/wsl.conf"])
    return {
        "wsl": True,
        "already": False,
        "enabled_now": rc == 0,
        "needs_restart": rc == 0,
        "rc": rc,
        "hint": (
            "已寫入 /etc/wsl.conf [boot] systemd=true；請在 Windows 端執行 "
            "`wsl --shutdown`，重新進入 WSL 後再跑一次 `serialwrap setup --system --with-sudo`。"
        ),
    }


def _stage_system_unit(home: Path) -> Path:
    """把 system unit 真實內容寫到非特權 staging 檔，回傳路徑。

    以 staging + ``sudo install`` 取代直接 ``sudo tee``：Effects.run 不帶 stdin，直接
    ``sudo tee`` 會寫出空檔；staging 讓非特權步驟先備妥真實內容，特權步驟只做複製。
    unit 以安裝者本人帳號執行（run-as-user），ExecStart 指向其 pipx serialwrapd。
    """
    staging = home / ".local" / "share" / "serialwrap" / "serialwrap.service"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(
        render_system_unit(_system_exec_start(home), run_user=_resolve_run_user()),
        encoding="utf-8",
    )
    return staging


def _stage_system_profiles(home: Path) -> Path:
    """把套件內 profiles 物化到非特權 staging 目錄，供 sudo 複製到 /etc/serialwrap/profiles。"""
    staging = home / ".local" / "share" / "serialwrap" / "system-profiles"
    _assets.copy_tree("profiles", staging)
    return staging


def _system_install_cmds(home: Path, *, include_start: bool) -> list[list[str]]:
    """組出 system scope 安裝/啟用 unit + profiles 的特權指令（內容已先 stage 成真實檔）。

    含把 profiles 安裝到 ``/etc/serialwrap/profiles``——否則 system daemon 的
    ``--profile-dir`` 指向系統路徑卻沒有內容、開機讀不到 profiles（Codex #1b）。
    """
    unit_staging = _stage_system_unit(home)
    prof_staging = _stage_system_profiles(home)
    cmds = [
        ["sudo", "install", "-m", "0644", str(unit_staging), _SYSTEM_UNIT_PATH],
        ["sudo", "mkdir", "-p", SYSTEM_PROFILE_DIR],
        ["sudo", "cp", "-r", f"{prof_staging}/.", SYSTEM_PROFILE_DIR],
        ["sudo", "systemctl", "daemon-reload"],
        ["sudo", "systemctl", "enable", "serialwrap"],
    ]
    if include_start:
        cmds.append(["sudo", "systemctl", "start", "serialwrap"])
    return cmds


def _state_path_for(mode: str, home: Path) -> Path:
    """回傳指定 scope 的 state.json 路徑（用於跨 scope 遷移判斷）。

    user scope（``on-demand`` / ``systemd-user``）落在 XDG state home；
    system scope（``systemd-system``）對應 systemd ``StateDirectory=serialwrap``
    的 ``/var/lib/serialwrap``。同一 scope 內路徑相同 → 遷移為 no-op。

    Args:
        mode: 監管模式字串。
        home: 使用者家目錄。

    Returns:
        該 scope 對應的 ``state.json`` 路徑。
    """
    if mode == "systemd-system":
        return Path("/var/lib/serialwrap/state.json")
    state_home = os.environ.get("XDG_STATE_HOME") or str(home / ".local" / "state")
    return Path(state_home) / "serialwrap" / "state.json"


def _stop_old(
    *,
    old_mode: str,
    fx,
    daemon_running: bool,
    with_sudo: bool,
    pending_sudo: list[list[str]],
) -> None:
    """停掉舊機制，讓它釋放 tty FD（轉換的第一步，務必先於起新）。

    - ``on-demand``：只有 daemon 真的在跑才需停（讓正在跑的 on-demand daemon
      釋放 tty）。
    - ``systemd-user``：``systemctl --user stop`` 後 ``disable``。
    - ``systemd-system``：特權動作；帶 ``with_sudo`` 才實跑，否則記入
      *pending_sudo*（絕不靜默跑 sudo）。

    Args:
        old_mode:       目前監管模式。
        fx:             effects 介面（所有外部指令經此，供測試攔截）。
        daemon_running: on-demand daemon 是否在跑。
        with_sudo:      是否允許執行特權 sudo 指令。
        pending_sudo:   累積未授權特權指令的清單（原地修改）。
    """
    if old_mode == "on-demand":
        if daemon_running:
            fx.run(["serialwrap", "daemon", "stop"])
    elif old_mode == "systemd-user":
        fx.run(["systemctl", "--user", "stop", "serialwrap"])
        fx.run(["systemctl", "--user", "disable", "serialwrap"])
    elif old_mode == "systemd-system":
        if with_sudo:
            fx.run(["sudo", "systemctl", "stop", "serialwrap"])
            fx.run(["sudo", "systemctl", "disable", "serialwrap"])
        else:
            pending_sudo.append(["sudo", "systemctl", "stop", "serialwrap"])
            pending_sudo.append(["sudo", "systemctl", "disable", "serialwrap"])


def _start_new(
    *,
    target_mode: str,
    fx,
    home: Path,
    with_sudo: bool,
    pending_sudo: list[list[str]],
) -> None:
    """啟動新機制（轉換的最後一步，必在停舊之後）。

    - ``systemd-user``：寫 user unit → ``daemon-reload`` / ``enable`` / ``start``
      / ``loginctl enable-linger``。
    - ``systemd-system``：特權動作；帶 ``with_sudo`` 才寫 unit（經
      ``sudo tee``）並 ``sudo systemctl daemon-reload``/``enable``/``start``，
      否則把確切的 sudo 指令記入 *pending_sudo*，不執行。
    - ``on-demand``：無需啟動（daemon 依需求自生），直接略過。

    Args:
        target_mode:  目標監管模式。
        fx:           effects 介面（外部指令經此）。
        home:         使用者家目錄（user unit 寫此目錄下，可在 tmp 測試）。
        with_sudo:    是否允許執行特權 sudo 指令。
        pending_sudo: 累積未授權特權指令的清單（原地修改）。
    """
    if target_mode == "systemd-user":
        unit_path = home / _USER_UNIT_REL
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(render_user_unit(_USER_EXEC_START), encoding="utf-8")
        fx.run(["systemctl", "--user", "daemon-reload"])
        fx.run(["systemctl", "--user", "enable", "serialwrap"])
        fx.run(["systemctl", "--user", "start", "serialwrap"])
        # loginctl enable-linger 須帶使用者名稱：多數 systemd 版本不接受省略/空字串，否則 linger
        # 不生效、服務無法開機自啟（Copilot PR review）。取不到使用者名時略過、不送空字串。
        _linger_user = os.environ.get("USER") or os.environ.get("LOGNAME")
        if _linger_user:
            fx.run(["loginctl", "enable-linger", _linger_user])
    elif target_mode == "systemd-system":
        # 特權：unit 內容先 stage 成真實檔，再以 sudo install 複製到 /etc（避免空檔的 sudo tee）。
        # 未授權路徑已於 reconcile 上游早退，這裡 with_sudo 必為真；保留 else 為防禦。
        cmds = _system_install_cmds(home, include_start=True)
        if with_sudo:
            for c in cmds:
                fx.run(c)
        else:
            pending_sudo.extend(cmds)
    # target_mode == "on-demand"：無需啟動，daemon 依需求自生。


def reconcile(
    *,
    old_mode,
    target_mode,
    fx,
    home,
    daemon_running: bool = False,
    any_flashing: bool = False,
    with_sudo: bool = False,
    force: bool = False,
    socket_path=None,
    config_path=None,
) -> dict:
    """決定並套用監管模式；模式改變時以「先停舊、再起新」嚴格順序轉換。

    順序的存在是為了避免 on-demand ↔ systemd 交接時兩個程序同時開啟同一支
    /dev/ttyUSB*。flash 進行中除非 *force* 否則拒絕（不動任何東西）。system
    scope 特權動作未帶 *with_sudo* 時記入 pending_sudo，不靜默執行。

    Args:
        old_mode:       目前監管模式（``on-demand`` / ``systemd-user`` /
                        ``systemd-system``）。
        target_mode:    目標監管模式。
        fx:             effects 介面，所有外部指令（systemctl/loginctl/daemon
                        stop）經此以便單元測試攔截。
        home:           使用者家目錄；unit 檔寫於此目錄下（可在 tmp 測試）。
        daemon_running: on-demand daemon 是否在跑（決定是否需先停舊）。
        any_flashing:   是否有 flash／傳輸進行中（護欄）。
        with_sudo:      是否允許執行特權 sudo 指令。
        force:          ``True`` 時跳過 flash 護欄。
        socket_path:    寫入 config 的有效 socket 路徑（可為 ``None``）。
        config_path:    config.yaml 路徑；``None`` 時用
                        ``home/.config/serialwrap/config.yaml``。

    Returns:
        摘要字典::

            {
                "mode": target_mode,
                "transitioned": old_mode != target_mode,
                "ran": [已實際執行的指令字串清單],
                "pending_sudo": [未授權待執行的特權指令清單],
            }

    Raises:
        FlashingBusy: ``any_flashing`` 為真且未帶 ``force``。
    """
    home = Path(home)
    pending_sudo: list[list[str]] = []

    # ── 護欄 1：flash 進行中除非 force，否則拒絕（不動任何東西）────────────
    if any_flashing and not force:
        raise FlashingBusy("flash 進行中，拒絕切換監管模式（可用 force 覆寫）")

    cfg = RuntimeConfig(config_path or (home / ".config" / "serialwrap" / "config.yaml"))

    # ── 護欄 2：涉及 system scope 的特權操作（裝/起新 system unit、或停舊 system service）都需 root；
    #    未帶 with_sudo → 蒐集 pending 並早退，且「不停舊、不起新、不寫 config」。這同時擋掉兩種半套：
    #    (a) config 說 systemd-system 但 unit 沒裝/daemon 沒跑（I-1）；
    #    (b) 從 systemd-system 轉出時，舊 system daemon 未被停掉卻又起新 daemon → /dev/ttyUSB*
    #        two-reader（Codex 對抗式審查 CRITICAL #4）。使用者改用 --with-sudo 或手動跑 pending。
    _system_target = target_mode == "systemd-system"
    _system_old_transition = old_mode == "systemd-system" and old_mode != target_mode
    if (_system_target or _system_old_transition) and not with_sudo:
        pending: list[list[str]] = []
        if _system_old_transition:
            pending.append(["sudo", "systemctl", "stop", "serialwrap"])
            pending.append(["sudo", "systemctl", "disable", "serialwrap"])
        if _system_target:
            pending.extend(_system_install_cmds(home, include_start=(old_mode != target_mode)))
        return {
            "mode": old_mode,            # 實際生效模式未變（config 不寫）
            "requested_mode": target_mode,
            "transitioned": False,
            "applied": False,
            "ran": [],
            "pending_sudo": pending,
        }

    # ── 情境 A：同模式 → 冪等刷新（不 stop、不啟新、不打斷正在跑的 daemon）──
    if target_mode == old_mode:
        if target_mode == "systemd-user":
            # 只重寫 unit + reload + enable，不 start churn。
            unit_path = home / _USER_UNIT_REL
            unit_path.parent.mkdir(parents=True, exist_ok=True)
            unit_path.write_text(render_user_unit(_USER_EXEC_START), encoding="utf-8")
            fx.run(["systemctl", "--user", "daemon-reload"])
            fx.run(["systemctl", "--user", "enable", "serialwrap"])
        elif target_mode == "systemd-system":
            # 同模式刷新：不含 start（已在跑）。未授權路徑已於上游早退。
            cmds = _system_install_cmds(home, include_start=False)
            if with_sudo:
                for c in cmds:
                    fx.run(c)
            else:
                pending_sudo.extend(cmds)
        # on-demand 同模式：無 unit 可刷新，僅寫 config。
        cfg.set_mode(target_mode, socket_path=socket_path)
        return {
            "mode": target_mode,
            "transitioned": False,
            "applied": True,
            "ran": [list(c) for c in getattr(fx, "calls", [])],
            "pending_sudo": pending_sudo,
        }

    # ── 情境 B：模式改變 → 嚴格順序轉換 ────────────────────────────────────
    # a. 先停舊（釋放 tty FD）——務必先於起新，避免 two-reader。
    _stop_old(
        old_mode=old_mode,
        fx=fx,
        daemon_running=daemon_running,
        with_sudo=with_sudo,
        pending_sudo=pending_sudo,
    )

    # b. 跨 scope 才需遷移 state（best-effort、僅 dest 空才搬、絕不拋例外）。
    legacy_state = _state_path_for(old_mode, home)
    new_state = _state_path_for(target_mode, home)
    if legacy_state != new_state:
        try:
            migrate_legacy_state(legacy_state, new_state)
        except Exception:
            # 遷移屬 best-effort，失敗不可中斷模式轉換。
            pass

    # c. 再起新（必在停舊之後）。
    _start_new(
        target_mode=target_mode,
        fx=fx,
        home=home,
        with_sudo=with_sudo,
        pending_sudo=pending_sudo,
    )

    # d. 寫 config（單一事實來源）。此處必為已實際套用（system-no-sudo 已於上游早退）。
    cfg.set_mode(target_mode, socket_path=socket_path)

    return {
        "mode": target_mode,
        "transitioned": True,
        "applied": True,
        "ran": [list(c) for c in getattr(fx, "calls", [])],
        "pending_sudo": pending_sudo,
    }
