"""#131 點 5：Windows TCP console 的最小 Telnet 相容層（純邏輯單測）。

`sw_core/telnet_console.py`：

- ``TELNET_GREETING``：accept 即送的 server 主動協商（WILL ECHO + WILL SGA +
  DO SGA + WILL BINARY，12 bytes）→ Tera Term/PuTTY/telnet.exe 進 char-mode、
  關本地回顯；
- ``TelnetFilter.feed(data) -> (uart_bytes, reply_bytes)``：入向 IAC 狀態機
  （吞協商/子協商、IAC IAC 還原、NVT CR NUL／CR LF 摺疊為 CR），狀態跨
  ``recv()`` 邊界存活；
- ``escape_iac()``：出向 0xFF → IAC IAC 逸出。
"""
from __future__ import annotations

import pytest

from sw_core.telnet_console import TELNET_GREETING, TelnetFilter, escape_iac

IAC = b"\xff"
WILL, WONT, DO, DONT, SB, SE = b"\xfb", b"\xfc", b"\xfd", b"\xfe", b"\xfa", b"\xf0"
OPT_BINARY, OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS = (
    b"\x00", b"\x01", b"\x03", b"\x18", b"\x1f",
)


class TestGreeting:
    def test_greeting_bytes(self) -> None:
        assert TELNET_GREETING == (
            IAC + WILL + OPT_ECHO
            + IAC + WILL + OPT_SGA
            + IAC + DO + OPT_SGA
            + IAC + WILL + OPT_BINARY
        )
        assert len(TELNET_GREETING) == 12


class TestDataPassthrough:
    def test_plain_ascii(self) -> None:
        assert TelnetFilter().feed(b"ls -la") == (b"ls -la", b"")

    def test_high_bytes_passthrough(self) -> None:
        assert TelnetFilter().feed(b"\x80\xfe\x01") == (b"\x80\xfe\x01", b"")

    def test_iac_iac_unescapes_to_single_ff(self) -> None:
        assert TelnetFilter().feed(b"a" + IAC + IAC + b"b") == (b"a\xffb", b"")

    def test_iac_iac_split_across_feeds(self) -> None:
        f = TelnetFilter()
        assert f.feed(IAC) == (b"", b"")
        assert f.feed(IAC) == (b"\xff", b"")


class TestNvtCrFolding:
    def test_cr_nul_folds_to_cr(self) -> None:
        assert TelnetFilter().feed(b"PING\r\x00") == (b"PING\r", b"")

    def test_cr_lf_folds_to_cr(self) -> None:
        assert TelnetFilter().feed(b"PING\r\n") == (b"PING\r", b"")

    def test_cr_emitted_immediately_at_chunk_end(self) -> None:
        """CR 於 chunk 尾必須即刻 emit（零延遲），不得押住等下一 byte。"""
        f = TelnetFilter()
        assert f.feed(b"\r") == (b"\r", b"")
        assert f.feed(b"x") == (b"x", b"")  # 非 NUL/LF → 不吞

    def test_cr_then_lf_across_boundary_swallows_lf(self) -> None:
        f = TelnetFilter()
        assert f.feed(b"\r") == (b"\r", b"")
        assert f.feed(b"\n") == (b"", b"")

    def test_cr_then_nul_across_boundary_swallows_nul(self) -> None:
        f = TelnetFilter()
        assert f.feed(b"\r") == (b"\r", b"")
        assert f.feed(b"\x00") == (b"", b"")

    def test_consecutive_crs_all_emitted(self) -> None:
        assert TelnetFilter().feed(b"\r\r") == (b"\r\r", b"")

    def test_bare_lf_passthrough(self) -> None:
        assert TelnetFilter().feed(b"a\nb") == (b"a\nb", b"")

    def test_cr_then_iac_negotiation(self) -> None:
        uart, reply = TelnetFilter().feed(b"\r" + IAC + WILL + OPT_ECHO)
        assert uart == b"\r"
        assert reply == IAC + DONT + OPT_ECHO  # 未經請求 WILL ECHO → DONT


