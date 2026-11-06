import os
import tempfile
import unittest
from pathlib import Path

import pytest

# POSIX-only：daemon_lock 經 lock_posix.py:4 頂層 import fcntl；非 POSIX 整檔略過、不 import。
if os.name != "posix":
    pytest.skip("SingletonLock POSIX 後端依賴 fcntl（lock_posix.py:4）", allow_module_level=True)

from sw_core.daemon_lock import SingletonLock  # noqa: E402

pytestmark = [pytest.mark.posix_only, pytest.mark.memory_only]


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
