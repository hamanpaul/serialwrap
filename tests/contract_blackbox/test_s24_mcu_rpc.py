"""P8 Task 10（09-slice24-transfer-flash.md）：`mcu.patterns`／`mcu.status` 回應形狀與 `/dev/ttyMCU` 生命週期黑箱
（Python 基線與 Rust artifact 同檔各跑一次，A/B 以同一組期望位元組比對）。

案例 `s24-mcu-rpc-shapes`（tests/acceptance/cases.yaml，Slice 2.4 凍結時登記的節點）：
- `test_patterns_exact_bytes`：`mcu.patterns` 的 RPC 位元組（插入序）與 CLI stdout 位元組；
- `test_status_idle_no_sessions`：無 session 時 `mcu.status` 的 RPC 位元組與 CLI stdout 位元組。
其餘節點為額外覆蓋（不在凍結案例的 evidence 內）：候選順序與 `real_path`、端點 link 隨 daemon 啟停建立／移除、
`SERIALWRAP_TTYMCU_PATH` 覆寫、端點啟動失敗 exit 1。

外部 oracle：RPC 回應以 NDJSON 原文依插入序重新序列化比對（`json.loads` 保留鍵序），去除 framing 的 `id` 與
`rpc()` 尾端附加的中繼欄位 `version`／`code_fingerprint`（#154、D1：Python 由 checkout 算指紋、Rust 讀 release
manifest，null 時省略）；CLI 以 stdout 位元組比對（CLI-11.16：`sort_keys=True`，中繼欄位取同一 daemon 的實際回值）；
link 以檔案系統觀察。stdlib＋pytest＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import json
import os

import pytest

pytestmark = [pytest.mark.posix_only]

PATTERNS = {"ok": True, "patterns": [{"family": "ti-cc26xx", "probe": "55 55", "expect": "00 cc", "baud": 115200}]}
IDLE_STATUS = {"ok": True, "candidates": [], "flashing": False, "flashing_com": None, "last_detect": None}


_META_KEYS = ("version", "code_fingerprint")


def _body(raw: dict) -> dict:
    """RPC 回應本體：去 framing `id` 與中繼欄位（保留其餘鍵的插入序）。"""
    return {k: v for k, v in raw.items() if k != "id" and k not in _META_KEYS}


def _meta(raw: dict) -> dict:
    return {k: raw[k] for k in _META_KEYS if k in raw}


def _wire(d: dict) -> str:
    """RPC_WIRE：插入序、`ensure_ascii=False`、緊湊分隔。"""
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


def _cli_bytes(d: dict, meta: dict) -> bytes:
    """CLI stdout：原樣印出回應（含 CLI 自身請求的 `"id":1`）、`sort_keys=True`、緊湊分隔＋換行；
    中繼欄位取 daemon 實際回值。"""
    full = dict(d)
    full["id"] = 1
    full.update(meta)
    return (json.dumps(full, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _link(sw_env):
    return sw_env.run_dir / "dev" / "ttyMCU"


def _start_idle(sw_env) -> None:
    """無 target、by-id 目錄為空（全走動態偵測且無裝置）：daemon 無任何 session。"""
    sw_env.start(targets={}, profile="passthrough")
    sw_env.wait_until(lambda: sw_env.rpc("session.list", {}).get("sessions") == [], timeout=10)


@pytest.mark.process_rpc
def test_patterns_exact_bytes(sw_env):
    _start_idle(sw_env)
    raw = sw_env.rpc("mcu.patterns", {})
    assert _wire(_body(raw)) == _wire(PATTERNS)
    r = sw_env.cli("mcu", "patterns")
    assert r.exit == 0, r.stderr
    assert r.stdout == _cli_bytes(PATTERNS, _meta(raw))
    # `--retries` 唯讀白名單（§4.2）：daemon 在線時與單次呼叫同位元組。
    r2 = sw_env.cli("--retries", "1", "mcu", "patterns")
    assert (r2.exit, r2.stdout) == (0, r.stdout), r2.stderr


@pytest.mark.process_rpc
def test_status_idle_no_sessions(sw_env):
    _start_idle(sw_env)
    raw = sw_env.rpc("mcu.status", {})
    assert _wire(_body(raw)) == _wire(IDLE_STATUS)
    r = sw_env.cli("mcu", "status")
    assert r.exit == 0, r.stderr
    assert r.stdout == _cli_bytes(IDLE_STATUS, _meta(raw))
    r2 = sw_env.cli("--retries", "1", "mcu", "status")
    assert (r2.exit, r2.stdout) == (0, r.stdout), r2.stderr


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_status_candidates_and_ttymcu_lifecycle(sw_env, fake_mcu):
    """passthrough（非 command_capable）ATTACHED 的 COM0 是候選，`real_path` 為 by-id 解析後的實體路徑；
    端點 link 在 daemon 服務中存在、停止後移除。"""
    sw_env.start(targets={"COM0": fake_mcu}, profile="passthrough")
    sw_env.wait_state("COM0", "ATTACHED", timeout=10)
    link = _link(sw_env)
    sw_env.wait_until(link.is_symlink, timeout=10)
    by_id = sw_env.session("COM0")["device_by_id"]
    want = {
        "ok": True,
        "candidates": [{"com": "COM0", "by_id": by_id, "real_path": os.path.realpath(by_id), "command_capable": False}],
        "flashing": False,
        "flashing_com": None,
        "last_detect": None,
    }
    assert _wire(_body(sw_env.rpc("mcu.status", {}))) == _wire(want)
    assert fake_mcu.rx_log() == b"", "mcu.status 不得觸發任何 TX"
    sw_env.stop()
    assert not link.is_symlink() and not link.exists(), "daemon 停止後端點 link 必須移除"


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_ttymcu_path_env_override(sw_env):
    alt = sw_env.base / "alt-dev" / "mcu-link"
    sw_env._env["SERIALWRAP_TTYMCU_PATH"] = str(alt)  # SwEnv 的隔離 env（start 前設定）
    _start_idle(sw_env)
    sw_env.wait_until(alt.is_symlink, timeout=10)
    assert not _link(sw_env).exists() and not _link(sw_env).is_symlink(), "覆寫後不得再建預設路徑"
    sw_env.stop()
    assert not alt.is_symlink() and not alt.exists()


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_ttymcu_start_failure_exits_1(sw_env):
    """端點 link 的上層是一般檔案（目錄無法建立）→ daemon exit 1、不留 RPC socket。"""
    blocker = sw_env.base / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    sw_env._env["SERIALWRAP_TTYMCU_PATH"] = str(blocker / "ttyMCU")
    sw_env.prepare(targets={}, profile="passthrough")
    with pytest.raises(RuntimeError, match=r"rc=1$"):
        sw_env._start_daemon(timeout_s=15)
    assert not os.path.exists(sw_env.socket), "端點啟動失敗不得留下 RPC socket"
