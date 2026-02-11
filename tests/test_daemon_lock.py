import tempfile
import unittest
from pathlib import Path

from sw_core.daemon_lock import SingletonLock


class TestDaemonLock(unittest.TestCase):
    def test_singleton_lock_pid_written(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = str(Path(td) / "serialwrapd.lock")
            socket_path = str(Path(td) / "serialwrapd.sock")
            lk = SingletonLock(lock_path, socket_path)
            lk.acquire()
            text = Path(lock_path).read_text(encoding="utf-8").strip()
            self.assertTrue(text.isdigit())
            lk.release()


if __name__ == "__main__":
    unittest.main()
