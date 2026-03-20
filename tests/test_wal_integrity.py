"""WAL 完整性與復原測試。

涵蓋：rotate 歸檔、seq 連續性、損壞行跳過、損壞後 append、雙檔同寫。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from sw_core.wal import WalWriter


def _append_n(wal: WalWriter, n: int, *, com: str = "COM0") -> list[dict]:
    """快速寫入 n 筆記錄並回傳。"""
    records = []
    for i in range(n):
        rec = wal.append(
            com=com, direction="TX", source="test",
            payload=f"cmd-{i}\n".encode(), cmd_id=f"c{i}",
        )
        records.append(rec)
    return records


class TestWalIntegrity(unittest.TestCase):

    def test_wal_rotate_creates_archive(self) -> None:
        """寫超過 rotate_bytes 後，舊檔應被改名歸檔，新檔繼續寫入。"""
        with tempfile.TemporaryDirectory() as td:
            # 設定極小的 rotate 門檻
            wal = WalWriter(wal_dir=td, rotate_bytes=500)
            # 寫入足夠資料觸發 rotate
            for i in range(30):
                wal.append(com="COM0", direction="TX", source="test",
                           payload=f"line-{i:04d}\n".encode(), cmd_id=f"r{i}")

            # 應該有至少一個歸檔檔案
            archive_files = [
                f for f in os.listdir(td)
                if f.startswith("raw.wal.ndjson.") and len(f) > len("raw.wal.ndjson")
            ]
            self.assertGreaterEqual(len(archive_files), 1,
                                    f"應有 rotate 歸檔，實際: {os.listdir(td)}")
            # 主檔仍存在
            self.assertTrue(os.path.exists(wal.wal_path))

    def test_wal_seq_continuity_after_rotate(self) -> None:
        """rotate 後 seq 應持續遞增，不會重置。"""
        with tempfile.TemporaryDirectory() as td:
            wal = WalWriter(wal_dir=td, rotate_bytes=300)
            records = _append_n(wal, 20)
            seqs = [r["seq"] for r in records]
            # seq 應為 1..20 連續遞增
            self.assertEqual(seqs, list(range(1, 21)))
            # 再次建立 WalWriter 讀取 last_seq
            wal2 = WalWriter(wal_dir=td, rotate_bytes=300)
            rec = wal2.append(com="COM0", direction="TX", source="test",
                              payload=b"after-rotate\n", cmd_id="ar")
            # 新 WalWriter 只讀主檔，但主檔一定保留最後一筆 seq=20，
            # 因此下一筆 append 應為 21，不能重置為 1。
            self.assertEqual(rec["seq"], 21)

    def test_wal_load_last_seq_skips_corrupt_lines(self) -> None:
        """WAL 檔中間有亂碼行時，_load_last_seq 應跳過並繼續讀取。"""
        with tempfile.TemporaryDirectory() as td:
            wal = WalWriter(wal_dir=td, rotate_bytes=10_000_000)
            _append_n(wal, 5)  # seq 1..5

            # 手動插入亂碼行
            with open(wal.wal_path, "a", encoding="utf-8") as fp:
                fp.write("THIS IS GARBAGE LINE\n")
                fp.write("{invalid json\n")
                fp.write('{"seq": "not-an-int"}\n')

            # 再寫一筆正常的
            rec = wal.append(com="COM0", direction="TX", source="test",
                             payload=b"after-corrupt\n", cmd_id="ac")
            self.assertEqual(rec["seq"], 6)

            # 重新載入，應正確找到 seq=6
            wal2 = WalWriter(wal_dir=td, rotate_bytes=10_000_000)
            self.assertEqual(wal2.current_seq, 6)

    def test_wal_append_after_corrupt_recovery(self) -> None:
        """損壞恢復後仍可正常 append，且後續 seq 連續。"""
        with tempfile.TemporaryDirectory() as td:
            # 先寫 3 筆
            wal = WalWriter(wal_dir=td, rotate_bytes=10_000_000)
            _append_n(wal, 3)

            # 模擬 daemon crash：直接在檔案尾端寫入不完整 JSON
            with open(wal.wal_path, "a", encoding="utf-8") as fp:
                fp.write('{"seq": 4, "partial": true\n')  # 缺少結尾 }

            # 重新啟動 WalWriter（模擬 daemon 重啟）
            wal2 = WalWriter(wal_dir=td, rotate_bytes=10_000_000)
            # 應該找到 seq=3（損壞行被跳過）
            self.assertEqual(wal2.current_seq, 3)

            # 繼續 append 應從 seq=4 開始
            records = _append_n(wal2, 3)
            self.assertEqual([r["seq"] for r in records], [4, 5, 6])

            # tail_raw 應能讀到所有有效記錄（跳過損壞行）
            rows = wal2.tail_raw(from_seq=0, limit=100)
            valid_seqs = [r["seq"] for r in rows]
            self.assertEqual(valid_seqs, [1, 2, 3, 4, 5, 6])

    def test_wal_mirror_and_wal_both_written(self) -> None:
        """每次 append 應同時寫入 .wal.ndjson 和 .mirror.log。"""
        with tempfile.TemporaryDirectory() as td:
            wal = WalWriter(wal_dir=td, rotate_bytes=10_000_000)
            payloads = [b"hello\n", b"world\n", b"test\n"]
            for i, p in enumerate(payloads):
                wal.append(com="COM0", direction="RX", source="uart",
                           payload=p, cmd_id=f"m{i}")

            # WAL 檔應有 3 行有效 JSON
            with open(wal.wal_path, "r", encoding="utf-8") as fp:
                wal_lines = [l for l in fp if l.strip()]
            self.assertEqual(len(wal_lines), 3)
            for line in wal_lines:
                obj = json.loads(line)
                self.assertIn("seq", obj)
                self.assertIn("payload_b64", obj)

            # Mirror 檔應有可讀文字
            with open(wal.mirror_path, "r", encoding="utf-8") as fp:
                mirror_content = fp.read()
            self.assertIn("hello", mirror_content)
            self.assertIn("world", mirror_content)
            self.assertIn("test", mirror_content)


if __name__ == "__main__":
    unittest.main()
