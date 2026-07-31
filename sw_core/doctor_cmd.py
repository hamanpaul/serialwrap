"""sw_core.doctor_cmd — serialwrap 環境診斷（doctor）。

提供 :func:`run_doctor`，對安裝／執行環境做一系列**唯讀**檢查並回報結果。
每項檢查永不拋例外（探測失敗一律視為不 ok 並給修復提示），讓 `serialwrap
doctor` 在任何破損環境下都能順利印出報告而非崩潰。

回傳清單中每個項目的結構::

    {"check": str, "ok": bool, "detail": str, "fix": str}

``fix`` 在 ``ok`` 為真時為空字串。

依賴僅限 stdlib + :mod:`sw_core.sysenv` + :mod:`sw_core.constants` +
:mod:`sw_core.runtime_config`，不觸碰 daemon／socket。
"""

from __future__ import annotations

import os
import sys

from sw_core.constants import DEVICE_BY_ID_DIR
from sw_core.sysenv import SystemEffects


def _check_python() -> dict:
    """Python 版本是否 ≥ 3.10。"""
    ok = sys.version_info >= (3, 10)
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return {
        "check": "python",
        "ok": ok,
        "detail": version,
        "fix": "" if ok else "需 Python ≥ 3.10",
    }


def _check_pyyaml() -> dict:
    """PyYAML 是否可匯入（runtime_config／profiles 需要）。"""
    try:
        import yaml  # noqa: F401
        ok = True
        detail = getattr(yaml, "__version__", "已安裝")
    except Exception:  # pragma: no cover - 環境相依
        ok = False
        detail = "未安裝"
    return {
        "check": "pyyaml",
        "ok": ok,
        "detail": detail,
        "fix": "" if ok else "安裝 PyYAML（pipx 安裝會自動帶入）",
    }


def _check_on_path(
    fx,
    name: str,
    fix_hint: str = "確認 ~/.local/bin 在 PATH（pipx ensurepath）",
    *,
    check_name: str | None = None,
) -> dict:
    """指定二進位是否在 PATH 上。``fix_hint`` 預設值即原 Linux 字串（輸出不變）。

    ``check_name``（keyword-only，#149）：覆寫回報鍵，供含連字號的二進位名稱
    （如 ``serialwrap-minicom``）產生底線命名的 ``serialwrap_minicom_on_path``，
    與既有 ``serialwrap_on_path``／``serialwrapd_on_path`` 命名一致；預設
    ``None`` 時沿用 ``f"{name}_on_path"``，既有呼叫端行為逐字不變。
    """
    path = fx.which(name)
    ok = path is not None
    return {
        "check": check_name or f"{name}_on_path",
        "ok": ok,
        "detail": path or "找不到",
        "fix": "" if ok else fix_hint,
    }


def _check_other_serialwrap_installs(fx) -> dict:
    """同機 PATH 上是否有多份不同版本的 serialwrap 安裝（#154）。

    純診斷資訊：只看得見「呼叫 doctor 這個行程的 PATH」上找得到的安裝——以絕對
    路徑呼叫、venv bin/ 不在 PATH 上的情境（如 TestPilot）看不到，那類漂移的
    即時防線是 CLI 每次 RPC 的 client↔daemon 版本比對（見 cli.py
    `_warn_version_mismatch`），與 PATH 無關。0 或 1 筆時視為健康、不另跑
    subprocess（trivially single）。
    """
    paths = fx.which_all("serialwrap")
    if len(paths) <= 1:
        detail = "僅偵測到目前這份" if paths else "PATH 上找不到 serialwrap（可能以絕對路徑呼叫）"
        return {"check": "other_serialwrap_installs", "ok": True, "detail": detail, "fix": ""}
    versions: list[str] = []
    entries: list[str] = []
    for p in paths:
        rc, out, _err = fx.run([p, "--version"], timeout_s=2.0)
        v = out.strip() if rc == 0 and out.strip() else "無法取得"
        versions.append(v)
        entries.append(f"{p}={v}")
    ok = len(set(versions)) == 1
    fix = "" if ok else (
        "確認各安裝版本一致，或統一改呼叫同一份"
        "（建議 pipx 系統安裝／以絕對路徑或 SERIALWRAP_ENDPOINT 釘住）"
    )
    return {"check": "other_serialwrap_installs", "ok": ok, "detail": "；".join(entries), "fix": fix}


