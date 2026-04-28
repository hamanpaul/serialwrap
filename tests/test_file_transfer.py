"""file_transfer 模組的單元測試。"""
from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
from typing import Any
from unittest import mock

from sw_core.file_transfer import (
    _extract_between_sentinels,
    _split_chunks,
    pull_file,
    push_file,
)

_PROMPT = r"root@host:~# "
_PROMPT_REGEX = r"root@host:~#\s"


class _FakeBridge:
    """模擬 UARTBridge，記錄送出的命令並回放預設的 RX 回應。"""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self._rx_text = ""
        self._rx_responses: list[str] = []

    def enqueue_rx(self, text: str) -> None:
        self._rx_responses.append(text)

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None) -> None:
        self.commands.append(cmd)
        if self._rx_responses:
            self._rx_text += self._rx_responses.pop(0)

    def rx_snapshot_len(self) -> int:
        return len(self._rx_text)

    def rx_text_from(self, from_offset: int) -> str:
        return self._rx_text[from_offset:]

    def wait_for_regex_from(self, pattern: str, from_offset: int, timeout_s: float) -> bool:
        import re
        return bool(re.search(pattern, self._rx_text[from_offset:]))


class TestPushFileSuccess(unittest.TestCase):
    """push_file 正常流程：分段傳輸 + checksum 驗證 + mv。"""

    def test_push_small_file(self) -> None:
        data = b"hello world"
        md5 = hashlib.md5(data).hexdigest()

        bridge = _FakeBridge()
        # chunk 回應：每段 printf 後跟一個 prompt
        bridge.enqueue_rx(f"printf done\r\n{_PROMPT}")
        # md5sum 回應
        bridge.enqueue_rx(f"{md5}  /tmp/.sw_upload_test\r\n{_PROMPT}")
        # mv 回應
        bridge.enqueue_rx(f"{_PROMPT}")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(data)
            local = f.name

        try:
            result = push_file(
                bridge, local, "/tmp/dest.bin",
                chunk_size=4096,
                timeout_s=5.0,
                prompt_regex=_PROMPT_REGEX,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["bytes"], len(data))
            self.assertEqual(result["chunks"], 1)
            self.assertEqual(result["md5"], md5)
            self.assertEqual(result["remote_path"], "/tmp/dest.bin")
            # 應送出 3 個命令：printf、md5sum、mv
            self.assertEqual(len(bridge.commands), 3)
            self.assertIn("printf", bridge.commands[0])
            self.assertIn("md5sum", bridge.commands[1])
            self.assertIn("mv", bridge.commands[2])
        finally:
            os.unlink(local)

    def test_push_multiple_chunks(self) -> None:
        data = b"A" * 100
        md5 = hashlib.md5(data).hexdigest()

        bridge = _FakeBridge()
        # 3 段（chunk_size=40 → 40+40+20）
        for _ in range(3):
            bridge.enqueue_rx(f"ok\r\n{_PROMPT}")
        bridge.enqueue_rx(f"{md5}  /tmp/.sw_upload_test\r\n{_PROMPT}")
        bridge.enqueue_rx(f"{_PROMPT}")

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            local = f.name

        try:
            result = push_file(
                bridge, local, "/remote/file",
                chunk_size=40,
                timeout_s=5.0,
                prompt_regex=_PROMPT_REGEX,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["chunks"], 3)
            self.assertEqual(result["bytes"], 100)
            # 3 chunks + md5sum + mv = 5 commands
            self.assertEqual(len(bridge.commands), 5)
            # 第一段用 >，後續用 >>
            self.assertIn(" > ", bridge.commands[0])
            self.assertIn(" >> ", bridge.commands[1])
            self.assertIn(" >> ", bridge.commands[2])
        finally:
            os.unlink(local)


class TestPushFileNotFound(unittest.TestCase):
    """push_file 本地檔案不存在時回傳 LOCAL_FILE_NOT_FOUND。"""

    def test_not_found(self) -> None:
        bridge = _FakeBridge()
        result = push_file(
            bridge, "/nonexistent/path/file.bin", "/tmp/dest",
            prompt_regex=_PROMPT_REGEX,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "LOCAL_FILE_NOT_FOUND")


class TestPushChecksumMismatch(unittest.TestCase):
    """push_file checksum 不符時回傳 CHECKSUM_MISMATCH。"""

    def test_mismatch(self) -> None:
        data = b"test data"
        wrong_md5 = "0" * 32

        bridge = _FakeBridge()
        bridge.enqueue_rx(f"ok\r\n{_PROMPT}")
        bridge.enqueue_rx(f"{wrong_md5}  /tmp/.sw_upload_test\r\n{_PROMPT}")
        # cleanup rm -f 回應
        bridge.enqueue_rx(f"{_PROMPT}")

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            local = f.name

        try:
            result = push_file(
                bridge, local, "/tmp/dest",
                prompt_regex=_PROMPT_REGEX,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "CHECKSUM_MISMATCH")
            self.assertEqual(result["actual"], wrong_md5)
            self.assertEqual(result["expected"], hashlib.md5(data).hexdigest())
        finally:
            os.unlink(local)


