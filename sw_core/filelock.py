"""提供 POSIX 與 Windows 共用的跨行程獨占檔案鎖。"""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def exclusive_file_lock(path: str, *, blocking: bool = True) -> Iterator[None]:
    """以不刪除的鎖檔序列化跨行程操作。"""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            from .win32_files import lock_file_ex, unlock_file_ex

            if not lock_file_ex(fd, exclusive=True, fail_immediately=not blocking):
                raise BlockingIOError(errno.EAGAIN, "檔案已由其他行程鎖定", path)
            try:
                yield
            finally:
                unlock_file_ex(fd)
        else:
            import fcntl

            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(fd, flags)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