def _check_dialout(fx) -> dict:
    """目前使用者是否屬於 dialout 群組（存取 /dev/ttyUSB* 必需）。"""
    ok = fx.user_in_group("dialout")
    return {
        "check": "dialout",
        "ok": ok,
        "detail": "已在 dialout 群組" if ok else "不在 dialout 群組",
        "fix": "" if ok else "sudo usermod -aG dialout $USER（之後重新登入）",
    }


def _check_systemd(fx) -> dict:
    """systemd 是否為 init（advisory：缺少不致命，可走 on-demand）。"""
    ok = fx.has_systemd()
    return {
        "check": "systemd",
        "ok": ok,
        "detail": "systemd 為 init" if ok else "未偵測到 systemd",
        "fix": "" if ok else (
            "WSL 可於 /etc/wsl.conf 設 [boot] systemd=true 後 wsl --shutdown"
            "（on-demand 模式可忽略）"
        ),
    }


def _check_supervision_mode(home) -> dict:
    """目前有效的監管模式（advisory，永遠 ok）。

    以 ``_safe_runtime_config`` 讀取（#132 review）：config.yaml 損壞/不可讀時
    退化回報 on-demand 預設，維持 run_doctor「永不拋例外」契約。
    """
    # 延遲匯入：避免 doctor 對 cli 形成匯入循環，且 mode 在呼叫時才解析。
    from sw_core.cli import _safe_runtime_config

    rc = _safe_runtime_config()
    mode = (rc.mode() if rc is not None else None) or "on-demand"
    return {"check": "supervision_mode", "ok": True, "detail": mode, "fix": ""}


def _check_devices() -> dict:
    """是否有 USB-serial 裝置出現在 by-id 目錄。"""
    detail = "0"
    ok = False
    try:
        if os.path.isdir(DEVICE_BY_ID_DIR):
            entries = os.listdir(DEVICE_BY_ID_DIR)
            ok = len(entries) > 0
            detail = str(len(entries))
    except Exception:  # pragma: no cover - 環境相依
        ok = False
        detail = "無法讀取"
    return {
        "check": "devices",
        "ok": ok,
        "detail": detail,
        "fix": "" if ok else (
            "確認 USB-serial 已接上且有 /dev/serial/by-id/* "
            "（或設 SERIALWRAP_BY_ID_DIR）"
        ),
    }


def _check_wsl_systemd(fx) -> dict:
    """WSL 環境下是否啟用 systemd（非 WSL 一律視為 ok）。"""
    is_wsl = fx.is_wsl()
    ok = (not is_wsl) or fx.has_systemd()
    if not is_wsl:
        detail = "非 WSL 環境"
    elif ok:
        detail = "WSL 已啟用 systemd"
    else:
        detail = "WSL 未啟用 systemd"
    return {
        "check": "wsl_systemd",
        "ok": ok,
        "detail": detail,
        "fix": "" if ok else (
            "於 /etc/wsl.conf 設 [boot] systemd=true 後 wsl --shutdown"
            "（on-demand 模式可忽略）"
        ),
    }


def _check_single_daemon(proc_root: str = "/proc") -> dict:
    """是否只有單一 serialwrapd 在跑（#101）。

    daemon-less：doctor 為獨立程序、不碰 socket，直接掃 /proc 找 serialwrapd。
    多開（不同 socket / systemd-user 與 system 同時在跑）會造成 two-reader 靜默掉字，
    這裡僅偵測 + 回報，不終止任何 daemon。
    """
    from .multi_open import detect_multi_open

    res = detect_multi_open(proc_root=proc_root)
    n = len(res["daemons"])
    ok = not res["multi_open"]
    detail = f"{n} 個 serialwrapd 在跑" + ("（偵測到多開）" if res["multi_open"] else "")
    fix = (
        ""
        if ok
        else "停掉多餘 daemon（serialwrap service stop；並檢查 systemd-user 與 system 是否同時在跑）"
    )
    return {"check": "single_daemon", "ok": ok, "detail": detail, "fix": fix}


def _check_pyserial() -> dict:
    """pyserial 是否可匯入（Windows 序列埠後端 _PySerialPort 需要，#84 PORT-1）。"""
    try:
        import serial  # noqa: F401,PLC0415

        ok = True
        detail = getattr(serial, "__version__", "已安裝")
    except Exception:  # pragma: no cover - 環境相依
        ok = False
        detail = "未安裝"
    return {
        "check": "pyserial",
        "ok": ok,
        "detail": detail,
        "fix": "" if ok else "安裝 pyserial（pipx 於 Windows 自動帶入；release exe 已內嵌）",
    }


