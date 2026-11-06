#!/usr/bin/env python3
"""擷取 sw_core.file_transfer 純函式的 Python 基線輸出，產生 contracts/golden/file_transfer.ndjson。

每列為 ``{"fn","args","out"}``；bytes 一律以小寫 hex 表示；Python 對未知工具 raise ValueError
時 ``out`` 為字串 ``"<ValueError>"``。輸入集合由 ``random.Random(20261015)`` 決定性產生，
輸出使用 ``ensure_ascii=True``，使重跑結果逐位元組相同（供 tests/test_transfer_golden_fresh.py 驗證）。
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sw_core import file_transfer as ft  # noqa: E402

DEFAULT_OUT = ROOT / "contracts" / "golden" / "file_transfer.ndjson"
VALUE_ERROR = "<ValueError>"

PATHS = [
    "/tmp/a", "/tmp/with space", "/tmp/it's", "", "/tmp/ü中", "/tmp/$(rm -rf)",
    "/tmp/a\nb", "-rf", "/tmp/.sw_upload_0123456789ab", "/tmp/a'b\"c`d", "/tmp/a;b&c|d",
]
TOOLS = ["base64", "openssl", "bogus", ""]
OPS = [">", ">>"]


def call(fn, *args: Any) -> Any:
    try:
        return fn(*args)
    except ValueError:
        return VALUE_ERROR


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    opts = ap.parse_args()
    rng = random.Random(20261015)
    rows: list[dict[str, Any]] = []

    def add(fn: str, args: list[Any], out: Any) -> None:
        rows.append({"fn": fn, "args": args, "out": out})

    def rand_bytes(n: int) -> bytes:
        return bytes(rng.randrange(256) for _ in range(n))

    # ── 常數 ──
    add("constants", [], {
        "SENTINEL_BEGIN": ft._SENTINEL_BEGIN, "SENTINEL_END": ft._SENTINEL_END,
        "TOOL_PROBE_PAYLOAD": ft._TOOL_PROBE_PAYLOAD,
        "DECODER_B64": ft._DECODER_B64_SENTINEL, "DECODER_OPENSSL": ft._DECODER_OPENSSL_SENTINEL,
        "ENCODER_B64": ft._ENCODER_B64_SENTINEL, "ENCODER_OPENSSL": ft._ENCODER_OPENSSL_SENTINEL,
        "CHUNK": ft._CHUNK_SENTINEL, "MD5": ft._MD5_SENTINEL, "MV": ft._MV_SENTINEL,
        "CLEANUP": ft._CLEANUP_SENTINEL, "ENCODER_FAILURE": ft._ENCODER_FAILURE_SENTINEL,
        "DECODERS": list(ft._DECODERS), "ENCODERS": list(ft._ENCODERS),
        "ACK_MODES": list(ft._ACK_MODES), "DEFAULT_CHUNK_SIZE": ft.DEFAULT_CHUNK_SIZE,
        "DEFAULT_ECHO_SLICE_SIZE": ft.DEFAULT_ECHO_SLICE_SIZE,
        "DEFAULT_ECHO_TIMEOUT_S": ft.DEFAULT_ECHO_TIMEOUT_S,
    })

    # ── 樣板類 ──
    for prefix in ["SW_XFER_CHUNK_", "SW_XFER_MD5_", "x", "", "it's"]:
        add("runtime_sentinel", [prefix], ft._runtime_sentinel(prefix))
    for marker in [ft._SENTINEL_BEGIN, ft._SENTINEL_END, "===SW_XFER_X===", "===SW_XFER_===",
                   "BEGIN", "===SW_XFER_BEGIN", "SW_XFER_BEGIN===", "", "===SW_XFER_a'b==="]:
        add("marker_expression", [marker], call(ft._marker_expression, marker))
    add("decoder_probe_command", [], ft._decoder_probe_command())
    add("encoder_probe_command", [], ft._encoder_probe_command())
    for d in TOOLS:
        add("decoder_command_name", [d], call(ft._decoder_command_name, d))
        add("encoder_command_name", [d], call(ft._encoder_command_name, d))

    encoded_samples = [""] + [ft.base64.b64encode(rand_bytes(n)).decode("ascii") for n in (0, 1, 2, 3, 512, 513)]
    for enc, dec, op, path in itertools.product(encoded_samples, TOOLS, OPS, PATHS[:6]):
        add("chunk_command", [enc, dec, op, path], call(ft._chunk_command, enc, dec, op, path))
    for p in PATHS:
        add("md5_command", [p], ft._md5_command(p))
        add("cleanup_command", [p], ft._cleanup_command(p))
        for q in PATHS:
            add("mv_command", [p, q], ft._mv_command(p, q))
        for e in TOOLS:
            add("pull_command", [p, e], call(ft._pull_command, p, e))

    # ── b64encode / md5_hex / split_chunks ──
    for n in range(0, 7):
        for _ in range(4):
            b = rand_bytes(n)
            add("b64encode", [b.hex()], ft.base64.b64encode(b).decode("ascii"))
    for n in (0, 1, 2, 3, 511, 512, 513, 1024):
        b = rand_bytes(n)
        add("b64encode", [b.hex()], ft.base64.b64encode(b).decode("ascii"))
    for n in (0, 1, 15, 16, 55, 56, 63, 64, 65, 512, 513):
        b = rand_bytes(n)
        add("md5_hex", [b.hex()], ft.hashlib.md5(b).hexdigest())
    for n, cs in [(0, 512), (0, 1), (1, 512), (511, 512), (512, 512), (513, 512), (1025, 512),
                  (10, 3), (9, 3), (7, 1), (100, 64)]:
        b = rand_bytes(n)
        add("split_chunks", [b.hex(), cs], [c.hex() for c in ft._split_chunks(b, cs)])

    # ── line_budget_exceeded（UTF-8 bytes 計）──
    for cmds, budget in [
        ([], 10), (["abc"], None), (["abc"], 3), (["abc"], 2), (["中"], 3), (["中"], 2), (["ü"], 1),
        (["a", "bbbb"], 3), (["a", "bb"], 3), ([""], 0), (["x"], 0), (["😀"], 4), (["😀"], 3),
        ([ft._decoder_probe_command()], 282), ([ft._decoder_probe_command()], 281),
        ([ft._encoder_probe_command()], 273), ([ft._encoder_probe_command()], 272),
        ([ft._md5_command("/tmp/a")], 80), (["abc", "中"], None), (["abc"], -1),
    ]:
        add("line_budget_exceeded", [cmds, budget], ft._line_budget_exceeded(tuple(cmds), budget))

    # ── strip_command_echo ──
    cmd = ft._md5_command("/tmp/a")
    bodies = ["out", "", "\nrest", "\r\nrest", "\rrest", "rest", "\n\nrest", "\r\n\r\nrest"]
    for body in bodies:
        for text in (cmd + body, "X" + cmd + body, cmd[:-3] + body):
            add("strip_command_echo", [text, cmd], ft._strip_command_echo(text, cmd))
        add("strip_command_echo", [cmd + body, None], ft._strip_command_echo(cmd + body, None))
        add("strip_command_echo", [cmd + body, ""], ft._strip_command_echo(cmd + body, ""))
    add("strip_command_echo", ["", cmd], "")
    add("strip_command_echo", ["", None], "")

    # ── *_sentinel_seen ──
    S = ft._CHUNK_SENTINEL
    chunk = ft._chunk_command("YQ==", "base64", ">", "/tmp/x")
    suffixes = ["", "\n", "\r\n", "\r", " ", "\r\r\n", "x", "\n# ", "\r\n# ", "\r# ", "\n\n"]
    prefixes = ["", "\n", "\r\n", "\r", "abc", "abc\n", "abc\r\n", "abc\r", " ", "\n\n", "\r\r\n", "\x1b[0m"]
    for pre, suf in itertools.product(prefixes, suffixes):
        text = pre + S + suf
        for fn, py in (("runtime_sentinel_seen", ft._runtime_sentinel_seen),
                       ("failure_sentinel_seen", ft._failure_sentinel_seen)):
            add(fn, [text, S, None], py(text, S, command=None))
            echoed = chunk + "\r\n" + text
            add(fn, [echoed, S, chunk], py(echoed, S, command=chunk))
    # 只出現在 echo 中、半截 sentinel、多次出現
    echo_only = chunk + "\r\n# "
    for fn, py in (("runtime_sentinel_seen", ft._runtime_sentinel_seen),
                   ("failure_sentinel_seen", ft._failure_sentinel_seen)):
        for text, c in [(echo_only, chunk), (echo_only, None), (chunk, chunk),
                        ("SW_XFER_CHUNK_4", None), ("SW_XFER_CHUNK_421\n", None),
                        ("junk" + S + "\nmore\n" + S + "\n", None),
                        (S + "x\n" + S + "\n", None), ("", None), ("", chunk),
                        ("a\n" + S, None), ("a\r\n" + S + "\r\n", None)]:
            add(fn, [text, S, c], py(text, S, command=c))
    ES = ft._ENCODER_FAILURE_SENTINEL
    for text in ["partial" + ES + "\n", "partial" + ES, ES + "\r\n", ES + "x", "no"]:
        add("failure_sentinel_seen", [text, ES, None], ft._failure_sentinel_seen(text, ES, command=None))

    # ── probe_shell_error ──
    pc = ft._decoder_probe_command()
    words = ["syntax error", "bad substitution", "unterminated", "permission denied"]
    for w in words:
        for variant in (w, w.upper(), w.title(), w.swapcase()):
            for text in (variant, "sh: 1: " + variant + "\n", pc + "\r\n" + variant + "\r\n# "):
                add("probe_shell_error", [text, pc], ft._probe_shell_error(text, pc))
    for text in ["ſyntax error", "ſyntax ERROR", "SYNTAX ERROR", "syntax\terror", "syntaxerror",
                 "permiſsion denied", "permission deniеd", "Kunterminated", "unterminated quote",
                 "bad  substitution", "", "ok\n# ", "unterminate", "Permission Denied"]:
        add("probe_shell_error", [text, pc], ft._probe_shell_error(text, pc))
        full = pc + "\r\n" + text
        add("probe_shell_error", [full, pc], ft._probe_shell_error(full, pc))
    # echo 內含關鍵字（命令本身）
    kw_cmd = "echo syntax error"
    for text in [kw_cmd + "\r\nok\r\n", kw_cmd + "\nsyntax error\n", "X" + kw_cmd, kw_cmd]:
        add("probe_shell_error", [text, kw_cmd], ft._probe_shell_error(text, kw_cmd))

    # ── extract_between_sentinels ──
    B, E = ft._SENTINEL_BEGIN, ft._SENTINEL_END
    pull = ft._pull_command("/tmp/a", "base64")
    bodies2 = [
        "YWJj\r\nZGVm\r\n", "", "YW Jj", "\x1b[32mYWJj\x1b[0m", "YW\x1cJj", "YW\x85Jj", "YW Jj",
        "YW　Jj", "YW\x0bJj\x0c", "YW\x1fJj", "YW Jj", "YW​Jj", "\t\n", "A\x1b[1;2HB",
    ]
    for body in bodies2:
        for text in (f"{B}\n{body}{E}\n", f"{B}\r\n{body}{E}\r\n# ", f"junk{B}{body}{E}junk"):
            add("extract_between_sentinels", [text, None], ft._extract_between_sentinels(text, command=None))
            full = pull + "\r\n" + text
            add("extract_between_sentinels", [full, pull], ft._extract_between_sentinels(full, command=pull))
    cases = [
        f"{B}\nAAAA\n{E}\n{B}\nBBBB\n{E}\n", f"{E}\n{B}\nAAAA\n", f"{E}{B}", f"{B}{E}", f"{B}\n{B}\nX\n{E}",
        f"{B}\nX\n{E}\n{E}\n", f"{B}\nAAAA\n", f"AAAA\n{E}\n", "", f"{B}\nA\n{E}\n{E}\n{B}\n",
        f"{B}\nA\n{B}\nB\n{E}\n",
    ]
    for text in cases:
        add("extract_between_sentinels", [text, None], ft._extract_between_sentinels(text, command=None))
        add("extract_between_sentinels", [pull + "\n" + text, pull], ft._extract_between_sentinels(pull + "\n" + text, command=pull))

    # ── remote_md5_from_output（Python _remote_md5 的解析段）──
    def md5_parse(raw: str, command: str) -> str | None:
        output = ft._strip_command_echo(raw, command)
        if not ft._runtime_sentinel_seen(raw, ft._MD5_SENTINEL, command=command):
            return None
        before = output[: output.find(ft._MD5_SENTINEL)]
        import re
        for line in before.splitlines():
            m = re.match(r"\s*(?:\\)?([0-9a-fA-F]{32})(?:\s|$)", line)
            if m:
                return m.group(1).lower()
        return None

    h = "0123456789abcdef0123456789ABCDEF"
    h2 = "fedcba9876543210fedcba9876543210"
    M = ft._MD5_SENTINEL
    for path in ["/tmp/a", "/tmp/" + h2, "/tmp/with space", "/tmp/a\\b"]:
        mc = ft._md5_command(path)
        outs = [
            f"{h}  {path}\n{M}\n# ", f"{h}  {path}\r\n{M}\r\n# ", f"\\{h}  {path}\n{M}\n",
            f"{h.lower()} *{path}\n{M}\n", f"{h}\n{M}\n", f"{h}", f"  {h}  x\n{M}\n",
            f"md5sum: {path}: No such file\n{M}\n", f"noise\n{h2}  {path}\n{h}  {path}\n{M}\n",
            f"{path}{h2}\n{h}  z\n{M}\n", f"{h}x  {path}\n{M}\n", f"{h}\t{path}\n{M}\n",
            f"{h}  {path}\n", f"{h}  {path}\n# {M}x\n", f"{h[:31]}  {path}\n{M}\n",
            f"{h}{h2}  {path}\n{M}\n", f"\\\\{h}  {path}\n{M}\n", f"{h}  {M}\n{M}\n",
            f"{M}\n{h}  {path}\n", "",
        ]
        for o in outs:
            for raw in (mc + "\r\n" + o, o):
                add("remote_md5_from_output", [raw, mc], md5_parse(raw, mc))
        # sentinel 只在 echo
        raw = mc + "\r\n" + "# "
        add("remote_md5_from_output", [raw, mc], md5_parse(raw, mc))
        raw = mc + "\n" + h + "  x\n"
        add("remote_md5_from_output", [raw, mc], md5_parse(raw, mc))

    # 隨機補充：混合 sentinel 與雜訊，確保 >= 600 列且覆蓋非預期組合
    alphabet = ["\n", "\r\n", "\r", " ", "x", S, M, B, E, "\x1b[0m", "\x85", " ", h, "\\"]
    for _ in range(80):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 9)))
        add("runtime_sentinel_seen", [text, S, None], ft._runtime_sentinel_seen(text, S, command=None))
        add("failure_sentinel_seen", [text, S, None], ft._failure_sentinel_seen(text, S, command=None))
        add("extract_between_sentinels", [text, None], ft._extract_between_sentinels(text, command=None))
        add("remote_md5_from_output", [text, ""], md5_parse(text, ""))
        add("probe_shell_error", [text, ""], ft._probe_shell_error(text, ""))

    out = Path(opts.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=True, separators=(",", ":")) + "\n")
    print(f"wrote {len(rows)} rows -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
