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


def _check_wal_writable() -> dict:
    """WAL 可寫性（#189）：daemon 回報的 WAL 目錄是否**真的存在且可寫**。

    #148 的 ``wal_dir`` 檢查只比對 shell/daemon 的 WAL_DIR 是否一致並印出路徑，
    **從不檢查該路徑是否存在**——實地事故中它對著一個已被 rmtree 的目錄回 ok:true，
    而 daemon 已經對著虛空累加 seq 六天、該 bench 的 console 紀錄全部無法回溯。
    這一項本可以在六天前就抓到。

    本檢查**非 advisory**：稽核紀錄整個消失必須拉低 doctor 整體 ok（``wal_dir``
    的一致性提醒維持 advisory WARN，兩者各司其職）。daemon 未在跑／連不上／舊版
    daemon 不回 ``wal`` 欄位時降級為 informational（ok=True），與 ``wal_dir`` 同
    哲學——doctor 常在啟動 daemon 前執行，「連不到」不是這項要抓的錯誤。
    """
    from sw_core.cli import _local_default_endpoint, _safe_runtime_config  # 延遲匯入避免循環
    from sw_core.client import rpc_call

    rc = _safe_runtime_config()
    cfg_sock = None
    if rc is not None:
        try:
            cfg_sock = rc.socket_path()
        except Exception:  # noqa: BLE001
            cfg_sock = None
    endpoint = cfg_sock or _local_default_endpoint()

    resp = rpc_call(endpoint, "health.status", {}, timeout_s=0.5)
    wal = resp.get("wal") if isinstance(resp, dict) else None
    if not resp.get("ok") or not isinstance(wal, dict):
        return {
            "check": "wal_writable",
            "ok": True,
            "detail": "daemon 未在跑、無法連線，或為不回報 wal 健康欄位的舊版 daemon；略過",
            "fix": "",
        }
    if wal.get("healthy"):
        detail = f"WAL 目錄可寫：{wal.get('wal_dir')}（current_seq={wal.get('current_seq')}）"
        if wal.get("recreated_count"):
            detail += f"；曾自癒重建 {wal['recreated_count']} 次（消失期間的紀錄無法復原）"
        return {"check": "wal_writable", "ok": True, "detail": detail, "fix": ""}

    reasons: list[str] = []
    if not wal.get("wal_dir_exists"):
        reasons.append("目錄不存在")
    elif not wal.get("wal_dir_writable"):
        reasons.append("目錄不可寫")
    if int(wal.get("current_seq") or 0) > 0 and not wal.get("wal_file_exists"):
        reasons.append(f"已寫過 {wal.get('current_seq')} 筆紀錄但現行 WAL 檔不存在")
    if wal.get("write_failures"):
        reasons.append(
            f"append 失敗 {wal['write_failures']} 次（最後一次：{wal.get('last_write_error')}）"
        )
    return {
        "check": "wal_writable",
        "ok": False,
        "detail": f"WAL 目錄 {wal.get('wal_dir')} 異常：{'；'.join(reasons) or '未知'}",
        "fix": (
            "確認沒有外部工具刪除該目錄（已知案例：testpilot 的 clean_wal() 會 rmtree "
            "硬編路徑 /tmp/serialwrap/wal，見 hamanpaul/testpilot-core#36）。daemon 會在"
            "下一次寫入時自動重建目錄續寫，但消失期間的紀錄無法復原；需要持久稽核請把 "
            "WAL_DIR 指向非 /tmp 的路徑（systemd 模式在 unit 加 "
            "Environment=SERIALWRAP_WAL_DIR=<path> 後 systemctl daemon-reload 並 "
            "`serialwrap service restart`）"
        ),
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


def _check_endpoint_reachable(proc_root: str = "/proc") -> dict:
    """本 client 解析到的 endpoint 是否與實際執行中的 daemon 一致且可連（#173）。

    根因：POSIX daemon 過去不寫 config.yaml 的 socket_path，加上部署 wrapper 常以
    ``SERIALWRAP_STATE_DIR`` 搬移 socket，使『daemon 實際在哪』與『本 client 會解析到
    哪』可能永久分歧，且此前沒有任何診斷指出這件事。

    判定（與 CLI 相同的解析 seam，見 :func:`sw_core.cli._resolve_default_endpoint_with_source`）：

    - ``/proc`` 掃不到任何 serialwrapd 行程 → advisory ok（on-demand 模式尚未啟動
      daemon 本身不是異常）。
    - 有 daemon 行程，本 client 解析到的 endpoint 可連 → ok。
    - 有 daemon 行程，但本 client 解析到的 endpoint 連不上 → **not ok**，detail 同時
      點出本 client 解析到的路徑（含來源）與實際執行中 daemon 綁定的路徑，方便 operator
      在一分鐘內對照 SERIALWRAP_STATE_DIR／config.yaml 是否同步（而非像事故現場那樣
      耗掉一整個下午）。
    """
    from sw_core.cli import _endpoint_alive, _resolve_default_endpoint_with_source  # 延遲匯入避免循環
    from .multi_open import detect_multi_open

    try:
        resolved, source = _resolve_default_endpoint_with_source()
    except Exception as exc:  # pragma: no cover - 探測永不拋
        return {
            "check": "endpoint_reachable",
            "ok": True,
            "detail": f"無法解析本 client 的 endpoint（{exc}），略過",
            "fix": "",
        }

    mo = detect_multi_open(proc_root=proc_root)
    daemons = mo["daemons"]
    if not daemons:
        return {
            "check": "endpoint_reachable",
            "ok": True,
            "detail": f"未偵測到執行中的 serialwrapd（on-demand 模式正常）；本 client 解析到 {resolved}（來源：{source}）",
            "fix": "",
        }

    try:
        reachable = _endpoint_alive(resolved)
    except Exception:  # pragma: no cover - 探測永不拋
        reachable = False
    if reachable:
        return {
            "check": "endpoint_reachable",
            "ok": True,
            "detail": f"本 client 解析到 {resolved}（來源：{source}），可連上執行中 daemon",
            "fix": "",
        }

    daemon_sockets = sorted({d.get("socket") or "未知（無法從 /proc 讀出 --socket）" for d in daemons})
    daemon_desc = "、".join(daemon_sockets)
    return {
        "check": "endpoint_reachable",
        "ok": False,
        "detail": (
            f"本 client 解析到 {resolved}（來源：{source}）連不上；"
            f"目前執行中 daemon 綁在 {daemon_desc}"
        ),
        "fix": (
            "確認本 client 的 SERIALWRAP_STATE_DIR/config.yaml 是否與該 daemon 一致"
            "（可用 --socket/--endpoint 明確指定該路徑繞過解析；"
            "daemon 若為 #173 修正後版本，重啟一次即會把實際 socket 同步寫回 config.yaml）"
        ),
    }


def _profile_templates_missing_bootloader_prompts(
    asset_text: str, live_text: str
) -> list[str]:
    """純函式：出貨資產有 ``bootloader_prompts`` 而線上檔沒有的 template 名單。

    只比對兩邊都存在的 template，且只看「資產有、線上無（或為空）」的方向——線上
    自行加配置不算漂移。YAML 解析失敗一律回空清單（doctor 不得因此失敗）。
    """
    try:
        import yaml
    except Exception:  # pragma: no cover - 環境相依
        return []
    try:
        asset = yaml.safe_load(asset_text) or {}
        live = yaml.safe_load(live_text) or {}
    except Exception:  # noqa: BLE001
        return []
    asset_profiles = asset.get("profiles") or {}
    live_profiles = live.get("profiles") or {}
    if not isinstance(asset_profiles, dict) or not isinstance(live_profiles, dict):
        return []
    drifted: list[str] = []
    for name, tpl in asset_profiles.items():
        if not isinstance(tpl, dict) or not tpl.get("bootloader_prompts"):
            continue
        live_tpl = live_profiles.get(name)
        if not isinstance(live_tpl, dict):
            continue
        if not live_tpl.get("bootloader_prompts"):
            drifted.append(str(name))
    return sorted(drifted)


def _check_profile_bootloader_prompts() -> dict:
    """線上 profile 是否漏了出貨資產有的 ``bootloader_prompts``（#162 C4，WARN）。

    實務動機：線上 ``~/.config/serialwrap/profiles/default.yaml`` 若為舊版物化結果，
    prpl-template 會缺此欄位，使「卡在 bootloader」的偵測（quiet 解除守衛、
    recovery lease 授予、self-test BOOTLOADER 分類）整條 no-op——operator 手上拿不到
    任何 daemon 給的可行動訊號。既有測試只驗資產、驗不到線上檔，故補這項。
    （偵測本身另有 ``UBOOT_FALLBACK_PROMPTS`` 兜底，故此項僅 advisory WARN。）
    """
    from sw_core.constants import PROFILE_DIR

    asset_path = os.path.join(os.path.dirname(__file__), "assets", "profiles", "default.yaml")
    live_path = os.path.join(PROFILE_DIR, "default.yaml")
    try:
        with open(asset_path, encoding="utf-8") as fh:
            asset_text = fh.read()
    except Exception:  # noqa: BLE001 - 資產不可讀時本檢查無意義，不得拋出
        return {"check": "profile_bootloader_prompts", "ok": True, "detail": "資產不可讀，略過", "fix": ""}
    try:
        with open(live_path, encoding="utf-8") as fh:
            live_text = fh.read()
    except Exception:  # noqa: BLE001 - 尚未 setup 物化過，非漂移
        return {
            "check": "profile_bootloader_prompts",
            "ok": True,
            "detail": f"線上 profile 未物化（{live_path}），略過",
            "fix": "",
        }

    drifted = _profile_templates_missing_bootloader_prompts(asset_text, live_text)
    if not drifted:
        return {"check": "profile_bootloader_prompts", "ok": True, "detail": "與出貨資產一致", "fix": ""}
    return {
        "check": "profile_bootloader_prompts",
        "ok": False,
        "detail": f"線上 {live_path} 的 {', '.join(drifted)} 缺 bootloader_prompts（出貨資產有）",
        "fix": (
            "重新物化資產或手動補上：備份現有檔後執行 `serialwrap setup`，"
            "或把 sw_core/assets/profiles/default.yaml 對應 template 的 bootloader_prompts 段落補進線上檔"
        ),
    }


# advisory 檢查名單（單一事實來源；sw_core/cli.py re-export 沿用）：這些項 ok=False
# 僅屬 WARN 性質，不拉低 doctor 整體 ok；機器消費者（realhw/regression preflight 的
# 「doctor 全綠」判定）依 run_doctor 蓋章的 per-check `advisory` 欄位識別，
# 避免 advisory WARN（如 #148 的 shell/daemon WAL_DIR 不一致提醒）誤觸 suite-refuse。
DOCTOR_ADVISORY_CHECKS = frozenset({
    "systemd", "wsl_systemd", "devices", "other_serialwrap_installs", "wal_dir",
    "serialwrap_minicom_on_path", "jq_on_path", "minicom_on_path",
    "profile_bootloader_prompts",
})
DOCTOR_ADVISORY_CHECKS_WIN = frozenset({
    "serialwrap_on_path", "serialwrapd_on_path", "daemon_endpoint",
    "devices", "other_serialwrap_installs", "wal_dir",
    "profile_bootloader_prompts",
})


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
    advisory = DOCTOR_ADVISORY_CHECKS_WIN if plat.startswith("win") else DOCTOR_ADVISORY_CHECKS

    def _stamp(report: list[dict]) -> list[dict]:
        # per-check 蓋章 advisory：機器消費者（preflight）據此不把 WARN 當 FAIL。
        for item in report:
            item["advisory"] = item.get("check") in advisory
        return report

    if plat.startswith("win"):
        # Windows 單例由 WindowsSingletonLock（msvcrt 檔鎖 + TCP probe）強制，
        # 無 /proc 可掃 → 不移植 single_daemon；dialout/systemd/wsl_systemd 不適用。
        win_path_hint = "將 serialwrap.exe / serialwrapd.exe 所在目錄加入 PATH"
        return _stamp([
            _check_python(),
            _check_pyyaml(),
            _check_pyserial(),
            _check_on_path(fx, "serialwrap", win_path_hint),
            _check_on_path(fx, "serialwrapd", win_path_hint),
            _check_other_serialwrap_installs(fx),
            _check_supervision_mode(home),
            _check_daemon_endpoint(),
            _check_wal_dir(),
            _check_wal_writable(),
            _check_profile_bootloader_prompts(),
            _check_devices_windows(),
        ])
    return _stamp([
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
        _check_endpoint_reachable(),
        _check_wal_dir(),
        _check_wal_writable(),
        _check_profile_bootloader_prompts(),
        _check_devices(),
        _check_wsl_systemd(fx),
    ])
