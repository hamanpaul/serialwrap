#!/usr/bin/env python3
"""Stage 0 Python 基線行為變更凍結值擷取腳本（P1 Task T19）。

在暫存目錄以真實的 WalWriter 與 SessionManager 實跑各情境，推導並輸出：
(1) --raw-out（可選）：原始證據（未正規化）；
(2) --out：凍結語意投影（DISK_STABLE 序列化：sort_keys、indent=2、尾端換行），
    schema 為 contracts/captures/stage0-python-bc.schema.json。

依據：docs/rust-refactor/plans/02-stage0-python-bc.md Task T19。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

import sw_core.constants as const
import sw_core.session_manager as sm_mod
from stage0_fakes import FakePort, make_bridge, make_manager, make_profile
from sw_core.config import UartProfile
from sw_core.serial_port import DeviceHeldError, DeviceNotPresentError, HolderReport
from sw_core.uart_io import UARTBridge
from sw_core.wal import WalWriter

BY_ID = "/dev/serial/by-id/X"


def _get_git_revision() -> str:
    """取得當前 git revision HEAD。"""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return "0" * 40


def _normalize_meta_record(rec: dict[str, Any], tmp_dir: str) -> dict[str, Any]:
    """依 schema x-normalized 清單正規化 META 記錄。"""
    normalized = json.loads(json.dumps(rec))
    normalized["mono_ts_ns"] = 0
    normalized["wall_ts"] = "<wall_ts>"
    if normalized.get("cmd_id") is not None:
        normalized["cmd_id"] = "<uuid>"

    meta = normalized.get("meta", {})
    if "previous_identity" in meta and meta["previous_identity"] is not None:
        # 將 "dev:ino" 替換為 "<id>:<id>"
        meta["previous_identity"] = re.sub(r"^\d+:\d+$", "<id>:<id>", str(meta["previous_identity"]))
    if tmp_dir:
        # 替換任何暫存路徑前綴
        s = json.dumps(meta)
        s = s.replace(tmp_dir, "<tmp>")
        normalized["meta"] = json.loads(s)
    return normalized


def capture_wal_behavior(raw_artifacts: dict[str, Any]) -> dict[str, Any]:
    """實跑 WalWriter 各情境推導 WAL 凍結語意。"""
    rotate_bytes_constant = const.DEFAULT_WAL_ROTATE_BYTES

    # 1. 驗證 rotate 門檻（size < R 不 rotate，size >= R 於寫入前 rotate）
    with tempfile.TemporaryDirectory() as td:
        w = WalWriter(wal_dir=td, rotate_bytes=1000)
        w.append(com="COM0", direction="TX", source="agent:x", payload=b"123")
        sz = os.path.getsize(w.wal_path)

        # R - 1：不應觸發 rotate
        w._rotate_bytes = sz + 1
        w.append(com="COM0", direction="TX", source="agent:x", payload=b"456")
        archives_before = glob.glob(os.path.join(td, "raw.wal.ndjson.*"))
        if len(archives_before) != 0:
            sys.exit("實測錯誤：size < rotate_bytes 發生了 rotate")

        # 恰好等於 R：下一筆應於寫入前 rotate
        cur_sz = os.path.getsize(w.wal_path)
        w._rotate_bytes = cur_sz
        w.append(com="COM0", direction="TX", source="agent:x", payload=b"789")
        archives_after = glob.glob(os.path.join(td, "raw.wal.ndjson.*"))
        if len(archives_after) != 1:
            sys.exit("實測錯誤：size >= rotate_bytes 未觸發 before_write rotate")
        w.close()

    # 2. 實測 rotate 後 seq 連續性
    with tempfile.TemporaryDirectory() as td:
        w = WalWriter(wal_dir=td, rotate_bytes=1000)
        r1 = w.append(com="COM0", direction="TX", source="agent:x", payload=b"pre-rotate")
        cur_sz = os.path.getsize(w.wal_path)
        w._rotate_bytes = cur_sz
        r2 = w.append(com="COM0", direction="TX", source="agent:x", payload=b"post-rotate")
        seq_after_rotate = r2["seq"] - r1["seq"]
        raw_artifacts["wal_rotate"] = {"r1": r1, "r2": r2, "seq_delta": seq_after_rotate}
        w.close()

    # 3. 實測 reset 後 seq
    with tempfile.TemporaryDirectory() as td:
        w = WalWriter(wal_dir=td)
        w.append(com="COM0", direction="TX", source="agent:x", payload=b"item1")
        w.append(com="COM0", direction="TX", source="agent:x", payload=b"item2")
        w.reset()
        r_reset = w.append(com="COM0", direction="TX", source="agent:x", payload=b"after_reset")
        seq_after_reset = r_reset["seq"]
        raw_artifacts["wal_reset"] = {"r_reset": r_reset}
        w.close()

    # 4. 實測 restart 後 seq
    with tempfile.TemporaryDirectory() as td:
        w = WalWriter(wal_dir=td)
        w.append(com="COM0", direction="TX", source="agent:x", payload=b"p1")
        w.append(com="COM0", direction="TX", source="agent:x", payload=b"p2")
        max_before = w.current_seq
        w.close()
        w2 = WalWriter(wal_dir=td)
        r_restart = w2.append(com="COM0", direction="TX", source="agent:x", payload=b"restarted")
        seq_after_restart = r_restart["seq"] - max_before
        raw_artifacts["wal_restart"] = {"max_before": max_before, "r_restart": r_restart}
        w2.close()

    # 5. 實測 external unlink 自癒與 META 記錄
    with tempfile.TemporaryDirectory() as td:
        w = WalWriter(wal_dir=td)
        w.append(com="COM0", direction="TX", source="agent:x", payload=b"u1")
        w.append(com="COM0", direction="TX", source="agent:x", payload=b"u2")
        prev_seq = w.current_seq
        os.unlink(w.wal_path)
        r_after = w.append(com="COM0", direction="TX", source="agent:x", payload=b"u3")

        lines = [json.loads(line) for line in open(w.wal_path, encoding="utf-8")]
        meta_record = lines[0]
        rec_record = lines[1]
        raw_artifacts["wal_unlink"] = {
            "prev_seq": prev_seq,
            "meta_record": meta_record,
            "rec_record": rec_record,
        }
        meta_seq_delta = meta_record["seq"] - prev_seq
        record_seq_delta = rec_record["seq"] - meta_record["seq"]
        meta_recreated_example = _normalize_meta_record(meta_record, td)
        w.close()

    # 6. 空 payload CRC 與空 payload 屬性
    with tempfile.TemporaryDirectory() as td:
        w = WalWriter(wal_dir=td)
        rec_empty = w.append(com="COM0", direction="TX", source="agent:x", payload=b"")
        crc_empty = rec_empty["crc32"]
        meta_empty_payload = {
            "len": rec_empty["len"],
            "payload_b64": rec_empty["payload_b64"],
        }
        raw_artifacts["wal_empty"] = rec_empty
        w.close()

    return {
        "rotate_bytes": rotate_bytes_constant,
        "rotate_compare": ">=",
        "rotate_check": "before_write",
        "crc_empty": crc_empty,
        "meta_empty_payload": meta_empty_payload,
        "seq_after_rotate": seq_after_rotate,
        "seq_after_reset": seq_after_reset,
        "seq_after_restart": seq_after_restart,
        "seq_after_external_unlink": {
            "meta_seq_delta": meta_seq_delta,
            "record_seq_delta": record_seq_delta,
        },
        "meta_recreated_example": meta_recreated_example,
    }


def capture_bc03_mapping(raw_artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    """實跑 SessionManager._map_aborted_result 推導 BC-03 中止映射。"""
    cases = [
        ((2, False), True),
        ((2, True), True),
        ((2, False), False),
        ((0, True), True),
        ((0, True), False),
        ((0, False), True),
        ((0, False), False),
    ]
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mgr = make_manager(tmp, [make_profile()])
        wal = mgr._wal
        b = UARTBridge("COM0", "fake", make_profile().uart, wal=wal)
        b._serial = FakePort()
        b._tx_accounts = {}

        for account, aborted_by_drain in cases:
            cmd_id = f"cmd_{account[0]}_{account[1]}_{aborted_by_drain}"
            b._tx_accounts[cmd_id] = account
            res = mgr._map_aborted_result(
                cmd_id,
                {"ok": False},
                bridge=b,
                aborted_by_drain=aborted_by_drain,
            )
            results.append({
                "account": list(account),
                "aborted_by_drain": aborted_by_drain,
                "expected": res["error_code"],
            })
        b.stop()
        wal.close()
    raw_artifacts["bc03_mapping"] = results
    return results


def capture_release_drain_timeout_shape(raw_artifacts: dict[str, Any]) -> dict[str, Any]:
    """實跑 SessionManager.release_device 逾時情境推導回傳形狀。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mgr = make_manager(tmp, [make_profile(by_id=BY_ID)])
        gate = threading.Event()
        port = FakePort(block=gate)
        s = mgr.get_session("COM0")
        b = UARTBridge(com="COM0", device_path="fake", profile=UartProfile(), wal=mgr._wal)
        b._serial = port
        b._serial_fd = None
        with mgr._lock:
            s.bridge = b
            s.state = "READY"
            s.bridge_generation = 4

        t = threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True)
        t.start()
        time.sleep(0.05)

        orig_drain_const = const.DRAIN_TIMEOUT_MS
        orig_drain_sm = sm_mod.DRAIN_TIMEOUT_MS
        try:
            const.DRAIN_TIMEOUT_MS = 50
            sm_mod.DRAIN_TIMEOUT_MS = 50
            r = mgr.release_device("COM0")
        finally:
            const.DRAIN_TIMEOUT_MS = orig_drain_const
            sm_mod.DRAIN_TIMEOUT_MS = orig_drain_sm
            gate.set()
            t.join(2.0)
            mgr._wal.close()
            b.stop()

    raw_artifacts["release_drain_timeout"] = r
    return {
        "keys": list(r.keys()),
        "error_code": r["error_code"],
        "drain_stalled": r["drain_stalled"],
        "drain_timeout_epoch_type": type(r["drain_timeout_epoch"]).__name__,
    }


