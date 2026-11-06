#!/usr/bin/env python3
"""socat 接線 spike 專用之 echo 子程序（§15.4.1、Task 9）。

依據規格書與測試契約：
1. 以 sys.stdin.buffer 讀取二進位資料，以 sys.stdout.buffer 原樣回寫（每次寫入立即 flush）。
2. 僅使用 Python 標準函式庫，嚴格禁止 import sw_core。
3. 讀到位元組序列 __EXIT__\n 或 __EXIT__\r\n 時，回寫該序列之前的所有資料並以 exit 0 結束。
4. 任何非結束序列之資料（包含 __EXIT__ 後接非換行字元）皆原樣回寫，不受讀取分塊切分影響。
"""

from __future__ import annotations

import sys


def main() -> None:
    buf = bytearray()
    sentinel = b"__EXIT__"

    while True:
        chunk = sys.stdin.buffer.read1(65536)
        if not chunk:
            # 讀到 EOF，若緩衝區尚有殘餘資料則回寫後退出
            if buf:
                sys.stdout.buffer.write(buf)
                sys.stdout.buffer.flush()
            break
        buf.extend(chunk)

        while True:
            pos = buf.find(sentinel)
            if pos != -1:
                after_pos = pos + len(sentinel)
                rest = buf[after_pos:]
                if rest.startswith(b"\r\n") or rest.startswith(b"\n"):
                    # 找到完整結束標記序列（__EXIT__\n 或 __EXIT__\r\n）
                    if pos > 0:
                        sys.stdout.buffer.write(buf[:pos])
                        sys.stdout.buffer.flush()
                    sys.exit(0)
                elif rest in (b"", b"\r"):
                    # 緩衝區剛好在 sentinel 結尾或 \r，可能尚在等待結尾換行字元，留待下一輪 chunk
                    # 先將 sentinel 之前的資料寫出
                    if pos > 0:
                        sys.stdout.buffer.write(buf[:pos])
                        sys.stdout.buffer.flush()
                        del buf[:pos]
                    break
                else:
                    # sentinel 後接非換行字元（例如 __EXIT__abc），視為一般資料輸出
                    sys.stdout.buffer.write(buf[: pos + 1])
                    sys.stdout.buffer.flush()
                    del buf[: pos + 1]
                    continue
            else:
                # 未找到 sentinel，檢查 buf 結尾是否可能為 sentinel 前綴
                hold_len = 0
                for k in range(min(len(buf), len(sentinel) - 1), 0, -1):
                    if sentinel.startswith(buf[-k:]):
                        hold_len = k
                        break
                to_write = len(buf) - hold_len
                if to_write > 0:
                    sys.stdout.buffer.write(buf[:to_write])
                    sys.stdout.buffer.flush()
                    del buf[:to_write]
                break


if __name__ == "__main__":
    main()
