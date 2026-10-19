"""Windows human console 使用的 Telnet byte codec。"""
from __future__ import annotations

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
_ACCEPTED_WILL = {0, 1, 3}  # BINARY、ECHO、SUPPRESS-GO-AHEAD


class TelnetClientCodec:
    """以增量狀態機剝除 Telnet 命令並回覆一次性 option 協商。"""

    def __init__(self) -> None:
        self._state = "data"
        self._command: int | None = None
        self._replied: set[tuple[int, int]] = set()

    @staticmethod
    def encode(data: bytes) -> bytes:
        """將應用資料中的 IAC byte 編碼為兩個 IAC。"""
        return data.replace(bytes((IAC,)), bytes((IAC, IAC)))

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        """消費任意大小的片段，回傳應用資料與協商回覆。"""
        payload = bytearray()
        replies = bytearray()
        for byte in data:
            if self._state == "data":
                if byte == IAC:
                    self._state = "iac"
                else:
                    payload.append(byte)
            elif self._state == "iac":
                if byte == IAC:
                    payload.append(IAC)
                    self._state = "data"
                elif byte in (WILL, WONT, DO, DONT):
                    self._command = byte
                    self._state = "option"
                elif byte == SB:
                    self._state = "subnegotiation"
                else:
                    self._state = "data"
            elif self._state == "option":
                command = self._command
                if command == WILL:
                    reply = DO if byte in _ACCEPTED_WILL else DONT
                    key = (reply, byte)
                    if key not in self._replied:
                        replies.extend((IAC, reply, byte))
                        self._replied.add(key)
                elif command == DO:
                    reply = WILL if byte == 3 else WONT
                    key = (reply, byte)
                    if key not in self._replied:
                        replies.extend((IAC, reply, byte))
                        self._replied.add(key)
                self._command = None
                self._state = "data"
            elif self._state == "subnegotiation":
                if byte == IAC:
                    self._state = "subnegotiation_iac"
            elif self._state == "subnegotiation_iac":
                if byte == SE:
                    self._state = "data"
                elif byte != IAC:
                    self._state = "subnegotiation"
                else:
                    self._state = "subnegotiation"
        return bytes(payload), bytes(replies)


__all__ = ["DONT", "DO", "IAC", "SB", "SE", "WILL", "WONT", "TelnetClientCodec"]
