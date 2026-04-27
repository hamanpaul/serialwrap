from __future__ import annotations

import re

_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07|[@-Z\\-_])")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class LineBuffer:
    """Per-COM byte→line splitter with ANSI cleaning."""

    def __init__(self, max_line_bytes: int = 16 * 1024) -> None:
        self._buf = bytearray()
        self._max = max_line_bytes

    def feed(self, data: bytes) -> list[str]:
        self._buf.extend(data)
        out: list[str] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                if len(self._buf) > self._max:
                    chunk = bytes(self._buf[: self._max])
                    out.append(self._finalize(chunk, truncated=True))
                    del self._buf[: self._max]
                break
            if idx > self._max:
                chunk = bytes(self._buf[: self._max])
                out.append(self._finalize(chunk, truncated=True))
                del self._buf[: idx + 1]  # discard rest of oversized line up to newline
            else:
                chunk = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                if chunk.endswith(b"\r"):
                    chunk = chunk[:-1]
                out.append(self._finalize(chunk, truncated=False))
        return out

    def _finalize(self, raw: bytes, *, truncated: bool) -> str:
        text = raw.decode("utf-8", errors="replace")
        text = strip_ansi(text)
        if truncated:
            text += " ...truncated"
        return text
