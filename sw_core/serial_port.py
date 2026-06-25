"""可替換的序列埠 port 抽象（#84 PORT-1）。

核心 broker 邏輯（仲裁/狀態機/WAL）本身 OS 無關，但序列埠 I/O 原先在
``sw_core/uart_io.py`` 直接綁 ``termios`` + ``os.open`` + ``fcntl``，使該模組在
Windows（無 ``termios``/``fcntl``）連 import 都會 ``ModuleNotFoundError``。

本模組把序列埠 I/O 收斂成單一 ``SerialPort`` 介面，並提供兩個後端：

- ``_PosixSerialPort``：維持現有 ``termios`` 行為（Linux/WSL 生產路徑，**逐位元組等價**，
  零回歸是最高約束）。透過 ``fileno()`` 暴露可被 ``select()`` 多工的整數 fd。
- ``_PySerialPort``：以 ``pyserial`` 實作（Windows，亦可用於其他平台）。Windows 的序列埠
  handle 無法被 ``select()``，故 ``fileno()`` 回 ``None``；呼叫端改走「阻塞讀取（含 timeout）」路徑。

後端由 :func:`open_serial_port` 依平台選擇，並可用環境變數
``SERIALWRAP_SERIAL_BACKEND``（``auto`` / ``posix`` / ``pyserial``）覆寫以利測試。
"""

from __future__ import annotations

import errno
import os
import re
import sys
import time
from abc import ABC, abstractmethod

from .config import UartProfile

# termios/fcntl 僅 POSIX 有；在 Windows 上絕不於 import 期求值，否則整個 daemon 載入即失敗。
# _BAUD_MAP 也用 termios.Bxxxx 常數，故一併放進 POSIX 守衛內。
_BAUD_MAP: dict[int, int] = {}
if os.name == "posix":  # pragma: no cover - 平台相依
    import fcntl
    import termios

    _BAUD_MAP = {
        9600: termios.B9600,
        19200: termios.B19200,
        38400: termios.B38400,
        57600: termios.B57600,
        115200: termios.B115200,
        230400: termios.B230400,
        460800: termios.B460800,
        921600: termios.B921600,
    }

_COM_RE = re.compile(r"^COM\d+$", re.IGNORECASE)


def _win_device_name(device_path: str) -> str:
    """Windows 上把 ``COMx`` 正規化成 ``\\\\.\\COMx``（COM10 以上的高編號埠必須此前綴）。

    已是 ``\\\\.\\`` 形式或非 COMx（如 URL）則原樣返回。
    """
    if device_path.startswith("\\\\.\\"):
        return device_path
    if _COM_RE.match(device_path):
        return r"\\.\%s" % device_path.upper()
    return device_path


class SerialPort(ABC):
    """序列埠 port 介面。

    生命週期：``open()`` → ``configure(profile)`` → 多次 ``read()``/``write()`` → ``close()``。
    """

    @abstractmethod
    def open(self) -> None:
        """開啟底層裝置。失敗拋 ``OSError``（或後端等價例外）。"""

    @abstractmethod
    def configure(self, profile: UartProfile) -> None:
        """套用 baud / data_bits / parity / stop_bits / flow_control 等線路設定。"""

    @abstractmethod
    def read(self, max_bytes: int = 8192) -> bytes:
        """讀取至多 ``max_bytes``。

        - POSIX：呼叫端已用 ``select()`` 確認可讀後才呼叫，等同 ``os.read(fd, n)``
          （非阻塞；無資料時拋 ``BlockingIOError``、EOF 回 ``b""``、其他錯誤拋 ``OSError``）。
        - pyserial：阻塞至多 read timeout 取得 ≥1 byte，再 drain ``in_waiting``；idle 回 ``b""``。
        """

    @abstractmethod
    def write(self, payload: bytes) -> None:
        """寫出整段 ``payload``。"""

    @abstractmethod
    def close(self) -> None:
        """關閉底層裝置（best-effort，吞 I/O 例外）。"""

    @abstractmethod
    def set_baud(self, baud: int) -> None:
        """動態變更 baud rate。"""

    @abstractmethod
    def is_alive(self) -> bool:
        """底層裝置是否仍開啟可用。"""

    def fileno(self) -> int | None:
        """可被 ``select()`` 多工的整數 fd；無法多工（如 Windows pyserial）回 ``None``。"""
        return None