def capture_device_attach_results(raw_artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    """實跑 SessionManager.attach_device 各情境推導 T10 結果表情境。"""
    cases: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mgr = make_manager(tmp, [make_profile(by_id=BY_ID)])
        s = mgr.get_session("COM0")
        bridge, _ = make_bridge(tmp, port=FakePort())

        # 1. drain_in_progress
        s.state = "RECOVERING"
        mgr._draining_by_ids[BY_ID] = object()
        r1 = mgr.attach_device("COM0")
        cases.append({
            "case": "drain_in_progress",
            "expected": {
                "ok": r1["ok"],
                "error_code": r1.get("error_code"),
                "state": s.state,
            },
        })
        mgr._draining_by_ids.clear()

        # 2. already_attached
        s.state = "READY"
        s.bridge = bridge
        r2 = mgr.attach_device("COM0")
        cases.append({
            "case": "already_attached",
            "expected": {
                "ok": r2["ok"],
                "error_code": r2.get("error_code"),
                "state": s.state,
            },
        })
        s.bridge = None

        # 設定為 RELEASED
        s.state = "RELEASED"
        mgr._released_by_ids.add(BY_ID)

        # 3. device_not_present
        mgr._devices.clear()
        r3 = mgr.attach_device("COM0")
        cases.append({
            "case": "device_not_present",
            "expected": {
                "ok": r3["ok"],
                "error_code": r3.get("error_code"),
                "state": s.state,
            },
        })

        # 加入裝置物件
        dev = types.SimpleNamespace(real_path="/dev/ttyFake")
        mgr._devices[BY_ID] = dev

        # 4. held_by_external_probe
        with patch.object(
            mgr,
            "_probe_external_holder",
            return_value={"pids": [12345], "holder_unknown": False, "status": "ok"},
        ):
            r4 = mgr.attach_device("COM0", force=False)
            cases.append({
                "case": "held_by_external_probe",
                "expected": {
                    "ok": r4["ok"],
                    "error_code": r4.get("error_code"),
                    "state": s.state,
                },
            })

        # 5. held_by_open_conflict
        hr = HolderReport((12345,), False, "ok")
        with patch("sw_core.uart_io.open_serial_port", side_effect=DeviceHeldError("/dev/ttyFake", holder=hr)):
            r5 = mgr.attach_device("COM0", force=True)
            cases.append({
                "case": "held_by_open_conflict",
                "expected": {
                    "ok": r5["ok"],
                    "error_code": r5.get("error_code"),
                    "state": s.state,
                },
            })

        # 6. open_failed_device_not_present
        with patch("sw_core.uart_io.open_serial_port", side_effect=DeviceNotPresentError()):
            r6 = mgr.attach_device("COM0", force=True)
            cases.append({
                "case": "open_failed_device_not_present",
                "expected": {
                    "ok": r6["ok"],
                    "error_code": r6.get("error_code"),
                    "state": s.state,
                },
            })

        # 7. open_failed_other_oserror
        with patch("sw_core.uart_io.open_serial_port", side_effect=PermissionError("denied")):
            r7 = mgr.attach_device("COM0", force=True)
            cases.append({
                "case": "open_failed_other_oserror",
                "expected": {
                    "ok": r7["ok"],
                    "error_code": r7.get("error_code"),
                    "state": s.state,
                },
            })

        # 重設回 RELEASED
        s.state = "RELEASED"
        mgr._released_by_ids.add(BY_ID)

        # 8. attach_success
        fake_port = FakePort()
        with patch("sw_core.uart_io.open_serial_port", return_value=fake_port):
            r8 = mgr.attach_device("COM0", force=True)
            cases.append({
                "case": "attach_success",
                "expected": {
                    "ok": r8["ok"],
                    "error_code": r8.get("error_code"),
                    "state": s.state,
                },
            })

        bridge.stop()
        mgr._wal.close()

    raw_artifacts["device_attach_results"] = cases
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 0 Python 基線凍結值擷取工具")
    parser.add_argument("--out", type=Path, default=REPO / "contracts" / "captures" / "stage0-python-bc.json")
    parser.add_argument("--raw-out", type=Path, default=None)
    args = parser.parse_args()

    raw_artifacts: dict[str, Any] = {}

    wal_data = capture_wal_behavior(raw_artifacts)
    bc03_data = capture_bc03_mapping(raw_artifacts)
    drain_shape = capture_release_drain_timeout_shape(raw_artifacts)
    attach_results = capture_device_attach_results(raw_artifacts)
    git_rev = _get_git_revision()

    result = {
        "schema_version": 1,
        "git_revision": git_rev,
        "wal": wal_data,
        "bc03_mapping": bc03_data,
        "release_drain_timeout_shape": drain_shape,
        "device_attach_results": attach_results,
    }

    if args.raw_out is not None:
        args.raw_out.mkdir(parents=True, exist_ok=True)
        raw_path = args.raw_out / "raw_evidence.json"
        raw_path.write_text(json.dumps(raw_artifacts, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