class TestPushTransferTimeout(unittest.TestCase):
    """push_file 分段傳輸中 prompt timeout。"""

    def test_timeout_on_chunk(self) -> None:
        data = b"X" * 100

        bridge = _FakeBridge()
        # 不回應 prompt → wait_for_regex_from 回傳 False
        bridge.enqueue_rx("no prompt here")
        # cleanup 回應
        bridge.enqueue_rx(f"{_PROMPT}")

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            local = f.name

        try:
            result = push_file(
                bridge, local, "/tmp/dest",
                timeout_s=0.1,
                prompt_regex=_PROMPT_REGEX,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "TRANSFER_TIMEOUT")
            self.assertEqual(result["chunks_sent"], 0)
        finally:
            os.unlink(local)


class TestPullFileSuccess(unittest.TestCase):
    """pull_file 正常流程：base64 擷取 + 解碼 + checksum 驗證。"""

    def test_pull_basic(self) -> None:
        data = b"hello pull test"
        b64 = base64.b64encode(data).decode("ascii")
        md5 = hashlib.md5(data).hexdigest()
        from sw_core.file_transfer import _SENTINEL_BEGIN, _SENTINEL_END

        bridge = _FakeBridge()
        # base64 命令回應
        bridge.enqueue_rx(
            f"echo ... && base64 < /etc/test && echo ...\r\n"
            f"{_SENTINEL_BEGIN}\r\n"
            f"{b64}\r\n"
            f"{_SENTINEL_END}\r\n"
            f"{_PROMPT}"
        )
        # md5sum 驗證回應
        bridge.enqueue_rx(f"{md5}  /etc/test\r\n{_PROMPT}")

        outdir = tempfile.mkdtemp()
        local = os.path.join(outdir, "pulled.bin")
        try:
            result = pull_file(
                bridge, "/etc/test", local,
                timeout_s=5.0,
                prompt_regex=_PROMPT_REGEX,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["bytes"], len(data))
            self.assertEqual(result["md5"], md5)
            self.assertEqual(result["local_path"], local)
            with open(local, "rb") as f:
                self.assertEqual(f.read(), data)
        finally:
            if os.path.exists(local):
                os.unlink(local)
            os.rmdir(outdir)

    def test_pull_default_local_path(self) -> None:
        """local_path 為 None 時使用 basename。"""
        data = b"default path"
        b64 = base64.b64encode(data).decode("ascii")
        md5 = hashlib.md5(data).hexdigest()
        from sw_core.file_transfer import _SENTINEL_BEGIN, _SENTINEL_END

        bridge = _FakeBridge()
        bridge.enqueue_rx(
            f"cmd\r\n{_SENTINEL_BEGIN}\r\n{b64}\r\n{_SENTINEL_END}\r\n{_PROMPT}"
        )
        bridge.enqueue_rx(f"{md5}  /etc/config.txt\r\n{_PROMPT}")

        # pull_file 會用 basename("config.txt") 作為 local_path
        result = pull_file(
            bridge, "/etc/config.txt",
            timeout_s=5.0,
            prompt_regex=_PROMPT_REGEX,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["local_path"], "config.txt")
        # 清理
        if os.path.exists("config.txt"):
            os.unlink("config.txt")

    def test_pull_with_ansi_contamination(self) -> None:
        """pull_file should work with ANSI-contaminated base64 output."""
        data = b"binary\x00\x01\x02file"
        b64 = base64.b64encode(data).decode("ascii")
        md5 = hashlib.md5(data).hexdigest()
        from sw_core.file_transfer import _SENTINEL_BEGIN, _SENTINEL_END

        bridge = _FakeBridge()
        # Contaminate the base64 with ANSI color codes and bracketed-paste markers
        b64_with_ansi = (
            f"{b64[:8]}\x1b[0m{b64[8:16]}\x1b[31m"
            f"{b64[16:24]}\x1b[?2004h{b64[24:]}\x1b[?2004l\x1b[0m"
        )
        bridge.enqueue_rx(
            f"echo start\r\n{_SENTINEL_BEGIN}\r\n"
            f"{b64_with_ansi}\r\n"
            f"{_SENTINEL_END}\r\n{_PROMPT}"
        )
        bridge.enqueue_rx(f"{md5}  /tmp/binary.bin\r\n{_PROMPT}")

        outdir = tempfile.mkdtemp()
        local = os.path.join(outdir, "ansi_pull.bin")
        try:
            result = pull_file(
                bridge, "/tmp/binary.bin", local,
                timeout_s=5.0,
                prompt_regex=_PROMPT_REGEX,
            )
            self.assertTrue(result["ok"], f"Pull failed: {result}")
            self.assertEqual(result["bytes"], len(data))
            self.assertEqual(result["md5"], md5)
            with open(local, "rb") as f:
                self.assertEqual(f.read(), data)
        finally:
            if os.path.exists(local):
                os.unlink(local)
            os.rmdir(outdir)


