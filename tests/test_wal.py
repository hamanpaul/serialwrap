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


if __name__ == "__main__":
    unittest.main()
