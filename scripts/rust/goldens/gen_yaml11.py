#!/usr/bin/env python3
"""產生 ``contracts/golden/yaml11.tsv``（P3-T6 的 Python oracle：PyYAML ``safe_load``）。

oracle 為 PyYAML SafeLoader，基準版本 6.0.3（其他版本輸出不保證相同，會發出警告）。
產生器可 import PyYAML，但不得 import sw_core（golden 產生器例外，因其在擷取 Python 基線）。
輸出為確定性：固定 seed、固定語料（含出貨 ``default.yaml`` 全文）、依序寫出。

編碼 ``tag()`` 與 ``sw-proto::yaml11::to_tagged_json`` 逐一對應：

- None→``{"t":"null"}``；bool→``bool``；int→``int``（十進位字串）；float→``float``（``repr``）；
- str→``str``；date／datetime→``ts``（``str()``）；bytes→``bin``（小寫 hex）；
- ``!!set``→``set``（以 YAML 事件序排列，不使用 Python set 的迭代序）；
- dict→``map``（插入序，``[[k,v],...]``）；list／tuple（``!!omap`` 的 pair）→``seq``。

任何 YAML／constructor 例外皆記為 ``{"t":"error"}``（錯誤訊息非契約，見 plan G5）。
輸入語料禁止產生遞迴結構（例如 ``&x [*x]``），因為 ``tag()`` 無法編碼循環參照。

用法::

    python3 scripts/rust/goldens/gen_yaml11.py --out contracts/golden/yaml11.tsv
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import random
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import yaml  # noqa: E402  PyYAML 為本產生器唯一的第三方依賴
from _tsv import write_tsv  # noqa: E402

REPO = _DIR.parents[2]
BASELINE_PYYAML = "6.0.3"
SEED = 20261016
MIN_ROWS = 400
DEFAULT_YAML = REPO / "sw_core" / "assets" / "profiles" / "default.yaml"
ERROR_JSON = '{"t":"error"}'


class _OrderedSet(list):
    """``!!set`` 的標記型別：元素為 YAML 事件序（dict 插入序），不是 Python set。"""


class _Loader(yaml.SafeLoader):
    """與 ``yaml.SafeLoader`` 相同，唯一差異是 ``!!set`` 保留事件序。"""


def _construct_set(loader, node):
    mapping = loader.construct_mapping(node)  # dict：key 依事件序、重複 key 保留首位
    return _OrderedSet(mapping.keys())


_Loader.add_constructor("tag:yaml.org,2002:set", _construct_set)


def tag(x):
    """與 ``sw-proto::yaml11::to_tagged_json`` 逐一對應的型別標記編碼。"""
    if x is None:
        return {"t": "null"}
    if isinstance(x, bool):
        return {"t": "bool", "v": x}
    if isinstance(x, int):
        return {"t": "int", "v": str(x)}
    if isinstance(x, float):
        return {"t": "float", "v": repr(x)}
    if isinstance(x, str):
        return {"t": "str", "v": x}
    if isinstance(x, (datetime.date, datetime.datetime)):
        return {"t": "ts", "v": str(x)}
    if isinstance(x, (bytes, bytearray)):
        return {"t": "bin", "v": bytes(x).hex()}
    if isinstance(x, _OrderedSet):
        return {"t": "set", "v": [tag(e) for e in x]}
    if isinstance(x, dict):
        return {"t": "map", "v": [[tag(k), tag(v)] for k, v in x.items()]}
    if isinstance(x, (list, tuple)):
        return {"t": "seq", "v": [tag(e) for e in x]}
    raise TypeError(f"無法編碼的型別：{type(x).__name__}")


def expected_for(text: str) -> str:
    try:
        value = yaml.load(text, Loader=_Loader)  # 等同 yaml.safe_load（單文件），僅 !!set 改為事件序
    except Exception:  # noqa: BLE001  任何 YAML／constructor 例外皆為錯誤
        return ERROR_JSON
    # tag() 在此之外呼叫：編碼失敗代表產生器本身有缺陷，必須讓它直接崩潰。
    return json.dumps(tag(value), ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 1. 明確語料（依 YAML 1.1 規則分類；每一列的期望值由 PyYAML 產生，不手寫）
# ---------------------------------------------------------------------------

EXPLICIT = [
    # 1.1 bool／null（PyYAML 只認 yes/no/true/false/on/off 的 Title/UPPER/lower；y/n 不是 bool）
    "yes", "Yes", "YES", "no", "No", "NO", "true", "True", "TRUE", "false", "False", "FALSE",
    "on", "On", "ON", "off", "Off", "OFF", "y", "Y", "n", "N", "t", "f", "yEs", "tRUE", "oN",
    "~", "null", "Null", "NULL", "nULL", "", "   ", "\n", "# only comment\n", "---\n", "...\n", "---\n...\n",
    "a: yes\nb: y\nc: n\nd: ~\ne:\nf: null\ng: off\n",
    "[yes, no, on, off, y, n, ~, ]", "{a: , b: ~, c: null, d: Null}", "- ~\n- null\n-\n",
    # 1.2 整數（十進位、0x、0b、0NNN 八進位；0o 不是 int）
    "0", "-0", "+0", "0_", "00", "007", "017", "08", "0o17", "0O17", "0x1A", "0X1A", "0x1a",
    "-0x1A", "+0x1A", "0xg", "0b101", "-0b101", "0b", "0b102", "1_000", "1__0", "_1", "1_", "+1_000",
    "-1_0_0", "12345678901234567890123", "-12345678901234567890123",
    # 1.3 六十進位（sexagesimal）
    "190:20:30", "-190:20:30", "+190:20:30", "1:30", "0:30", "1:60", "1:5:9", "1:00",
    # 1.4 浮點（implicit 需要小數點；指數必須帶正負號）
    "1.0", "1.", ".5", "-.5", "+.5", "-1.", "0.", "-0.0", "-0.", "0.0", "3.14159", "1.0e+3", "1.0e3",
    "1e3", "1.0e-5", "1.0E+3", "1_000.5_0", "1.5_0e+1_0", "1_0.5_0e+1_0", "6.02e+23", "1e-5",
    ".inf", "-.inf", "+.Inf", ".INF", "-.Inf", ".nan", ".NaN", ".NAN", "-.nan", ".infx", "1e400", "-1e400",
    "190:20:30.5", "1:30.25", "-1:30.25", "00.5", "1.2.3",
    # 1.5 時間戳（implicit 與 constructor 規則；非法日期須為錯誤）
    "2001-12-14", "2002-12-14", "2001-1-4", "2001-12-14t21:59:43.10-05:00", "2001-12-14T21:59:43Z",
    "2001-12-14 21:59:43.10 -5", "2001-12-14\t21:59:43.123456789+09:30", "2001-12-14 21:59:43",
    "2001-12-14 9:59:43.1", "2001-12-14 21:59:43.10 +05:30", "2001-12-14 21:59:43+0530",
    "2001-12-14 21:59:43.1234567", "2001-02-30", "2001-12-14 25:00:00", "2001-12-14 21:59:43 UTC",
    "2001-12-14 21:59:43+09:00", "2001-12-14 21:59:43.5Z", "2001-12-14T00:00:00.000000-00:30",
    # 1.6 引號純量（一律 str）與跳脫
    "'quoted'", '"dq"', "'it''s'", '"esc\\tx"', '"\\x41\\u00e9\\U0001F600"', "'yes'", '"null"', "'~'",
    '"1"', "'0x1A'", '"\\N\\_\\ \\/"', '""', "''", "'a\n  b'", '"a\\\n  b"', "'  lead'", '"tab\\there"',
    '"x\\"y"', "'x\\y'", '"\\0\\a\\b\\e\\v\\f"', '"\\u00e9\\u0301"',
    # 1.7 block scalar 與多行純量
    "a: |\n  line1\n  line2\n", "a: |-\n  x\n", "a: |+\n  x\n\n", "a: >\n  folded\n  text\n\n  para\n",
    "a: >-\n  x\n   indented\n  y\n", "a: |2\n   two\n", "a: |\n\n  lead\n", "a: |\n  keep\n\nb: 1\n",
    "a: x\n  y\n", "key: first\n\n  second\n", "a: b #c\n", "a: b#c\n", "a: 'b' # c\n", 'a: "x" "y"\n',
    # 1.8 anchor／alias／merge key（<<：先合併、本身的 key 覆寫；序列 merge 前者優先）
    "a: &x [1, 2]\nb: *x\n", "a: &x {k: v}\nb:\n  <<: *x\n  k2: w\n",
    "base: &b {x: 1, y: 2}\nd:\n  <<: *b\n  y: 3\n",
    "a: &a {p: 1, q: 2}\nb: &b {q: 9, r: 3}\nd:\n  <<: [*a, *b]\n  r: 4\n",
    "<<: 1\n", '"<<": 1\n', "a: &x 1\nb: *x\n", "a: *undefined\n", "a: &x 1\na: &x 2\n", "= : 1\n",
    # 1.9 重複 key 與 Python 相等（1 == 1.0 == True；同一 .nan 物件視為同 key）
    "a: 1\na: 2\n", "1: a\ntrue: b\n", "1: a\n1.0: b\n", "1: a\n1.0: b\nTrue: c\n", "1: a\n'1': b\n",
    "~: a\nnull: b\n: c\n", "2001-12-14: x\n", "!!binary aGk=: x\n", ".nan: a\n.nan: b\n", "{a: 1, a: 2}",
    "? a\n: b\n", "? [a]\n: b\n", "? {a: 1}\n: b\n", "? !!set {a: }\n: b\n",
    # 1.10 結構與錯誤
    "- a\n- b\n", "[a, [b, c], {d: e}]", "{}", "[]", "{a: }", "a: {b: [1, {c: 2}]}\n", "- - a\n  - b\n",
    "a:\n  - 1\n  - 2\n", "a: 1\n---\nb: 2\n", "--- a\n--- b\n", "---\na: 1\n...\n", "\ta: 1\n", "a:\n\tb: 1\n",
    "a: 1\n b: 2\n", "a: [1, 2\n", "- a\n b: c\n", "a: - b\n", "key: value: x\n", "%YAML 1.1\n---\na: 1\n",
    "%TAG !e! tag:example.com,2000:\n---\na: !e!foo 1\n", 'a: "unterminated\n', "a: @x\n",
    # 1.11 顯式 tag
    "!!set {a, b, a}", "!!set {b: , a: }", "!!set {1, 1.0}", "!!omap [a: 1, b: 2]", "!!omap [a: 1, a: 2]",
    "!!omap [a: 1, b]", "!!omap [{a: 1, b: 2}]", "!!str 123", "!!str ~", "!!str [1]", "!!int '0x1A'",
    "!!int 1_000", "!!int 190:20:30", "!!int abc", "!!int 3.5", "!!int 0b11", "!!float 1e3", "!!float .inf",
    "!!float abc", "!!float 5", "!!bool yes", "!!bool true", "!!bool y", "!!null foo", "!!null",
    "!!seq [1, 2]", "!!seq {a: 1}", "!!map {a: 1}", "!!map [1]", "!!binary aGVsbG8=",
    "!!binary |\n  aGVs\n  bG8=\n", "!!binary MTI", "!!binary MTIz!", "!!binary ''",
    "!!timestamp 2001-12-14", "!!timestamp foo", "!foo bar", "!!python/str x", "!<tag:yaml.org,2002:int> 5",
    # 1.12 Unicode
    "ключ: значение", "é: ü", 'a: "\\u00e9"', "emoji: 😀", "a: 'é'",
    # 1.13 結構組合
    "a: 1\nb: [x, y]\nc: {d: e}\n", "- yes\n- 1_0\n- 0x10\n- 2001-12-14\n- .inf\n",
]

# ---------------------------------------------------------------------------
# 2. 隨機語料（固定 seed）與突變語料（產生錯誤與邊界）
# ---------------------------------------------------------------------------

_WORDS = ["alpha", "beta", "x", "Gamma", "root@prplOS", "a-b", "$^", "é", "ü", "sh", "k1", "k2", "login:x"]
_KEYS = ["k1", "1", "1.0", "true", "~", "2001-12-14", "'q'", "x", ".nan", "yes"]
_MUT_SEEDS = [
    "a: 1\nb: [x, y]\n",
    "k: {x: 1.5, y: 2001-12-14}\n",
    "- yes\n- 0x1A\n- 'q'\n",
    "a: |\n  block\n",
    "!!set {a, b}",
]
_MUT_CHARS = "{}[],:&*!|>'\"\t #-?%@`\n"


def _rand_int(rng: random.Random) -> str:
    sign = rng.choice(["", "", "-", "+"])
    k = rng.randrange(6)
    if k == 0:
        body = str(rng.randint(0, 10 ** rng.randint(1, 25)))
    elif k == 1:
        body = "0x" + "".join(rng.choice("0123456789abcdefABCDEF_") for _ in range(rng.randint(1, 8)))
    elif k == 2:
        body = "0b" + "".join(rng.choice("01_") for _ in range(rng.randint(1, 10)))
    elif k == 3:
        body = "0" + "".join(rng.choice("01234567_") for _ in range(rng.randint(1, 6)))
    elif k == 4:
        body = str(rng.randint(1, 999)) + "".join(f":{rng.randint(0, 59):02d}" for _ in range(rng.randint(1, 2)))
    else:
        body = "".join(rng.choice("0123456789_") for _ in range(rng.randint(1, 8)))
    return sign + body


def _rand_float(rng: random.Random) -> str:
    sign = rng.choice(["", "", "-", "+"])
    k = rng.randrange(6)
    if k == 0:
        body = f"{rng.randint(0, 999)}.{rng.randint(0, 99999)}"
    elif k == 1:
        body = f"{rng.randint(0, 9)}.{rng.randint(0, 9)}e{rng.choice('+-')}{rng.randint(0, 30)}"
    elif k == 2:
        body = f".{rng.randint(0, 9999)}"
    elif k == 3:
        body = rng.choice([".inf", ".Inf", ".INF", ".nan", ".NaN"])
    elif k == 4:
        body = f"{rng.randint(0, 59)}:{rng.randint(0, 59):02d}.{rng.randint(0, 99)}"
    else:
        body = f"{rng.randint(0, 9)}_{rng.randint(0, 9)}{rng.randint(0, 9)}.{rng.randint(0, 9)}"
    return sign + body


def _rand_ts(rng: random.Random) -> str:
    date = f"{rng.randint(1990, 2030):04d}-{rng.randint(1, 12):02d}-{rng.randint(1, 31):02d}"
    if rng.random() < 0.4:
        return date
    sep = rng.choice(["T", "t", " ", "\t", " \t"])
    clock = f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"
    if rng.random() < 0.5:
        clock += "." + "".join(str(rng.randint(0, 9)) for _ in range(rng.randint(1, 9)))
    tz = rng.choice(["", "Z", "+09:30", "-05:00", " -5", " +05", "+0530", "-00:30"])
    return date + sep + clock + tz


def _rand_str(rng: random.Random) -> str:
    w = rng.choice(_WORDS)
    k = rng.randrange(3)
    if k == 0:
        return w
    if k == 1:
        return "'" + w.replace("'", "''") + "'"
    return '"' + w.replace('"', '\\"') + '"'


def _rand_scalar(rng: random.Random) -> str:
    k = rng.randrange(9)
    if k == 0:
        return _rand_int(rng)
    if k == 1:
        return _rand_float(rng)
    if k == 2:
        return _rand_ts(rng)
    if k == 3:
        return _rand_str(rng)
    if k == 4:
        return rng.choice(["yes", "No", "on", "OFF", "y", "n", "~", "null", "0o17", "1:30", ".NaN"])
    if k == 5:
        raw = bytes(rng.randrange(256) for _ in range(rng.randint(0, 6)))
        return "!!binary " + base64.b64encode(raw).decode("ascii")
    if k == 6:
        return "!!set {" + ", ".join(rng.choice(_WORDS) for _ in range(rng.randint(1, 3))) + "}"
    if k == 7:
        return "!!timestamp " + _rand_ts(rng)
    return "!!int " + _rand_int(rng)


def _rand_flow(rng: random.Random, depth: int = 0) -> str:
    r = rng.random()
    if depth >= 2 or r < 0.45:
        return _rand_scalar(rng)
    if r < 0.7:
        return "[" + ", ".join(_rand_flow(rng, depth + 1) for _ in range(rng.randint(0, 3))) + "]"
    return "{" + ", ".join(f"{rng.choice(_KEYS)}: {_rand_flow(rng, depth + 1)}" for _ in range(rng.randint(0, 3))) + "}"


def _rand_doc(rng: random.Random) -> str:
    if rng.random() < 0.5:
        return _rand_flow(rng, 0)
    lines = [f"{rng.choice(_KEYS)}: {_rand_flow(rng, 1)}" for _ in range(rng.randint(1, 4))]
    return "\n".join(lines) + "\n"


def _mutate(rng: random.Random, s: str) -> str:
    i = rng.randrange(len(s) + 1)
    if s and rng.random() < 0.5:
        j = min(i, len(s) - 1)
        return s[:j] + s[j + 1 :]
    return s[:i] + rng.choice(_MUT_CHARS) + s[i:]


def build_rows() -> list[tuple[str, str, str, str]]:
    rng = random.Random(SEED)
    texts: list[str] = list(EXPLICIT)
    texts.append(DEFAULT_YAML.read_text(encoding="utf-8"))
    texts.extend(_rand_doc(rng) for _ in range(220))
    texts.extend(_mutate(rng, rng.choice(_MUT_SEEDS)) for _ in range(80))
    rows = [(f"y{i:04d}", "safe_load", text, expected_for(text)) for i, text in enumerate(texts)]
    if len(rows) < MIN_ROWS:
        raise SystemExit(f"語料不足：{len(rows)} < {MIN_ROWS}")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="產生 yaml11 golden TSV（PyYAML safe_load 為 oracle）")
    ap.add_argument("--out", required=True, help="輸出 TSV 路徑")
    args = ap.parse_args(argv)
    if yaml.__version__ != BASELINE_PYYAML:
        print(
            f"warning: golden 基準為 PyYAML {BASELINE_PYYAML}，目前 {yaml.__version__}；輸出可能與 contracts/golden 不同",
            file=sys.stderr,
        )
    rows = build_rows()
    write_tsv(args.out, rows)
    print(f"yaml11 golden：{len(rows)} 列", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