class TestPushChunkBoundary(unittest.TestCase):
    """邊界測試：空檔、恰好一個 chunk、多個 chunk。"""

    def test_empty_file(self) -> None:
        data = b""
        md5 = hashlib.md5(data).hexdigest()

        bridge = _FakeBridge()
        bridge.enqueue_rx(f"ok\r\n{_PROMPT}")
        bridge.enqueue_rx(f"{md5}  /tmp/.sw_upload_test\r\n{_PROMPT}")
        bridge.enqueue_rx(f"{_PROMPT}")

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            local = f.name

        try:
            result = push_file(
                bridge, local, "/tmp/empty",
                chunk_size=2048,
                prompt_regex=_PROMPT_REGEX,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["bytes"], 0)
            self.assertEqual(result["chunks"], 1)
        finally:
            os.unlink(local)

    def test_exact_one_chunk(self) -> None:
        data = b"X" * 64
        md5 = hashlib.md5(data).hexdigest()

        bridge = _FakeBridge()
        bridge.enqueue_rx(f"ok\r\n{_PROMPT}")
        bridge.enqueue_rx(f"{md5}  /tmp/.sw_upload_test\r\n{_PROMPT}")
        bridge.enqueue_rx(f"{_PROMPT}")

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            local = f.name

        try:
            result = push_file(
                bridge, local, "/tmp/exact",
                chunk_size=64,
                prompt_regex=_PROMPT_REGEX,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["chunks"], 1)
        finally:
            os.unlink(local)

    def test_exact_boundary_plus_one(self) -> None:
        data = b"Y" * 65
        md5 = hashlib.md5(data).hexdigest()

        bridge = _FakeBridge()
        bridge.enqueue_rx(f"ok\r\n{_PROMPT}")
        bridge.enqueue_rx(f"ok\r\n{_PROMPT}")
        bridge.enqueue_rx(f"{md5}  /tmp/.sw_upload_test\r\n{_PROMPT}")
        bridge.enqueue_rx(f"{_PROMPT}")

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            local = f.name

        try:
            result = push_file(
                bridge, local, "/tmp/boundary",
                chunk_size=64,
                prompt_regex=_PROMPT_REGEX,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["chunks"], 2)
        finally:
            os.unlink(local)


class TestSplitChunks(unittest.TestCase):
    """_split_chunks 輔助函式測試。"""

    def test_empty(self) -> None:
        self.assertEqual(_split_chunks(b"", 10), [b""])

    def test_exact(self) -> None:
        result = _split_chunks(b"1234567890", 10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], b"1234567890")

    def test_split(self) -> None:
        result = _split_chunks(b"12345", 2)
        self.assertEqual(result, [b"12", b"34", b"5"])


class TestExtractBetweenSentinels(unittest.TestCase):
    """_extract_between_sentinels 輔助函式測試。"""

    def test_normal(self) -> None:
        text = "junk===SW_XFER_BEGIN===\r\naGVsbG8=\r\n===SW_XFER_END===\r\nprompt"
        result = _extract_between_sentinels(text)
        self.assertEqual(result, "aGVsbG8=")

    def test_missing_begin(self) -> None:
        text = "no markers here===SW_XFER_END===done"
        self.assertIsNone(_extract_between_sentinels(text))

    def test_missing_end(self) -> None:
        text = "===SW_XFER_BEGIN===data but no end"
        self.assertIsNone(_extract_between_sentinels(text))

    def test_empty_content(self) -> None:
        text = "===SW_XFER_BEGIN======SW_XFER_END==="
        result = _extract_between_sentinels(text)
        self.assertEqual(result, "")

    def test_ansi_color_sequences(self) -> None:
        """ANSI color codes embedded in base64 should be stripped."""
        clean_b64 = "aGVsbG8gd29ybGQ="
        # Inject ANSI color codes (SGR reset, red foreground, etc.)
        text = (
            "===SW_XFER_BEGIN===\r\n"
            f"\x1b[0m{clean_b64[:8]}\x1b[31m{clean_b64[8:]}\x1b[0m"
            "\r\n===SW_XFER_END==="
        )
        result = _extract_between_sentinels(text)
        self.assertEqual(result, clean_b64)

    def test_bracketed_paste_marker(self) -> None:
        """Bracketed paste mode sequences should be stripped."""
        clean_b64 = "aGVsbG8="
        # \x1b[?2004h and \x1b[?2004l are bracketed-paste enable/disable
        text = (
            "===SW_XFER_BEGIN===\r\n"
            f"\x1b[?2004h{clean_b64}\x1b[?2004l"
            "\r\n===SW_XFER_END==="
        )
        result = _extract_between_sentinels(text)
        self.assertEqual(result, clean_b64)


class TestPullParseFailed(unittest.TestCase):
    """pull_file sentinel 解析失敗。"""

    def test_no_sentinels(self) -> None:
        bridge = _FakeBridge()
        bridge.enqueue_rx(f"some output without sentinels\r\n{_PROMPT}")

        result = pull_file(
            bridge, "/etc/missing",
            timeout_s=5.0,
            prompt_regex=_PROMPT_REGEX,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PULL_PARSE_FAILED")


if __name__ == "__main__":
    unittest.main()
