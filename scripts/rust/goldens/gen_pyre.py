#!/usr/bin/env python3
"""產生 ``contracts/golden/pyre.tsv``（P3-T5 的 Python oracle），並可選擇寫出 ``pyre.limits.json``。

oracle 為 CPython 標準函式庫 ``re``（``compile`` 與 ``search``），以及 ``sw_core.command_completion.valid_completion_regex``
（``completion_valid`` 列）。本產生器屬 plan §0.5 所列的例外：為擷取 Python 基線而 import ``sw_core``；
其他 ``sw-proto`` golden 產生器仍不得 import ``sw_core``。基準版本 CPython 3.12.3（CI 釘版）。

輸出 op（``input`` 欄位以 hex 編碼）：

- ``compile``：``ok`` ／ ``err:<str(re.error)>`` ／ ``recursion`` ／ ``overflow``（``input`` 僅為 pattern）。
- ``search``：``none`` 或 ``<start>,<end>``（code point 索引）；``input`` 為 ``pattern + \\x1f + haystack``；
  只對 Python 可編譯的 pattern 產生。
- ``completion_valid``：``1`` ／ ``0`` ／ ``crash``（``OverflowError`` 未被 ``valid_completion_regex`` 攔截，記 ``crash``）。

巢狀深度（``PARSE_DEPTH_LIMIT``）：以二分搜尋找出 ``re.compile("("*n + ")"*n)`` 開始 ``RecursionError`` 的最小 n＝T，
寫入 ``--limits-out``。golden 只收 n ∈ {50, 100, T−20}（必須 ``ok``）與 {T+20, 2T}（必須 ``recursion``），
避開呼叫端堆疊深度差異造成的邊界抖動。

語料格式（``pyre_corpus.txt``）：每行一個 pattern；行首 ``#`` 為註解；可選的 TAB 之後為 haystack 清單，以 ``<SEP>`` 分隔。
欄位原文即 UTF-8 文字（反斜線照字面，不做跳脫）；控制字元與不便書寫的字元以 ``<U+XXXX>`` 表示；空字串以 ``<EMPTY>`` 表示。

用法::

    python3 scripts/rust/goldens/gen_pyre.py --out contracts/golden/pyre.tsv --limits-out contracts/golden/pyre.limits.json
    python3 scripts/rust/goldens/gen_pyre.py --corpus scripts/rust/goldens/pyre_corpus.txt --out <tmp>/pyre.tsv
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_REPO = _DIR.parents[2]
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from _tsv import Row, write_tsv  # noqa: E402

from sw_core.command_completion import valid_completion_regex  # noqa: E402

BASELINE = (3, 12, 3)
CORPUS_DEFAULT = _DIR / "pyre_corpus.txt"
SEP = "<SEP>"
EMPTY = "<EMPTY>"
_TOKEN = re.compile(r"<U\+([0-9A-Fa-f]{4,6})>")
_BISECT_HI = 100_000


def decode(field: str) -> str:
    """語料欄位解碼：``<EMPTY>`` 為空字串，``<U+XXXX>`` 為單一 code point（拒絕 surrogate）。"""
    if field == EMPTY:
        return ""
    if EMPTY in field or SEP in field:
        raise ValueError(f"欄位不得同時含 {EMPTY}／{SEP} 與其他內容：{field!r}")

    def _sub(m: re.Match[str]) -> str:
        cp = int(m.group(1), 16)
        if 0xD800 <= cp <= 0xDFFF or cp > 0x10FFFF:
            raise ValueError(f"不合法的 code point：{m.group(0)}")
        return chr(cp)

    return _TOKEN.sub(_sub, field)


def read_corpus(path: Path) -> list[tuple[str, str, list[str]]]:
    """回傳 ``(entry_id, pattern, haystacks)``；entry_id 為 1 起算的語料序號（僅計入非註解、非空行）。"""
    raw_text = path.read_bytes().decode("utf-8")
    if "\r" in raw_text:
        raise ValueError("pyre_corpus.txt 必須為 LF 換行")
    entries: list[tuple[str, str, list[str]]] = []
    for raw in raw_text.split("\n"):
        if raw == "" or raw.startswith("#"):
            continue
        pat_field, tab, hay_field = raw.partition("\t")
        hays = [decode(h) for h in hay_field.split(SEP)] if (tab and hay_field) else []
        entries.append((f"p{len(entries) + 1:04d}", decode(pat_field), hays))
    return entries


def compile_op(pat: str) -> str:
    try:
        re.compile(pat)
    except re.error as e:
        return f"err:{e}"
    except RecursionError:
        return "recursion"
    except OverflowError:
        return "overflow"
    return "ok"


def search_op(pat: str, hay: str) -> str:
    m = re.compile(pat).search(hay)
    return "none" if m is None else f"{m.start()},{m.end()}"


def completion_op(pat: str) -> str:
    try:
        ok = valid_completion_regex(pat)
    except OverflowError:
        return "crash"
    return "1" if ok else "0"


def _recursion_at(n: int) -> bool:
    try:
        re.compile("(" * n + ")" * n)
    except RecursionError:
        return True
    except re.error:
        return False
    return False


def measure_parse_depth_limit() -> int:
    """二分搜尋 ``RecursionError`` 的最小巢狀深度 T（單調：n 越大越易遞迴錯）。"""
    lo, hi = 1, _BISECT_HI
    if _recursion_at(lo) or not _recursion_at(hi):
        raise SystemExit("深度二分搜尋的前提不成立（lo 不應遞迴錯、hi 應遞迴錯）")
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _recursion_at(mid):
            hi = mid
        else:
            lo = mid
    return hi


def _nest(n: int) -> str:
    return "(" * n + ")" * n


def build_rows(corpus: Path) -> tuple[list[Row], int]:
    rows: list[Row] = []
    for cid, pat, hays in read_corpus(corpus):
        c = compile_op(pat)
        rows.append((cid, "compile", pat, c))
        if c == "ok":
            for k, hay in enumerate(hays):
                rows.append((f"{cid}.{k}", "search", pat + "\x1f" + hay, search_op(pat, hay)))
        rows.append((cid, "completion_valid", pat, completion_op(pat)))

    t = measure_parse_depth_limit()
    # 深度區段：T−20 必須成功、T+20 與 2T 必須遞迴錯（與 Rust 端的 PARSE_DEPTH_LIMIT 比對時留出 40 層緩衝）。
    for n, want in ((50, "ok"), (100, "ok"), (t - 20, "ok"), (t + 20, "recursion"), (2 * t, "recursion")):
        pat = _nest(n)
        got = compile_op(pat)
        if got != want:
            raise SystemExit(f"深度 {n} 預期 {want}、實得 {got}，golden 前提不成立")
        rows.append((f"dep{n}", "compile", pat, got))
        rows.append((f"dep{n}", "completion_valid", pat, completion_op(pat)))

    # completion 長度邊界：1024 bytes 為有效上限、1025 bytes 直接 Invalid（不論 regex 內容）。
    boundary = [
        ("len_ascii1024", "x" * 1024),
        ("len_ascii1025", "x" * 1025),
        ("len_mb1024", "é" * 512),
        ("len_mb1025", "x" * 1023 + "é"),
    ]
    for cid, pat in boundary:
        assert len(pat.encode("utf-8")) in (1024, 1025)
        rows.append((cid, "compile", pat, compile_op(pat)))
        rows.append((cid, "completion_valid", pat, completion_op(pat)))
    return rows, t


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="產生 pyre golden TSV（CPython re／valid_completion_regex 為 oracle）")
    ap.add_argument("--corpus", type=Path, default=CORPUS_DEFAULT, help="語料檔（預設與本腳本同目錄的 pyre_corpus.txt）")
    ap.add_argument("--out", type=Path, required=True, help="輸出 TSV 路徑")
    ap.add_argument("--limits-out", type=Path, default=None, help="可選：輸出 pyre.limits.json（含 PARSE_DEPTH_LIMIT＝T）")
    args = ap.parse_args(argv)
    if sys.version_info[:3] != BASELINE:
        print(
            f"warning: golden 基準為 CPython {'.'.join(map(str, BASELINE))}，"
            f"目前 {sys.version.split()[0]}；輸出可能與 contracts/golden 不同",
            file=sys.stderr,
        )
    rows, t = build_rows(args.corpus)
    write_tsv(args.out, rows)
    if args.limits_out is not None:
        payload = {
            "baseline": ".".join(map(str, BASELINE)),
            "parse_depth_limit": t,
            "parse_depth_limit_source": "gen_pyre.py 二分搜尋 re.compile('('*n + ')'*n) 首次 RecursionError 的最小 n",
        }
        args.limits_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
