from __future__ import annotations

import errno
import fcntl
import os
import socket


class SingletonLock:
    def __init__(self, lock_path: str, socket_path: str) -> None:
        self.lock_path = lock_path
        self.socket_path = socket_path
        self._fd: int | None = None

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeError("DAEMON_ALREADY_RUNNING")

        if os.path.exists(self.socket_path):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(0.2)
                sock.connect(self.socket_path)
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                    try:
                        os.unlink(self.socket_path)
                    except OSError:
                        pass
                else:
                    os.close(fd)
                    raise RuntimeError("SOCKET_UNAVAILABLE")
            else:
                os.close(fd)
                raise RuntimeError("DAEMON_ALREADY_RUNNING")
            finally:
                sock.close()

        self._fd = fd
        os.ftruncate(self._fd, 0)
        os.write(self._fd, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(self._fd)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
