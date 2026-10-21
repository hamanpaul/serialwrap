"""P3-T16 委派探針：作為 release manifest `python_cli_delegate.entry` 的目標（僅 stdlib）。

被執行即把接收到的 argv、委派 env、stdin 與 pid 以 JSON 寫到 stdout；
`PROBE_EXIT` 指定 exit code，`PROBE_COUNTER` 指定追加 pid 的計數檔（驗證委派恰執行一次），
`PROBE_READ_STDIN=1` 時讀取 stdin 至 EOF（驗證 stdin 原樣傳遞）。
"""
import json, os, sys
if os.environ.get("PROBE_COUNTER"):                     # 每次被執行就追加一行，用來驗「委派恰執行一次」
    with open(os.environ["PROBE_COUNTER"], "a", encoding="utf-8") as f:
        f.write(f"{os.getpid()}\n")
data = sys.stdin.buffer.read() if os.environ.get("PROBE_READ_STDIN") == "1" else b""
# 以 UTF-8 位元組寫出：Windows pipe 的 stdout 為 ANSI code page（如 cp950），文字模式寫非 ASCII argv 會 UnicodeEncodeError。
sys.stdout.buffer.write((json.dumps({"argv": sys.argv[1:], "depth": os.environ.get("SERIALWRAP_DELEGATE_DEPTH"),
                                     "sha": os.environ.get("SERIALWRAP_DELEGATE_ARGV_SHA"), "stdin": data.decode("utf-8", "replace"),
                                     "manifest": os.environ.get("SERIALWRAP_RELEASE_MANIFEST"),
                                     "pid": os.getpid()}, ensure_ascii=False) + "\n").encode("utf-8"))
sys.stdout.flush()
sys.exit(int(os.environ.get("PROBE_EXIT", "0")))
