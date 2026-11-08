"""跨平台的人類 console 前端，供 realhw 與 regression 共用。"""
from __future__ import annotations

import ipaddress
import os
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from .drivers import strip_ansi
from .telnet_client import TelnetClientCodec

KEYMAP_TELNET: dict[str, bytes] = {
    "Enter": b"\r\n",
    "Tab": b"\t",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "C-c": b"\x03",
    "C-u": b"\x15",
}


class FrontendError(RuntimeError):
    """human frontend 無法建立或使用時回報。"""


class HumanFrontend(Protocol):
    kind: str

    def open(self, com: str) -> None: ...
    def alive(self) -> bool: ...
    def send_line(self, text: str) -> None: ...
    def send_text(self, text: str) -> None: ...
    def send_key(self, key: str) -> None: ...
    def capture(self) -> str: ...
    def client_id(self) -> str | None: ...
    def close(self) -> None: ...
    def crash(self) -> int | None: ...


class _TmuxFrontend:
    kind = "tmux-reader"

    def __init__(self, tmux: Any, tag: str) -> None:
        self._tmux = tmux
        self._tag = tag
        self._session: str | None = None

    def _new_session(self) -> str:
        name = self._tmux.name(self._tag)
        self._tmux.new(name, self._command())
        self._session = name
        return name

    def _command(self) -> str:
        raise NotImplementedError

    def _require_open(self) -> str:
        if self._session is None:
            raise FrontendError("human frontend 尚未 open")
        return self._session

    def alive(self) -> bool:
        if self._session is None:
            return False
        probe = getattr(self._tmux, "alive", None)
        if callable(probe):
            try:
                return bool(probe(self._session))
            except Exception:
                return False
        try:
            return bool((self._tmux.capture(self._session) or "").strip())
        except Exception:
            return False

    def send_line(self, text: str) -> None:
        self._tmux.send(self._require_open(), text)

    def send_text(self, text: str) -> None:
        self._tmux.send(self._require_open(), text, enter=False)

    def send_key(self, key: str) -> None:
        if key not in KEYMAP_TELNET:
            raise FrontendError(f"不支援的按鍵：{key}")
        self._tmux.send_key(self._require_open(), key)

    def capture(self) -> str:
        return strip_ansi(self._tmux.capture(self._require_open()) or "")

    def client_id(self) -> str | None:
        return None

    def _kill_pane(self) -> int:
        if self._session is None:
            raise FrontendError("human frontend 尚未 open，無法確認 minicom process")
        getter = getattr(self._tmux, "pane_pid", None)
        try:
            raw_pid = getter(self._session) if callable(getter) else ""
            pane_pid = int(str(raw_pid).strip())
        except (TypeError, ValueError, OSError) as exc:
            raise FrontendError("無法取得 tmux pane PID，未終止任何 process") from exc
        try:
            process_list = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,args="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise FrontendError(f"無法列出 pane 子行程，未終止任何 process：{exc}") from exc
        if process_list.returncode != 0:
            detail = (process_list.stderr or "ps failed").strip()
            raise FrontendError(f"無法列出 pane 子行程，未終止任何 process：{detail}")

        rows: dict[int, tuple[int, str]] = {}
        for line in process_list.stdout.splitlines():
            fields = line.strip().split(maxsplit=2)
            if len(fields) != 3:
                continue
            try:
                child_pid, parent_pid = int(fields[0]), int(fields[1])
            except ValueError:
                continue
            rows[child_pid] = (parent_pid, fields[2])

        descendants = {pane_pid}
        while True:
            found = {pid for pid, (parent, _args) in rows.items() if parent in descendants}
            if found <= descendants:
                break
            descendants.update(found)
        minicom_pids = []
        for pid in descendants - {pane_pid}:
            _parent, args = rows.get(pid, (0, ""))
            executable = args.split(maxsplit=1)[0].strip('"\'') if args else ""
            if os.path.basename(executable).casefold() == "minicom":
                minicom_pids.append(pid)
        if len(minicom_pids) != 1:
            raise FrontendError(
                f"無法唯一確認 pane 內 minicom process（找到 {len(minicom_pids)} 個），未終止任何 process"
            )
        pid = minicom_pids[0]
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError as exc:
            raise FrontendError(f"無法終止已確認的 minicom PID {pid}：{exc}") from exc
        return pid

    def close(self) -> None:
        if self._session is not None:
            session, self._session = self._session, None
            self._tmux.kill(session)

    def crash(self) -> int:
        return self._kill_pane()