class TestNegotiationReplies:
    @pytest.mark.parametrize("opt", [OPT_ECHO, OPT_SGA, OPT_BINARY])
    def test_do_acks_of_greeting_get_no_reply(self, opt: bytes) -> None:
        assert TelnetFilter().feed(IAC + DO + opt) == (b"", b"")

    def test_do_unsupported_option_gets_wont(self) -> None:
        assert TelnetFilter().feed(IAC + DO + OPT_TTYPE) == (b"", IAC + WONT + OPT_TTYPE)

    def test_will_sga_ack_gets_no_reply(self) -> None:
        assert TelnetFilter().feed(IAC + WILL + OPT_SGA) == (b"", b"")

    def test_will_binary_refused_to_keep_nvt_inbound(self) -> None:
        """入向刻意留在 NVT（CR 摺疊規則恆定）→ client WILL BINARY 一律 DONT。"""
        assert TelnetFilter().feed(IAC + WILL + OPT_BINARY) == (b"", IAC + DONT + OPT_BINARY)

    @pytest.mark.parametrize("opt", [OPT_NAWS, OPT_TTYPE, OPT_ECHO])
    def test_unsolicited_will_gets_dont(self, opt: bytes) -> None:
        assert TelnetFilter().feed(IAC + WILL + opt) == (b"", IAC + DONT + opt)

    def test_dont_echo_gets_wont_once_only(self) -> None:
        """DONT x（已宣告且 on）→ 回 WONT 一次標 off；off 態重複 DONT 不再回（防乒乓）。"""
        f = TelnetFilter()
        assert f.feed(IAC + DONT + OPT_ECHO) == (b"", IAC + WONT + OPT_ECHO)
        assert f.feed(IAC + DONT + OPT_ECHO) == (b"", b"")

    def test_wont_gets_no_reply(self) -> None:
        assert TelnetFilter().feed(IAC + WONT + OPT_NAWS) == (b"", b"")

    def test_putty_opening_burst(self) -> None:
        """PuTTY 開場（WILL NAWS + WILL TTYPE + DO ECHO）→ DONT×2、UART 零位元組。"""
        burst = IAC + WILL + OPT_NAWS + IAC + WILL + OPT_TTYPE + IAC + DO + OPT_ECHO
        uart, reply = TelnetFilter().feed(burst)
        assert uart == b""
        assert reply == IAC + DONT + OPT_NAWS + IAC + DONT + OPT_TTYPE

    def test_negotiation_split_across_feeds(self) -> None:
        f = TelnetFilter()
        assert f.feed(IAC) == (b"", b"")
        assert f.feed(WILL) == (b"", b"")
        assert f.feed(OPT_NAWS) == (b"", IAC + DONT + OPT_NAWS)


class TestSubnegotiation:
    def test_sb_swallowed_entirely(self) -> None:
        sb = IAC + SB + OPT_NAWS + b"\x00\x50\x00\x18" + IAC + SE
        assert TelnetFilter().feed(sb + b"after") == (b"after", b"")

    def test_sb_spanning_three_reads(self) -> None:
        f = TelnetFilter()
        assert f.feed(IAC + SB + OPT_NAWS) == (b"", b"")
        assert f.feed(b"\x00\x50\x00\x18") == (b"", b"")
        assert f.feed(IAC + SE + b"after") == (b"after", b"")

    def test_sb_with_escaped_iac_inside(self) -> None:
        """SB 內 IAC IAC 為資料逸出，不得誤判為 SE 終止。"""
        sb = IAC + SB + OPT_TTYPE + IAC + IAC + b"\x01" + IAC + SE
        assert TelnetFilter().feed(sb + b"x") == (b"x", b"")


class TestTwoByteCommands:
    def test_nop_and_ayt_swallowed(self) -> None:
        data = IAC + b"\xf1" + IAC + b"\xf6" + b"ok"  # NOP、AYT
        assert TelnetFilter().feed(data) == (b"ok", b"")

    def test_interleaved_data_negotiation_cr(self) -> None:
        data = b"a" + IAC + DO + OPT_SGA + b"b\r\x00c"
        assert TelnetFilter().feed(data) == (b"ab\rc", b"")


class TestEscapeIac:
    def test_escapes_ff(self) -> None:
        assert escape_iac(b"\xff\x01\xff") == b"\xff\xff\x01\xff\xff"

    def test_dense_ff_buffer(self) -> None:
        assert escape_iac(b"\xff" * 4) == b"\xff" * 8

    def test_no_ff_returns_same_object(self) -> None:
        payload = b"hello world"
        assert escape_iac(payload) is payload  # 無 0xFF → 不重配置


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
