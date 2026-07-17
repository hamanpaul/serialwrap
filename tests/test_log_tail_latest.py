"""Issue #124：log.tail_raw / log.tail_text 預設 latest 模式與 metadata 欄位。

- RPC params 未帶 ``from_seq`` key → latest 模式（回傳最新 N 筆）。
- 顯式帶 ``from_seq``（含 0）→ 舊 range 增量語意（老 client 相容）。
- 回應附五個 metadata 欄位：``from_seq`` / ``last_seq`` / ``current_seq`` /
  ``returned`` / ``truncated``。
- legacy ``result.tail``（無 cmd_id 的 WAL fallback）行為完全不變、不加 metadata。
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from sw_core.wal import WalWriter

META_KEYS = {"from_seq", "last_seq", "current_seq", "returned", "truncated"}


def _make_service(wal_dir: str) -> "SerialwrapService":
    """建立最小化 SerialwrapService：mock 掉 I/O 元件、換上真實 WalWriter。"""
    from sw_core.config import SessionProfile

    profiles: list[SessionProfile] = []
    with (
        patch("sw_core.service.WalWriter"),
        patch("sw_core.service.SessionManager"),
        patch("sw_core.service.DeviceWatcher"),
    ):
        from sw_core.service import SerialwrapService

        svc = SerialwrapService(profiles)
    svc._wal = WalWriter(wal_dir=wal_dir, rotate_bytes=10_000_000)
    return svc


class TestLogTailLatestRpc(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.svc = _make_service(self._td.name)
        for i in range(1, 31):
            self.svc._wal.append(
                com="COM0", direction="RX", source="uart", payload=f"line {i}\n".encode()
            )

    # ------------------------------------------------------------------
    # log.tail_raw
    # ------------------------------------------------------------------

    def test_tail_raw_default_latest_with_metadata(self) -> None:
        """未帶 from_seq key → latest 模式：最新 10 筆＋完整 metadata。"""
        resp = self.svc.rpc("log.tail_raw", {"limit": 10})
        self.assertTrue(resp["ok"])
        self.assertEqual([r["seq"] for r in resp["records"]], list(range(21, 31)))
        self.assertTrue(META_KEYS.issubset(resp.keys()))
        self.assertIsNone(resp["from_seq"])
        self.assertEqual(resp["last_seq"], 30)
        self.assertEqual(resp["current_seq"], 30)
        self.assertEqual(resp["returned"], 10)
        self.assertTrue(resp["truncated"])

    def test_tail_raw_explicit_from_seq_zero_keeps_range(self) -> None:
        """顯式 from_seq=0 → 舊 range 語意（seq 1-10），metadata 反映實際使用值。"""
        resp = self.svc.rpc("log.tail_raw", {"from_seq": 0, "limit": 10})
        self.assertTrue(resp["ok"])
        self.assertEqual([r["seq"] for r in resp["records"]], list(range(1, 11)))
        self.assertEqual(resp["from_seq"], 0)
        self.assertEqual(resp["last_seq"], 10)
        self.assertEqual(resp["current_seq"], 30)
        self.assertEqual(resp["returned"], 10)
        self.assertTrue(resp["truncated"])

    def test_tail_raw_from_seq_json_null_means_latest(self) -> None:
        """設計決策（#124 review）：JSON 顯式 `null` 視同「未帶 key」→ latest 模式。

        舊碼 `int(params.get("from_seq") or 0)` 會把 null 吃成 0（range 模式），
        屬意外行為、不予保留；要 range 語意必須帶 int（含 0）。
        """
        resp = self.svc.rpc("log.tail_raw", {"from_seq": None, "limit": 10})
        self.assertTrue(resp["ok"])
        self.assertEqual([r["seq"] for r in resp["records"]], list(range(21, 31)))
        self.assertIsNone(resp["from_seq"])

    def test_tail_raw_from_seq_invalid_returns_invalid_args(self) -> None:
        """非法 from_seq（""、"abc"）→ 明確回 INVALID_ARGS，例外不穿越 RPC 邊界。"""
        for bad in ("", "abc"):
            with self.subTest(from_seq=bad):
                resp = self.svc.rpc("log.tail_raw", {"from_seq": bad, "limit": 10})
                self.assertFalse(resp["ok"])
                self.assertEqual(resp["error_code"], "INVALID_ARGS")

    def test_tail_text_from_seq_invalid_returns_invalid_args(self) -> None:
        resp = self.svc.rpc("log.tail_text", {"from_seq": "", "limit": 10})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "INVALID_ARGS")

    def test_tail_raw_missing_wal_file_metadata_shape(self) -> None:
        """WAL 檔不存在（daemon 尚未寫入任何紀錄）時的 metadata 形狀（#124 review）。

        釘死實際值：records=[]、returned=0、last_seq=None、current_seq=0、truncated=False。
        """
        with tempfile.TemporaryDirectory() as td:
            svc = _make_service(td)  # 不 append → raw.wal.ndjson 尚未建立
            resp = svc.rpc("log.tail_raw", {"limit": 10})
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["records"], [])
            self.assertEqual(resp["returned"], 0)
            self.assertIsNone(resp["from_seq"])
            self.assertIsNone(resp["last_seq"])
            self.assertEqual(resp["current_seq"], 0)
            self.assertFalse(resp["truncated"])

    def test_tail_raw_not_truncated_when_limit_covers_all(self) -> None:
        resp = self.svc.rpc("log.tail_raw", {"limit": 100})
        self.assertEqual(resp["returned"], 30)
        self.assertFalse(resp["truncated"])
        resp = self.svc.rpc("log.tail_raw", {"from_seq": 0, "limit": 100})
        self.assertEqual(resp["returned"], 30)
        self.assertFalse(resp["truncated"])

    def test_tail_raw_empty_result_last_seq_null(self) -> None:
        """無符合紀錄時 last_seq 為 None、returned 為 0。"""
        resp = self.svc.rpc("log.tail_raw", {"from_seq": 999, "limit": 10})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["records"], [])
        self.assertIsNone(resp["last_seq"])
        self.assertEqual(resp["returned"], 0)
        self.assertFalse(resp["truncated"])

    # ------------------------------------------------------------------
    # log.tail_text
    # ------------------------------------------------------------------

    def test_tail_text_default_latest_with_metadata(self) -> None:
        """未帶 from_seq key → latest 模式：最新 10 筆的文字行＋完整 metadata。"""
        resp = self.svc.rpc("log.tail_text", {"limit": 10})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["lines"][-1], "line 30")
        self.assertNotIn("line 1", resp["lines"])
        self.assertTrue(META_KEYS.issubset(resp.keys()))
        self.assertIsNone(resp["from_seq"])
        self.assertEqual(resp["last_seq"], 30)
        self.assertEqual(resp["current_seq"], 30)
        self.assertEqual(resp["returned"], len(resp["lines"]))
        self.assertTrue(resp["truncated"])

    def test_tail_text_explicit_from_seq_zero_keeps_range(self) -> None:
        resp = self.svc.rpc("log.tail_text", {"from_seq": 0, "limit": 10})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["lines"][0], "line 1")
        self.assertEqual(resp["lines"][-1], "line 10")
        self.assertEqual(resp["from_seq"], 0)
        self.assertEqual(resp["last_seq"], 10)

    def test_tail_text_latest_includes_partial_prompt(self) -> None:
        """latest 模式下無換行的 partial prompt 出現在尾行（issue #124 動機）。"""
        self.svc._wal.append(com="COM0", direction="RX", source="uart", payload=b"root@host:~# ")
        resp = self.svc.rpc("log.tail_text", {"limit": 5})
        self.assertEqual(resp["lines"][-1], "root@host:~# ")

    # ------------------------------------------------------------------
    # legacy result.tail（WAL fallback）不動
    # ------------------------------------------------------------------

    def test_legacy_result_tail_wal_fallback_unchanged(self) -> None:
        """result.tail 無 cmd_id 的 WAL fallback：維持 from_seq 增量語意、不加 metadata。"""
        resp = self.svc.rpc("result.tail", {"limit": 10})
        self.assertTrue(resp["ok"])
        # 未帶 from_seq 視同 0（舊語意：最舊起算），非 latest 模式
        self.assertEqual([r["seq"] for r in resp["records"]], list(range(1, 11)))
        for key in META_KEYS:
            self.assertNotIn(key, resp)

    def test_legacy_result_tail_wal_fallback_explicit_from_seq(self) -> None:
        resp = self.svc.rpc("result.tail", {"from_seq": 20, "limit": 10})
        self.assertTrue(resp["ok"])
        self.assertEqual([r["seq"] for r in resp["records"]], list(range(21, 31)))


if __name__ == "__main__":
    unittest.main()
