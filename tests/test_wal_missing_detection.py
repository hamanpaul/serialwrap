"""WAL 檔案不存在時不得靜默回空（#189，自 #171 拆出）。

實地事故：daemon 同一 PID 連續運行六天，`log tail-raw` / `log tail-text` /
`wal export` 全部回 ``ok:true`` + 空陣列，而 ``current_seq`` 已累加到 1,261,000——
WAL 目錄被外部工具 rmtree 掉了，服務對此毫無所覺，也不告訴任何人。該 bench 六天的
console 紀錄因此無法回溯，兩輪事故取證落空。

本檔驗證三件事：讀取路徑會誠實回報檔案不見了（``WAL_MISSING``）、回應帶得出
``wal_path`` / ``wal_file_exists`` 讓呼叫端分辨「查得到但沒資料」vs「檔案不見了」、
以及 daemon 會自癒重建被刪掉的 WAL 目錄而非繼續寫進虛空。
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sw_core.wal import WalWriter


class _WalBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.wal_dir = str(Path(self._tmp.name) / "wal")
        self.wal = WalWriter(wal_dir=self.wal_dir)

    def _append(self, payload=b"hello"):
        return self.wal.append(com="COM0", direction="rx", source="test", payload=payload)


class TestWalHealth(_WalBase):
    def test_fresh_wal_is_healthy_with_no_file_yet(self):
        """全新 daemon 還沒有任何 UART 流量：檔案尚未建立不是故障。"""
        health = self.wal.health()
        self.assertEqual(health["wal_dir"], self.wal_dir)
        self.assertEqual(health["wal_path"], self.wal.wal_path)
        self.assertTrue(health["wal_dir_exists"])
        self.assertFalse(health["wal_file_exists"])
        self.assertEqual(health["current_seq"], 0)
        self.assertTrue(health["healthy"])

    def test_health_flags_vanished_wal_after_writes(self):
        """寫過紀錄之後檔案消失＝真故障，必須看得出來。"""
        self._append()
        self.assertTrue(os.path.exists(self.wal.wal_path))
        shutil.rmtree(self.wal_dir)

        health = self.wal.health()
        self.assertFalse(health["wal_dir_exists"])
        self.assertFalse(health["wal_file_exists"])
        self.assertGreater(health["current_seq"], 0)
        self.assertFalse(health["healthy"])

    def test_append_recreates_deleted_wal_dir(self):
        """#189 驗收：daemon 偵測到 WAL 被刪除後重建並告警，而不是安靜地寫進虛空。"""
        self._append()
        shutil.rmtree(self.wal_dir)

        with self.assertLogs("serialwrap", level="WARNING") as captured:
            record = self._append(b"after-rmtree")

        self.assertTrue(os.path.isdir(self.wal_dir))
        self.assertTrue(os.path.exists(self.wal.wal_path))
        self.assertFalse(record["loss_flag"], "自癒成功的 append 不該標 loss")
        self.assertEqual(self.wal.health()["recreated_count"], 1)
        self.assertTrue(self.wal.health()["healthy"])
        self.assertTrue(any("WAL" in line for line in captured.output))

    def test_recreated_wal_is_readable_again(self):
        self._append()
        shutil.rmtree(self.wal_dir)
        self._append(b"after-rmtree")

        rows = self.wal.tail_raw()
        self.assertEqual(len(rows), 1)
        import base64
        self.assertEqual(base64.b64decode(rows[0]["payload_b64"]), b"after-rmtree")

    def test_append_records_failure_when_recreate_fails(self):
        """重建也失敗（ENOSPC/EROFS/權限）時：標 loss、記錄錯誤，但不得讓 RX thread 崩潰。"""
        self._append()
        shutil.rmtree(self.wal_dir)
        with mock.patch("sw_core.wal.os.makedirs", side_effect=OSError("EROFS")):
            with self.assertLogs("serialwrap", level="WARNING"):
                record = self._append(b"doomed")

        self.assertTrue(record["loss_flag"])
        health = self.wal.health()
        self.assertGreaterEqual(health["write_failures"], 1)
        self.assertIsNotNone(health["last_write_error"])
        self.assertFalse(health["healthy"])

    @unittest.skipIf(os.geteuid() == 0, "root 繞過權限檢查，此案在 root 下無意義")
    def test_non_traversable_dir_is_not_writable(self):
        """目錄只有 W_OK 沒有 X_OK 時進不去、建不了檔，不得判為可寫。"""
        self._append()
        os.chmod(self.wal_dir, 0o600)          # rw- but no traverse
        self.addCleanup(os.chmod, self.wal_dir, 0o700)

        health = self.wal.health()
        self.assertFalse(health["wal_dir_writable"])
        self.assertFalse(health["healthy"])

    def test_available_from_seq_tracks_current_file(self):
        for _ in range(3):
            self._append()
        self.assertEqual(self.wal.available_from_seq(), 1)
        self.wal.reset()          # 輪替：現行檔清空、seq 歸零
        self.assertIsNone(self.wal.available_from_seq())
        self._append()
        self.assertEqual(self.wal.available_from_seq(), 1)