class TmuxMinicomFrontend(_TmuxFrontend):
    """既有 Linux console-attach router＋minicom 前端。"""

    kind = "tmux-minicom"

    def _command(self) -> str:
        return "serialwrap-minicom " + self._com

    def open(self, com: str) -> None:
        self._com = com
        self._new_session()


class TmuxReaderFrontend(_TmuxFrontend):
    """透過 console-attach 取得 vtty，再以 tmux cat 讀寫的前端。"""

    kind = "tmux-reader"

    def __init__(self, tmux: Any, sw: Any, tag: str, *, label: str = "realhw") -> None:
        super().__init__(tmux, tag)
        self._sw = sw
        self._label = label
        self._com: str | None = None
        self._client: str | None = None

    def _command(self) -> str:
        raise FrontendError("reader 前端需先取得 vtty")

    def open(self, com: str) -> None:
        response = self._sw.run("session", "console-attach", "--selector", com,
                                "--label", self._label)
        endpoint = response.get("vtty")
        if not response.get("ok") or not isinstance(endpoint, str) or not endpoint:
            raise FrontendError(f"console-attach 失敗：{response}")
        import shlex

        self._com = com
        self._client = response.get("client_id")
        name = self._tmux.name(self._tag)
        self._tmux.new(name, "cat " + shlex.quote(endpoint))
        self._session = name

    def client_id(self) -> str | None:
        return self._client

    def close(self) -> None:
        super().close()
        if self._com and self._client:
            try:
                self._sw.run("session", "console-detach", "--selector", self._com,
                             "--client-id", str(self._client))
            except Exception:
                pass
        self._com = None
        self._client = None


