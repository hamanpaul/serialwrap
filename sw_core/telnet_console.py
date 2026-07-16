"""Windows TCP console 的最小 Telnet 相容層（#131 點 5）。

純邏輯、無 I/O：由 ``uart_io.UARTBridge`` 在無 PTY 平台（Windows）的 console
listener 掛載——accept 即送 ``TELNET_GREETING`` 主動協商，之後每個 client 以
一個 ``TelnetFilter`` 實例過濾入向 byte stream、以 ``escape_iac`` 逸出出向
0xFF。POSIX（PTY 路徑）完全不進本模組。

設計要點：

- **Server 主動協商**（WILL ECHO + WILL SGA + DO SGA + WILL BINARY）：對
  Tera Term／PuTTY／telnet.exe 等被動或主動 client 皆確定性進入
  char-at-a-time + 遠端回顯模式（回顯由 DUT 經 RX fan-out 提供）。
- **出向 8-bit clean**（WILL BINARY）：UART 輸出 passthrough、僅逸出 IAC，
  免除 NVT 的 LF→CRLF 義務。
- **入向刻意留在 NVT**：client 的 WILL BINARY 一律回 DONT，使 CR 摺疊規則
  （CR NUL／CR LF → 單一 CR）恆定適用——Tera Term telnet 送 CR NUL、PuTTY 送
  CR LF，DUT 期望單一 CR。
- **回覆策略防迴圈**：僅狀態變更才回（DONT 已開選項 → WONT 一次並標 off）；
  未經請求的 WILL x → DONT x、DO x → WONT x；我方反應性輸出僅
  DONT／WONT，合規 client 對其至多回 WONT／DONT，而我方對此不回 → 有界終止。
"""
from __future__ import annotations

# Telnet 命令與選項（RFC 854／855）
IAC = 0xFF
SE = 0xF0
SB = 0xFA
WILL = 0xFB
WONT = 0xFC
DO = 0xFD
DONT = 0xFE
OPT_BINARY = 0x00
OPT_ECHO = 0x01
OPT_SGA = 0x03

# accept 即送的 server 主動協商（12 bytes）。
TELNET_GREETING = bytes(
    [
        IAC, WILL, OPT_ECHO,
        IAC, WILL, OPT_SGA,
        IAC, DO, OPT_SGA,
        IAC, WILL, OPT_BINARY,
    ]
)

# 狀態機狀態（跨 recv() 邊界存活）。
_DATA = 0        # 一般資料
_DATA_CR = 1     # 剛 emit 過 CR：吞掉緊跟的 NUL／LF 一次
_IAC = 2         # 收到 IAC，等命令 byte
_OPT_WILL = 3    # IAC WILL，等 option byte
_OPT_WONT = 4    # IAC WONT，等 option byte
_OPT_DO = 5      # IAC DO，等 option byte
_OPT_DONT = 6    # IAC DONT，等 option byte
_SB = 7          # 子協商中（IAC SB ... IAC SE），內容全吞
_SB_IAC = 8      # 子協商中收到 IAC：SE 結束、IAC 為資料逸出


def escape_iac(payload: bytes) -> bytes:
    """出向逸出：0xFF → IAC IAC。無 0xFF 時原物件直回（免重配置）。"""
    if b"\xff" not in payload:
        return payload
    return payload.replace(b"\xff", b"\xff\xff")


class TelnetFilter:
    """每個 telnet console client 一實例的入向過濾器。

    ``feed(data)`` 回傳 ``(uart_bytes, reply_bytes)``：``uart_bytes`` 為過濾後
    應送往 UART 的資料；``reply_bytes`` 為應回送給 client 的協商回應（可能為
    空）。狀態跨呼叫存活，容忍 IAC 序列／子協商／CR 對切在任意 recv 邊界。
    """

    def __init__(self) -> None:
        self._state = _DATA
        # 我方已宣告（greeting WILL）且仍生效的 local options。
        self._local_on: set[int] = {OPT_ECHO, OPT_SGA, OPT_BINARY}

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        uart = bytearray()
        reply = bytearray()
        for byte in data:
            state = self._state
            if state in (_DATA, _DATA_CR):
                if state == _DATA_CR:
                    self._state = _DATA
                    if byte in (0x00, 0x0A):
                        continue  # CR NUL／CR LF：CR 已即時 emit，吞後綴一次
                if byte == IAC:
                    self._state = _IAC
                elif byte == 0x0D:
                    # CR 立即 emit（零延遲，不押住鍵擊等下一 byte）再進 DATA_CR。
                    uart.append(0x0D)
                    self._state = _DATA_CR
                else:
                    uart.append(byte)
            elif state == _IAC:
                if byte == IAC:
                    uart.append(0xFF)  # IAC IAC → 資料 0xFF
                    self._state = _DATA
                elif byte == WILL:
                    self._state = _OPT_WILL
                elif byte == WONT:
                    self._state = _OPT_WONT
                elif byte == DO:
                    self._state = _OPT_DO
                elif byte == DONT:
                    self._state = _OPT_DONT
                elif byte == SB:
                    self._state = _SB
                else:
                    # 其餘 2-byte 命令（SE／NOP／DM／BRK／IP／AO／AYT／EC／EL／GA）一律吞。
                    self._state = _DATA
            elif state == _OPT_WILL:
                if byte != OPT_SGA:
                    # WILL SGA 為我方 DO SGA 的 ack → 靜默；其餘（含 WILL BINARY，
                    # 入向刻意留 NVT）一律 DONT。
                    reply += bytes((IAC, DONT, byte))
                self._state = _DATA
            elif state == _OPT_DO:
                if byte not in self._local_on:
                    reply += bytes((IAC, WONT, byte))
                self._state = _DATA
            elif state == _OPT_DONT:
                if byte in self._local_on:
                    # 狀態變更才回（防乒乓）：WONT 一次並標 off。
                    self._local_on.discard(byte)
                    reply += bytes((IAC, WONT, byte))
                self._state = _DATA
            elif state == _OPT_WONT:
                self._state = _DATA  # 不回（我方 DONT 的 ack 或 client 自行撤回）
            elif state == _SB:
                if byte == IAC:
                    self._state = _SB_IAC
            elif state == _SB_IAC:
                if byte == SE:
                    self._state = _DATA
                else:
                    self._state = _SB  # IAC IAC（子協商內資料逸出）或防禦容錯 → 續吞
        return bytes(uart), bytes(reply)