class _ServiceBase(_WalBase):
    def _service(self):
        from sw_core.service import SerialwrapService

        with (
            mock.patch("sw_core.service.WalWriter"),
            mock.patch("sw_core.service.DeviceWatcher"),
        ):
            svc = SerialwrapService([])
        svc._wal = self.wal
        return svc


class TestReadPathsReportMissing(_ServiceBase):
    def test_tail_raw_reports_wal_missing(self):
        self._append()
        shutil.rmtree(self.wal_dir)
        resp = self._service().rpc("log.tail_raw", {})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "WAL_MISSING")
        self.assertEqual(resp["wal_path"], self.wal.wal_path)
        self.assertFalse(resp["wal_file_exists"])
        self.assertGreater(resp["current_seq"], 0)

    def test_tail_text_reports_wal_missing(self):
        self._append()
        shutil.rmtree(self.wal_dir)
        resp = self._service().rpc("log.tail_text", {})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "WAL_MISSING")

    def test_wal_range_reports_wal_missing(self):
        """`serialwrap wal export` 走的就是這條——事故當下它回 ok:true + [] + rc=0。"""
        self._append()
        shutil.rmtree(self.wal_dir)
        resp = self._service().rpc("wal.range", {"from_seq": 0, "limit": 100})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "WAL_MISSING")
        self.assertEqual(resp["wal_path"], self.wal.wal_path)

    def test_fresh_daemon_without_traffic_is_not_an_error(self):
        """seq 為 0＝從來沒寫過，檔案不存在是正常的，不得誤報 WAL_MISSING。"""
        svc = self._service()
        for method, key in (("log.tail_raw", "records"), ("log.tail_text", "lines")):
            resp = svc.rpc(method, {})
            self.assertTrue(resp["ok"], method)
            self.assertEqual(resp[key], [], method)
            self.assertFalse(resp["wal_file_exists"], method)
        resp = svc.rpc("wal.range", {"from_seq": 0, "limit": 100})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["records"], [])

    def test_current_seq_comes_from_the_same_snapshot(self):
        """回應內的 current_seq 必須與同一份 wal_health 快照一致（且不再取 WAL 寫鎖）。"""
        for _ in range(3):
            self._append()
        with mock.patch.object(
            type(self.wal), "current_seq",
            new=property(lambda _self: (_ for _ in ()).throw(
                AssertionError("不得經 current_seq property 取 WAL 寫鎖"))),
        ):
            resp = self._service().rpc("log.tail_raw", {})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["current_seq"], 3)

    def test_normal_read_carries_wal_path_and_existence(self):
        """「查得到但沒資料」與「檔案不見了」必須能分辨——正常路徑也要帶這兩個欄位。"""
        self._append()
        resp = self._service().rpc("log.tail_raw", {})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["returned"], 1)
        self.assertEqual(resp["wal_path"], self.wal.wal_path)
        self.assertTrue(resp["wal_file_exists"])

    def test_wal_range_marks_rotated_out_range(self):
        """請求區間落在已輪替掉的範圍：明確標示，不要用空陣列 + ok 帶過。"""
        for _ in range(3):
            self._append()
        self.wal.reset()
        for _ in range(2):
            self._append()
        svc = self._service()

        resp = svc.rpc("wal.range", {"from_seq": 0, "limit": 100})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["available_from_seq"], 1)
        self.assertFalse(resp["rotated_out"])

    def test_wal_range_rotated_out_when_request_precedes_current_file(self):
        for _ in range(3):
            self._append()
        svc = self._service()
        # 人為把現行檔的最小 seq 推高：模擬輪替後只剩較新的紀錄
        rows = [json.loads(line) for line in
                open(self.wal.wal_path, encoding="utf-8").read().splitlines() if line.strip()]
        with open(self.wal.wal_path, "w", encoding="utf-8") as fh:
            for row in rows[-1:]:
                fh.write(json.dumps(row) + "\n")

        resp = svc.rpc("wal.range", {"from_seq": 0, "limit": 100})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["available_from_seq"], 3)
        self.assertTrue(resp["rotated_out"])


