import base64
import tempfile
import unittest
from pathlib import Path

from sw_core.wal import WalWriter


class TestWal(unittest.TestCase):
    def test_append_and_tail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wal = WalWriter(wal_dir=td, rotate_bytes=10_000_000)
            rec = wal.append(com="COM0", direction="TX", source="agent:a", payload=b"echo hi\n", cmd_id="x1")
            self.assertEqual(rec["seq"], 1)
            rows = wal.tail_raw(from_seq=0, com="COM0", limit=10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["cmd_id"], "x1")
            self.assertEqual(base64.b64decode(rows[0]["payload_b64"]), b"echo hi\n")

            lines = wal.tail_text(from_seq=0, com="COM0", limit=10)
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0], "echo hi")
            self.assertTrue(Path(td, "raw.mirror.log").exists())


class TestWalTailLatest(unittest.TestCase):
    """#124：tail_raw/tail_text 預設（from_seq 省略）改為 latest 模式回傳最新 N 筆。"""

    def _make_wal_with_records(self, td: str, count: int = 100) -> WalWriter:
        wal = WalWriter(wal_dir=td, rotate_bytes=10_000_000)
        for i in range(1, count + 1):
            wal.append(com="COM0", direction="RX", source="uart", payload=f"line {i}\n".encode())
        return wal

    def test_tail_raw_latest_mode_returns_newest(self) -> None:
        """from_seq 省略（None）→ latest 模式：100 筆時 limit=10 回 seq 91-100（升冪）。"""
        with tempfile.TemporaryDirectory() as td:
            wal = self._make_wal_with_records(td)
            rows = wal.tail_raw(limit=10)
            self.assertEqual([r["seq"] for r in rows], list(range(91, 101)))

    def test_tail_raw_range_mode_unchanged(self) -> None:
        """顯式 from_seq=0 → 舊 range 語意：回 seq 1-10（起點後最舊前 N 筆）。"""
        with tempfile.TemporaryDirectory() as td:
            wal = self._make_wal_with_records(td)
            rows = wal.tail_raw(from_seq=0, limit=10)
            self.assertEqual([r["seq"] for r in rows], list(range(1, 11)))

    def test_tail_raw_with_meta_truncated_semantics(self) -> None:
        """truncated：latest 模式＝視窗前還有更舊符合紀錄；range 模式＝視窗後還有更新符合紀錄。"""
        with tempfile.TemporaryDirectory() as td:
            wal = self._make_wal_with_records(td)
            rows, truncated = wal.tail_raw_with_meta(limit=10)
            self.assertEqual(len(rows), 10)
            self.assertTrue(truncated)
            rows, truncated = wal.tail_raw_with_meta(from_seq=0, limit=10)
            self.assertEqual(len(rows), 10)
            self.assertTrue(truncated)
            # limit 足夠涵蓋全部 → 兩種模式皆不截斷
            rows, truncated = wal.tail_raw_with_meta(limit=200)
            self.assertEqual(len(rows), 100)
            self.assertFalse(truncated)
            rows, truncated = wal.tail_raw_with_meta(from_seq=0, limit=200)
            self.assertEqual(len(rows), 100)
            self.assertFalse(truncated)
            # range 模式恰好收滿且無後續符合 → 不截斷
            rows, truncated = wal.tail_raw_with_meta(from_seq=90, limit=10)
            self.assertEqual([r["seq"] for r in rows], list(range(91, 101)))
            self.assertFalse(truncated)

    def test_tail_raw_with_meta_com_filter(self) -> None:
        """latest 模式的 com 過濾與 truncated 只計符合紀錄。"""
        with tempfile.TemporaryDirectory() as td:
            wal = WalWriter(wal_dir=td, rotate_bytes=10_000_000)
            for i in range(1, 7):
                com = "COM0" if i % 2 else "COM1"
                wal.append(com=com, direction="RX", source="uart", payload=f"line {i}\n".encode())
            rows, truncated = wal.tail_raw_with_meta(com="COM0", limit=2)
            self.assertEqual([r["seq"] for r in rows], [3, 5])
            self.assertTrue(truncated)
            rows, truncated = wal.tail_raw_with_meta(com="COM0", limit=10)
            self.assertEqual([r["seq"] for r in rows], [1, 3, 5])
            self.assertFalse(truncated)

    def test_tail_raw_with_meta_range_mode_com_filter(self) -> None:
        """range 模式 + com 過濾：起點與 truncated 皆只計符合 com 的紀錄。"""
        with tempfile.TemporaryDirectory() as td:
            wal = WalWriter(wal_dir=td, rotate_bytes=10_000_000)
            for i in range(1, 7):
                com = "COM0" if i % 2 else "COM1"
                wal.append(com=com, direction="RX", source="uart", payload=f"line {i}\n".encode())
            # COM0 佔 seq 1/3/5；from_seq=1 → 自 seq>1 起的 COM0 紀錄
            rows, truncated = wal.tail_raw_with_meta(from_seq=1, com="COM0", limit=1)
            self.assertEqual([r["seq"] for r in rows], [3])
            self.assertTrue(truncated)  # seq 5 仍在視窗之後
            rows, truncated = wal.tail_raw_with_meta(from_seq=1, com="COM0", limit=10)
            self.assertEqual([r["seq"] for r in rows], [3, 5])
            self.assertFalse(truncated)

    def test_tail_raw_latest_scope_is_current_file_after_rotation(self) -> None:
        """rotation 邊界契約（#124 review）：latest 模式僅涵蓋現行 WAL 檔。

        輪替後更舊紀錄保存在 raw.wal.ndjson.<ts> 歸檔檔、不列入掃描與 truncated 判定：
        latest limit=10 回現行檔**全部**筆數（可少於 limit）且 truncated=False。
        本測試把現狀語意釘死成契約；若未來改為回讀歸檔檔，需同步改文件與本測試。
        """
        with tempfile.TemporaryDirectory() as td:
            # rotate_bytes 調小（每筆 record 約 250 bytes）→ 寫 30 筆期間必然發生多次輪替，
            # 且現行檔累積筆數遠小於 10。
            wal = WalWriter(wal_dir=td, rotate_bytes=600)
            total = 30
            for i in range(1, total + 1):
                wal.append(com="COM0", direction="RX", source="uart", payload=f"line {i}\n".encode())
            rotated = [p for p in Path(td).iterdir() if p.name.startswith("raw.wal.ndjson.")]
            self.assertTrue(rotated, "測試前提：必須至少發生一次 WAL 輪替")
            # 現行檔實際筆數 K（1 <= K < 10，保證 limit=10 足以涵蓋現行檔全部）
            current_rows = wal.tail_raw(from_seq=0, limit=1000)
            k = len(current_rows)
            self.assertGreaterEqual(k, 1)
            self.assertLess(k, 10, "測試前提：現行檔筆數須小於 limit（調整 rotate_bytes）")
            rows, truncated = wal.tail_raw_with_meta(limit=10)
            # 回現行檔全部 K 筆（seq 尾端連續），不足 limit 也不回溯歸檔檔
            self.assertEqual(len(rows), k)
            self.assertEqual([r["seq"] for r in rows], list(range(total - k + 1, total + 1)))
            # 歸檔檔裡有更舊紀錄，但 truncated 僅以現行檔為範圍 → False
            self.assertFalse(truncated)

    def test_tail_text_latest_mode_includes_partial_prompt(self) -> None:
        """latest 模式下，無換行結尾的 partial prompt 必須出現在結果尾行（issue #124 動機）。"""
        with tempfile.TemporaryDirectory() as td:
            wal = self._make_wal_with_records(td)
            wal.append(com="COM0", direction="RX", source="uart", payload=b"root@host:~# ")
            lines = wal.tail_text(limit=10)
            self.assertEqual(lines[-1], "root@host:~# ")
            self.assertIn("line 100", lines)

    def test_tail_text_range_mode_unchanged(self) -> None:
        """顯式 from_seq=0 → tail_text 維持舊 range 語意（最舊起算）。"""
        with tempfile.TemporaryDirectory() as td:
            wal = self._make_wal_with_records(td)
            lines = wal.tail_text(from_seq=0, limit=10)
            self.assertEqual(lines[0], "line 1")
            self.assertEqual(lines[-1], "line 10")


if __name__ == "__main__":
    unittest.main()
