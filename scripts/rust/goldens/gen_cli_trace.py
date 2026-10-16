#!/usr/bin/env python3
"""產生 ``contracts/golden/cli_trace.tsv``（P3-T11 的 cli_trace 基準）。

呼叫 ``sw_core.cli_trace`` 產生 golden TSV。
輸出格式：
每行 ``<case_id>\\t<op>\\t<hex(input UTF-8)>\\t<hex(expected UTF-8)>\\n``

涵蓋四個操作：
1. ``parse_log_level``：日誌層級字串解析（>= 60 組）
2. ``resolve_cli_trace_level``：依 verbose 與環境變數決定層級
3. ``trace_enabled``：依層級判定是否輸出 trace（對齊 isEnabledFor(INFO)）
4. ``describe_trace_endpoint``：endpoint 摘要描述（>= 30 組）

用法::

    python3 scripts/rust/goldens/gen_cli_trace.py [--out contracts/golden/cli_trace.tsv]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sw_core.cli_trace as ct
from _tsv import Row, write_tsv

PARSE_LOG_LEVEL_INPUTS: list[str | None] = [
    None,
    "",
    "   ",
    "\t",
    "\n",
    "\r\n",
    "info",
    "INFO",
    "Info",
    "  info  ",
    " Warn ",
    "warn",
    "WARN",
    "warning",
    "WARNING",
    "  WARNING  ",
    "fatal",
    "FATAL",
    "critical",
    "CRITICAL",
    "error",
    "ERROR",
    "debug",
    "DEBUG",
    "notset",
    "NOTSET",
    "0",
    "00",
    "1",
    "10",
    "20",
    "21",
    "30",
    "40",
    "50",
    "100",
    "-1",
    "-5",
    "-10",
    "-20",
    "-100",
    "-0",
    "--5",
    "---1",
    "+10",
    " 20 ",
    " -5 ",
    "²",
    "³",
    "¹",
    "٠",
    "abc",
    "xyz",
    "LEVEL",
    "123a",
    "1" * 4299,
    "1" * 4300,
    "1" * 4301,
    "9" * 4300,
    "9" * 4301,
    "-1" + "0" * 4299,
    "-1" + "0" * 4300,
    "None",
    "true",
    "false",
]

RESOLVE_ENV_VALUES: list[str | None] = [
    None,
    "",
    "DEBUG",
    "debug",
    "INFO",
    "info",
    "WARNING",
    "WARN",
    "ERROR",
    "CRITICAL",
    "FATAL",
    "NOTSET",
    "10",
    "20",
    "21",
    "30",
    "0",
    "-5",
    "--5",
    "invalid",
    "1" * 4300,
    "1" * 4301,
]

TRACE_ENABLED_LEVELS: list[int] = [
    0,
    -1,
    -5,
    -10,
    -20,
    -50,
    -100,
    1,
    5,
    10,
    15,
    19,
    20,
    21,
    25,
    30,
    40,
    50,
    100,
    int("1" * 4300),
    -int("1" * 4300),
]

DESCRIBE_ENDPOINT_INPUTS: list[str] = [
    "/run/serialwrap/serialwrap.sock",
    "/nonexistent/x.sock",
    "/tmp/test.sock",
    "unix:///tmp/test.sock",
    "unix:///run/serialwrap/serialwrap.sock",
    "unix://localhost/tmp/sock",
    "/path/with spaces/sock",
    "/path/with-dashes_and.dots/s.sock",
    "/tmp/utf8_路徑_test.sock",
    "/tmp/café_test.sock",
    "tcp://127.0.0.1:1",
    "tcp://127.0.0.1:48700",
    "tcp://127.0.0.1:65535",
    "tcp://127.0.0.1:8080",
    "tcp://localhost:48700",
    "tcp://localhost:1",
    "tcp://LOCALHOST:5",
    "tcp://LOCALHOST:48700",
    "tcp://[::1]:5",
    "tcp://[::1]:48700",
    "tcp://[::1]:65535",
    "tcp://127.0.0.1:0",
    "tcp://192.168.1.1:48700",
    "tcp://10.0.0.1:80",
    "tcp://172.16.0.1:9000",
    "tcp://example.com:48700",
    "tcp://non-loopback.local:1234",
    "tcp://0.0.0.0:48700",
    "tcp://127.0.0.2:48700",
    "invalid_endpoint_no_scheme",
    "unknown://foo/bar",
    "http://localhost:80",
    "file:///tmp/sock",
    "tcp://:80",
    "tcp://invalid",
    "tcp://[::1",
    "tcp://127.0.0.1:65536",
    "",
    "   ",
]


def generate_rows() -> list[Row]:
    rows: list[Row] = []

    # 1. parse_log_level
    for i, raw in enumerate(PARSE_LOG_LEVEL_INPUTS, start=1):
        case_id = f"parse_level_{i:03d}"
        res = ct._parse_log_level(raw)
        input_str = "__NONE__" if raw is None else f"str:{raw}"
        expected_str = "none" if res is None else f"some:{res}"
        rows.append((case_id, "parse_log_level", input_str, expected_str))

    # 2. resolve_cli_trace_level
    case_idx = 1
    for verbose in (0, 1, 2, 3):
        for env_val in RESOLVE_ENV_VALUES:
            case_id = f"resolve_level_{case_idx:03d}"
            case_idx += 1
            env_dict = {"SERIALWRAP_LOG_LEVEL": env_val} if env_val is not None else {}
            res = ct.resolve_cli_trace_level(verbose, env=env_dict)
            input_str = f"{verbose}\x1f{env_val if env_val is not None else '__NONE__'}"
            expected_str = str(res)
            rows.append((case_id, "resolve_cli_trace_level", input_str, expected_str))

    # 3. trace_enabled (對齊 Python isEnabledFor(INFO))
    for i, level in enumerate(TRACE_ENABLED_LEVELS, start=1):
        case_id = f"trace_enabled_{i:03d}"
        logger = logging.getLogger(f"serialwrap.cli_trace.test.{i}")
        logger.propagate = False
        logger.setLevel(level)
        enabled = logger.isEnabledFor(logging.INFO)
        input_str = str(level)
        expected_str = "true" if enabled else "false"
        rows.append((case_id, "trace_enabled", input_str, expected_str))

    # 4. describe_trace_endpoint
    for i, ep in enumerate(DESCRIBE_ENDPOINT_INPUTS, start=1):
        case_id = f"describe_ep_{i:03d}"
        transport, ep_id = ct.describe_trace_endpoint(ep)
        input_str = ep
        expected_str = f"{transport}\x1f{ep_id}"
        rows.append((case_id, "describe_trace_endpoint", input_str, expected_str))

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "contracts/golden/cli_trace.tsv",
        help="輸出路徑（預設 contracts/golden/cli_trace.tsv）",
    )
    args = parser.parse_args()

    rows = generate_rows()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_tsv(args.out, rows)
    print(f"Wrote {len(rows)} cases to {args.out}")


if __name__ == "__main__":
    main()