def _check_daemon_endpoint() -> dict:
    """daemon RPC TCP endpoint 是否可連（Windows，advisory：未起 daemon 不致命）。

    與 CLI 的 endpoint 解析同 seam（``_local_default_endpoint``）：config 記錄非
    tcp:// 的殘留值（如 WSL unix 路徑）時視為缺席、改探測 canonical tcp，避免
    doctor 與 CLI 的 #108 fallback 行為分歧（#131 review）。
    """
    from sw_core.cli import _local_default_endpoint, _safe_runtime_config  # 延遲匯入避免循環
    from sw_core.lock_win import _endpoint_alive

    rc = _safe_runtime_config()
    cfg_sock = None
    if rc is not None:
        try:
            cfg_sock = rc.socket_path()
        except Exception:  # noqa: BLE001
            cfg_sock = None
    if cfg_sock and not str(cfg_sock).startswith("tcp://"):
        cfg_sock = None  # unix 殘留 → 視為缺席，探測 canonical（同 CLI fallback 語意）
    endpoint = cfg_sock or _local_default_endpoint()
    ok = False
    try:
        ok = _endpoint_alive(endpoint)
    except Exception:  # pragma: no cover - 探測永不拋
        ok = False
    return {
        "check": "daemon_endpoint",
        "ok": ok,
        "detail": f"{endpoint}（{'可連' if ok else '未在跑'}）",
        "fix": "" if ok else "serialwrap daemon start（或 serialwrapd.exe --socket tcp://127.0.0.1:48700）",
    }


def _check_devices_windows() -> dict:
    """SERIALCOMM 登錄列舉 COM 裝置（排除藍牙與 windows.exclude_coms，#84 PORT-4）。"""
    try:
        from sw_core.device_source import (
            _load_exclude_coms,
            _read_bt_ports,
            _read_serialcomm,
            exclude_bluetooth,
        )

        manual = _load_exclude_coms()
        serialcomm = _read_serialcomm()
        bt_ports = _read_bt_ports()
        kept = exclude_bluetooth(serialcomm, bt_ports, manual)
        excluded = len(serialcomm) - len(kept)
        ok = len(kept) > 0
        detail = f"{len(kept)}（{', '.join(sorted(kept))}）" if kept else "0"
        if excluded:
            detail += f"；排除 {excluded}（藍牙/exclude_coms）"
    except Exception:  # pragma: no cover - 環境相依（registry 不可讀等）
        ok = False
        detail = "無法讀取 SERIALCOMM"
    return {
        "check": "devices",
        "ok": ok,
        "detail": detail,
        "fix": "" if ok else "確認 USB-serial 已接上並出現在裝置管理員（藍牙 COM 會被自動排除）",
    }


def _check_wal_dir() -> dict:
    """WAL 目錄一致性（#148）：印出 daemon 實際生效的 WAL_DIR；shell 端顯式覆寫
    ``SERIALWRAP_WAL_DIR`` 但與 daemon 回報不一致時 WARN（ok=False，advisory，
    不拉低整體 ok）。

    daemon 未在跑／連不上時降級為 informational（ok=True）——doctor 常在啟動
    daemon 前執行，「連不到」本身不是這項檢查要抓的錯誤。RPC 探測沿用其餘
    advisory 檢查的 0.5s 短逾時、經 ``rpc_call`` 的『永不拋例外』契約
    （``sw_core/client.py`` 的 ``_rpc_call_once`` 只回 dict、不 raise），故本函式
    不需要外層 try/except 仍保有 ``run_doctor`` 『永不拋例外』的整體契約。
    """
    from sw_core.cli import _local_default_endpoint, _safe_runtime_config  # 延遲匯入避免循環
    from sw_core.client import rpc_call
    from sw_core.constants import WAL_DIR as local_wal_dir  # noqa: N811 — 僅供 fallback 顯示

    rc = _safe_runtime_config()
    cfg_sock = None
    if rc is not None:
        try:
            cfg_sock = rc.socket_path()
        except Exception:  # noqa: BLE001
            cfg_sock = None
    endpoint = cfg_sock or _local_default_endpoint()

    resp = rpc_call(endpoint, "health.status", {}, timeout_s=0.5)
    if not resp.get("ok") or not resp.get("wal_path"):
        return {
            "check": "wal_dir",
            "ok": True,
            "detail": f"daemon 未在跑或無法連線，僅顯示本地端解析值：WAL_DIR={local_wal_dir}",
            "fix": "",
        }

    daemon_wal_dir = os.path.dirname(str(resp["wal_path"]))
    shell_override = os.environ.get("SERIALWRAP_WAL_DIR", "").strip()
    mismatch = bool(shell_override) and (
        os.path.normpath(os.path.expanduser(shell_override)) != os.path.normpath(daemon_wal_dir)
    )
    if mismatch:
        return {
            "check": "wal_dir",
            "ok": False,
            "detail": f"daemon 實際生效 WAL_DIR={daemon_wal_dir}，與 shell SERIALWRAP_WAL_DIR={shell_override} 不一致",
            "fix": (
                "daemon 由 systemd 管理時 unit 不會繼承 shell 匯出的 env（.bashrc 的 export 對它無效）："
                "需在 unit 加 Environment=SERIALWRAP_WAL_DIR=<path> 後 systemctl daemon-reload "
                "並 `serialwrap service restart`；on-demand/前景啟動則需在同一個帶該 env 的 shell "
                "重跑 `serialwrap daemon stop && serialwrap daemon start`。查詢一律以 "
                "`serialwrap daemon status` 的 wal_path 為準，不要用 shell env 去猜"
            ),
        }
    return {
        "check": "wal_dir",
        "ok": True,
        "detail": f"daemon 實際生效 WAL_DIR={daemon_wal_dir}",
        "fix": "",
    }