class _PosixSerialPort(SerialPort):
    """POSIX/termios 後端——維持 uart_io 原行為，逐位元組等價。"""

    def __init__(self, device_path: str, profile: UartProfile) -> None:
        self._device_path = device_path
        self._profile = profile
        self._fd: int | None = None

    def _set_nonblock(self, fd: int) -> None:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def open(self) -> None:
        self._fd = os.open(self._device_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

    def configure(self, profile: UartProfile) -> None:
        self._profile = profile
        fd = self._fd
        if fd is None:
            raise OSError("serial not open")
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[3] = 0

        cflag = termios.CREAD | termios.CLOCAL
        cflag |= termios.CS7 if profile.data_bits == 7 else termios.CS8

        parity = profile.parity.upper()
        if parity == "E":
            cflag |= termios.PARENB
        elif parity == "O":
            cflag |= termios.PARENB | termios.PARODD

        if profile.stop_bits == 2:
            cflag |= termios.CSTOPB
        if profile.flow_control.lower() == "rtscts" and hasattr(termios, "CRTSCTS"):
            cflag |= termios.CRTSCTS
        attrs[2] = cflag

        speed = _BAUD_MAP.get(profile.baud, termios.B115200)
        if hasattr(termios, "cfsetispeed") and hasattr(termios, "cfsetospeed"):
            termios.cfsetispeed(attrs, speed)
            termios.cfsetospeed(attrs, speed)
        else:
            attrs[4] = speed
            attrs[5] = speed

        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        self._set_nonblock(fd)

    def read(self, max_bytes: int = 8192) -> bytes:
        fd = self._fd
        if fd is None:
            raise OSError("serial not open")
        return os.read(fd, max_bytes)

    def write(self, payload: bytes) -> None:
        fd = self._fd
        if fd is None:
            raise OSError("serial not open")
        view = memoryview(payload)
        sent = 0
        while sent < len(payload):
            try:
                n = os.write(fd, view[sent:])
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    time.sleep(0.01)
                    continue
                raise
            if n <= 0:
                break
            sent += n

    def close(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def set_baud(self, baud: int) -> None:
        fd = self._fd
        if fd is None or baud not in _BAUD_MAP:
            return
        speed = _BAUD_MAP[baud]
        attrs = termios.tcgetattr(fd)
        if hasattr(termios, "cfsetispeed") and hasattr(termios, "cfsetospeed"):
            termios.cfsetispeed(attrs, speed)
            termios.cfsetospeed(attrs, speed)
        else:
            attrs[4] = attrs[5] = speed
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    def is_alive(self) -> bool:
        fd = self._fd
        if fd is None:
            return False
        try:
            os.fstat(fd)
            return True
        except OSError:
            return False

    def fileno(self) -> int | None:
        return self._fd


class _PySerialPort(SerialPort):
    """pyserial 後端（Windows 及其他平台通用）。

    Windows 序列埠 handle 無法被 ``select()`` 多工，故 ``fileno()`` 回 ``None``，
    呼叫端改走阻塞讀取（read timeout 控制輪詢節奏）。
    """

    _READ_TIMEOUT_S = 0.2
    _WRITE_TIMEOUT_S = 5.0

    def __init__(self, device_path: str, profile: UartProfile) -> None:
        self._device_path = device_path
        self._profile = profile
        self._ser = None  # type: ignore[var-annotated]

    @staticmethod
    def _bytesize(profile: UartProfile):
        import serial

        return serial.SEVENBITS if profile.data_bits == 7 else serial.EIGHTBITS

    @staticmethod
    def _parity(profile: UartProfile):
        import serial

        return {
            "E": serial.PARITY_EVEN,
            "O": serial.PARITY_ODD,
            "N": serial.PARITY_NONE,
        }.get(profile.parity.upper(), serial.PARITY_NONE)

    @staticmethod
    def _stopbits(profile: UartProfile):
        import serial

        return serial.STOPBITS_TWO if profile.stop_bits == 2 else serial.STOPBITS_ONE

    def open(self) -> None:
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - 缺依賴時的清楚訊息
            raise OSError(
                "pyserial 後端需要 pyserial（pip install pyserial）"
            ) from exc
        ser = serial.Serial()
        ser.port = _win_device_name(self._device_path)
        ser.timeout = self._READ_TIMEOUT_S
        ser.write_timeout = self._WRITE_TIMEOUT_S
        self._ser = ser
        self._apply(self._profile)
        try:
            ser.open()
        except serial.SerialException as exc:
            self._ser = None
            # 統一成 OSError，讓呼叫端（既有 except OSError 路徑）契約與 POSIX 一致。
            raise OSError(getattr(exc, "errno", None) or errno.EIO, str(exc)) from exc

    def _apply(self, profile: UartProfile) -> None:
        ser = self._ser
        if ser is None:
            return
        ser.baudrate = profile.baud
        ser.bytesize = self._bytesize(profile)
        ser.parity = self._parity(profile)
        ser.stopbits = self._stopbits(profile)
        ser.rtscts = profile.flow_control.lower() == "rtscts"
        ser.xonxoff = bool(getattr(profile, "xonxoff", False))
        ser.dsrdtr = False

    def configure(self, profile: UartProfile) -> None:
        self._profile = profile
        # pyserial 允許在開啟後直接設定屬性即時 reconfigure。
        self._apply(profile)

    def read(self, max_bytes: int = 8192) -> bytes:
        import serial

        ser = self._ser
        if ser is None or not ser.is_open:
            raise OSError(errno.EBADF, "serial not open")
        try:
            first = ser.read(1)  # 阻塞至多 read timeout；b"" = timeout 無資料（非 EOF）
            if not first:
                return b""
            waiting = ser.in_waiting  # property（非 inWaiting()）
            if waiting:
                first += ser.read(min(waiting, max(max_bytes - 1, 0)))
            return bytes(first)
        except serial.SerialException as exc:  # 拔線：ClearCommError/ReadFile 失敗
            raise OSError(getattr(exc, "errno", None) or errno.EIO, str(exc)) from exc

    def write(self, payload: bytes) -> None:
        import serial

        ser = self._ser
        if ser is None or not ser.is_open:
            raise OSError(errno.EBADF, "serial not open")
        # 不每次 flush（對齊 POSIX _write_all 也不 drain）；write_timeout 防止永久 hang。
        view = memoryview(payload)
        sent = 0
        while sent < len(payload):
            try:
                n = ser.write(view[sent:])
            except serial.SerialTimeoutException as exc:
                raise OSError(errno.ETIMEDOUT, str(exc)) from exc
            except serial.SerialException as exc:
                raise OSError(getattr(exc, "errno", None) or errno.EIO, str(exc)) from exc
            if not n:
                break
            sent += n

    def close(self) -> None:
        ser = self._ser
        self._ser = None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001 - close best-effort
                pass

    def set_baud(self, baud: int) -> None:
        ser = self._ser
        if ser is None:
            return
        ser.baudrate = baud
        self._profile = type(self._profile)(
            baud=baud,
            data_bits=self._profile.data_bits,
            parity=self._profile.parity,
            stop_bits=self._profile.stop_bits,
            flow_control=self._profile.flow_control,
            xonxoff=self._profile.xonxoff,
        )

    def is_alive(self) -> bool:
        ser = self._ser
        return bool(ser is not None and ser.is_open)

    def fileno(self) -> int | None:
        return None


def _select_backend(backend: str | None) -> str:
    mode = (backend or os.environ.get("SERIALWRAP_SERIAL_BACKEND") or "auto").lower()
    if mode in ("posix", "termios"):
        return "posix"
    if mode in ("pyserial", "windows", "win32"):
        return "pyserial"
    # auto：Windows 走 pyserial，其餘維持 POSIX termios（生產路徑零回歸）。
    if os.name == "nt" or sys.platform.startswith("win"):
        return "pyserial"
    return "posix"


def open_serial_port(
    device_path: str,
    profile: UartProfile,
    *,
    backend: str | None = None,
) -> SerialPort:
    """依平台/覆寫建立（尚未開啟的）:class:`SerialPort`。

    呼叫端負責隨後呼叫 ``open()`` 與 ``configure(profile)``。
    """
    chosen = _select_backend(backend)
    if chosen == "pyserial":
        return _PySerialPort(device_path, profile)
    return _PosixSerialPort(device_path, profile)