class TelnetFrontend:
    """透過 Windows console-attach 回傳的 loopback Telnet endpoint 互動。"""

    kind = "telnet"

    def __init__(self, sw: Any, tag: str, *, label: str = "realhw",
                 connect_timeout_s: float = 5.0,
                 buffer_limit_bytes: int = 1_048_576) -> None:
        self._sw = sw
        self._tag = tag
        self._label = label
        self._connect_timeout_s = max(0.1, float(connect_timeout_s))
        self._buffer_limit_bytes = max(1, int(buffer_limit_bytes))
        self._sock: socket.socket | None = None
        self._client_id: str | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._disconnected = threading.Event()
        self._send_lock = threading.Lock()
        self._buffer_lock = threading.Lock()
        self._buffer = bytearray()
        self._codec = TelnetClientCodec()

    def open(self, com: str) -> None:
        if self._sock is not None:
            raise FrontendError("Telnet frontend 已開啟")
        response = self._sw.run("session", "console-attach", "--selector", com,
                                "--label", f"{self._label}-{self._tag}")
        if not response.get("ok"):
            raise FrontendError(f"console-attach 失敗：{response}")
        if response.get("transport") != "tcp" or response.get("protocol") != "telnet":
            raise FrontendError("Telnet frontend 需要 transport=tcp、protocol=telnet")
        endpoint = response.get("endpoint") or response.get("vtty")
        host, port = self._parse_endpoint(endpoint)
        try:
            sock = socket.create_connection((host, port), timeout=self._connect_timeout_s)
            sock.settimeout(0.2)
        except OSError as exc:
            raise FrontendError(f"Telnet endpoint 連線失敗：{exc}") from exc
        self._sock = sock
        self._client_id = response.get("client_id")
        self._stop.clear()
        self._disconnected.clear()
        self._reader = threading.Thread(target=self._read_loop, name=f"telnet-{self._tag}",
                                        daemon=True)
        self._reader.start()

    @staticmethod
    def _parse_endpoint(endpoint: Any) -> tuple[str, int]:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise FrontendError("console-attach 未提供 TCP endpoint")
        value = endpoint.strip()
        parsed = urlsplit(value if "://" in value else "tcp://" + value)
        host = parsed.hostname
        port = parsed.port
        if not host or port is None:
            raise FrontendError(f"Telnet endpoint 格式錯誤：{endpoint!r}")
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if not loopback:
            raise FrontendError("Telnet frontend 只允許 loopback endpoint")
        return host, port

    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                payload, replies = self._codec.feed(chunk)
                if replies:
                    self._send_protocol(replies)
                if payload:
                    with self._buffer_lock:
                        self._buffer.extend(payload)
                        if len(self._buffer) > self._buffer_limit_bytes:
                            del self._buffer[:-self._buffer_limit_bytes]
        finally:
            self._disconnected.set()

    def _send_protocol(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            with self._send_lock:
                sock.sendall(data)
        except OSError:
            self._disconnected.set()

    def _send_bytes(self, data: bytes) -> None:
        sock = self._sock
        if sock is None or self._disconnected.is_set():
            raise FrontendError("Telnet frontend 未連線")
        try:
            with self._send_lock:
                sock.sendall(TelnetClientCodec.encode(data))
        except OSError as exc:
            self._disconnected.set()
            raise FrontendError(f"Telnet 寫入失敗：{exc}") from exc

    def alive(self) -> bool:
        return self._sock is not None and not self._stop.is_set() and not self._disconnected.is_set()

    def send_line(self, text: str) -> None:
        self.send_text(text + "\r\n")

    def send_text(self, text: str) -> None:
        self._send_bytes(str(text).encode("utf-8"))

    def send_key(self, key: str) -> None:
        try:
            data = KEYMAP_TELNET[key]
        except KeyError as exc:
            raise FrontendError(f"不支援的按鍵：{key}") from exc
        self._send_bytes(data)

    def capture(self) -> str:
        with self._buffer_lock:
            data = bytes(self._buffer)
        return strip_ansi(data.decode("utf-8", errors="replace"))

    def client_id(self) -> str | None:
        return self._client_id

    def _close_socket(self, *, crash: bool) -> None:
        sock, self._sock = self._sock, None
        self._stop.set()
        if sock is None:
            return
        try:
            if crash:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.close()
        except OSError:
            pass
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        self._reader = None

    def close(self) -> None:
        self._close_socket(crash=False)

    def crash(self) -> None:
        self._close_socket(crash=True)


def select_frontend_kind(cfg: dict[str, Any], *, os_name: str | None = None) -> str:
    """依組態選 frontend；auto 在 Windows 使用 Telnet，其餘使用 tmux。"""
    requested = str(cfg.get("human_frontend") or "auto").strip().lower()
    if requested not in {"auto", "tmux", "telnet"}:
        raise FrontendError(f"未知 human_frontend：{requested}")
    if requested != "auto":
        return requested
    platform_name = os.name if os_name is None else os_name
    return "telnet" if platform_name == "nt" or platform_name.startswith("win") else "tmux"


def make_frontend_factory(cfg: dict[str, Any], *, tmux: Any, sw: Any,
                          os_name: str | None = None) -> Callable[[str, str], HumanFrontend]:
    """建立依 tag 與角色產生 frontend 的 factory。"""
    kind = select_frontend_kind(cfg, os_name=os_name)

    def factory(tag: str, role: str) -> HumanFrontend:
        if role not in {"minicom", "reader"}:
            raise FrontendError(f"未知 human frontend 角色：{role}")
        if kind == "telnet":
            return TelnetFrontend(sw, tag, label=str(cfg.get("human_frontend_label") or "realhw"))
        if role == "minicom":
            return TmuxMinicomFrontend(tmux, tag)
        return TmuxReaderFrontend(tmux, sw, tag,
                                  label=str(cfg.get("human_frontend_label") or "realhw"))

    return factory


def frontend_capability(cfg: dict[str, Any], *, os_name: str | None = None,
                        which: Callable[[str], str | None] = shutil.which) -> tuple[bool, str]:
    """回報選定 frontend 是否可用，以及可供 capability skip 的標籤。"""
    kind = select_frontend_kind(cfg, os_name=os_name)
    if kind == "telnet":
        return True, "telnet"
    for tool in ("tmux", "minicom"):
        if not which(tool):
            return False, f"cap_missing:{tool}"
    return True, "tmux"


__all__ = [
    "FrontendError", "HumanFrontend", "KEYMAP_TELNET", "TelnetFrontend",
    "TmuxMinicomFrontend", "TmuxReaderFrontend", "frontend_capability",
    "make_frontend_factory", "select_frontend_kind",
]