def run_doctor(fx=None, home=None, *, platform: str | None = None) -> list[dict]:
    """執行所有環境檢查並回傳結果清單（每項皆唯讀、永不拋例外）。

    Args:
        fx:       effects 介面；``None`` 時用 :class:`SystemEffects`。
        home:     使用者家目錄（目前僅 supervision_mode 取用，保留供測試）。
        platform: 平台字串（``sys.platform`` 語意）；``None`` 時取實際平台。
                  ``win*`` → Windows 檢查清單（#131 點 4：pyserial／PATH／daemon
                  endpoint／SERIALCOMM 裝置），其餘 → Linux 清單（dialout／
                  human console 就緒組：serialwrap-minicom／jq／minicom 是否在
                  PATH（#149）／systemd／single_daemon／by-id devices／
                  wsl_systemd）。

    Returns:
        檢查結果清單，每項為
        ``{"check", "ok", "detail", "fix"}``。
    """
    fx = fx if fx is not None else SystemEffects()
    plat = platform if platform is not None else sys.platform
    if plat.startswith("win"):
        # Windows 單例由 WindowsSingletonLock（msvcrt 檔鎖 + TCP probe）強制，
        # 無 /proc 可掃 → 不移植 single_daemon；dialout/systemd/wsl_systemd 不適用。
        win_path_hint = "將 serialwrap.exe / serialwrapd.exe 所在目錄加入 PATH"
        return [
            _check_python(),
            _check_pyyaml(),
            _check_pyserial(),
            _check_on_path(fx, "serialwrap", win_path_hint),
            _check_on_path(fx, "serialwrapd", win_path_hint),
            _check_other_serialwrap_installs(fx),
            _check_supervision_mode(home),
            _check_daemon_endpoint(),
            _check_wal_dir(),
            _check_devices_windows(),
        ]
    return [
        _check_python(),
        _check_pyyaml(),
        _check_on_path(fx, "serialwrap"),
        _check_on_path(fx, "serialwrapd"),
        _check_other_serialwrap_installs(fx),
        _check_dialout(fx),
        # human console 就緒檢查組（#149）：wrapper／jq／minicom 是否在 PATH——
        # doctor 全綠不等於 human console 真能動，補齊這個空白（見 issue #149
        # root_cause）。三項皆 fx.which() 純查表、無 I/O 副作用。
        _check_on_path(
            fx, "serialwrap-minicom", check_name="serialwrap_minicom_on_path",
            fix_hint=(
                "執行 `serialwrap setup` 物化 minicom wrapper 到 ~/.local/bin"
                "（並確認該目錄在 PATH，可用 pipx ensurepath）；"
                "human console 一律經 serialwrap-minicom COMx，勿直接對 tty 開 minicom（避免與 daemon two-reader 衝突）"
            ),
        ),
        _check_on_path(fx, "jq", fix_hint="安裝 jq（Debian/Ubuntu/Mint: sudo apt install jq）——serialwrap-minicom wrapper 解析 session 狀態需要"),
        _check_on_path(fx, "minicom", fix_hint="安裝 minicom（Debian/Ubuntu/Mint: sudo apt install minicom）"),
        _check_systemd(fx),
        _check_supervision_mode(home),
        _check_single_daemon(),
        _check_wal_dir(),
        _check_devices(),
        _check_wsl_systemd(fx),
    ]