class TestHealthStatusExposesWal(_ServiceBase):
    def test_health_status_carries_wal_health(self):
        self._append()
        shutil.rmtree(self.wal_dir)
        resp = self._service().rpc("health.status", {})
        self.assertIn("wal", resp)
        self.assertFalse(resp["wal"]["healthy"])
        self.assertFalse(resp["wal"]["wal_dir_exists"])


class TestDoctorWalWritable(unittest.TestCase):
    """doctor 的 wal_dir 檢查在事故當下回的是 ok:true——它只印路徑、從不檢查存在性。
    新增非 advisory 的 wal_writable 檢查把這一項補上。"""

    def _run_check(self, health_resp):
        from sw_core import doctor_cmd

        with mock.patch("sw_core.client.rpc_call", return_value=health_resp):
            return doctor_cmd._check_wal_writable()

    def test_fails_when_daemon_reports_unhealthy_wal(self):
        result = self._run_check({
            "ok": True,
            "wal_path": "/tmp/serialwrap/wal/raw.wal.ndjson",
            "wal": {
                "wal_dir": "/tmp/serialwrap/wal", "wal_dir_exists": False,
                "wal_file_exists": False, "wal_dir_writable": False,
                "current_seq": 1261000, "write_failures": 3,
                "last_write_error": "ENOENT", "recreated_count": 0, "healthy": False,
            },
        })
        self.assertEqual(result["check"], "wal_writable")
        self.assertFalse(result["ok"])
        self.assertIn("/tmp/serialwrap/wal", result["detail"])
        self.assertTrue(result["fix"])

    def test_passes_when_wal_healthy(self):
        result = self._run_check({
            "ok": True,
            "wal_path": "/tmp/serialwrap/wal/raw.wal.ndjson",
            "wal": {
                "wal_dir": "/tmp/serialwrap/wal", "wal_dir_exists": True,
                "wal_file_exists": True, "wal_dir_writable": True,
                "current_seq": 42, "write_failures": 0,
                "last_write_error": None, "recreated_count": 0, "healthy": True,
            },
        })
        self.assertTrue(result["ok"])

    def test_informational_when_daemon_unreachable(self):
        """doctor 常在啟動 daemon 前執行——連不到不是這項要抓的錯。"""
        result = self._run_check({"ok": False, "error_code": "SOCKET_ERROR"})
        self.assertTrue(result["ok"])

    def test_wal_writable_is_not_advisory(self):
        """稽核紀錄整個消失必須拉低 doctor 整體 ok，不能只是 WARN。"""
        from sw_core.doctor_cmd import DOCTOR_ADVISORY_CHECKS, DOCTOR_ADVISORY_CHECKS_WIN

        self.assertNotIn("wal_writable", DOCTOR_ADVISORY_CHECKS)
        self.assertNotIn("wal_writable", DOCTOR_ADVISORY_CHECKS_WIN)

    def test_run_doctor_includes_wal_writable(self):
        from sw_core.doctor_cmd import run_doctor

        with mock.patch("sw_core.client.rpc_call", return_value={"ok": False}):
            names = {row["check"] for row in run_doctor()}
        self.assertIn("wal_writable", names)


if __name__ == "__main__":
    unittest.main()
