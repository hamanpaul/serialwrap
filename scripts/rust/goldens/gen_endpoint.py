#!/usr/bin/env python3
"""產生 ``contracts/golden/endpoint.tsv``（P3-T7 的 endpoint 解析基準）。

呼叫 ``sw_core.client._parse_endpoint`` 產生 golden TSV。
輸出格式：
- 成功 unix: ``ok:unix:<path>``
- 成功 tcp: ``ok:tcp:<host>:<port>``
- 失敗: ``err:<str(ValueError)>``

語料至少 120 筆，涵蓋所有規格與邊界案例。

用法::

    python3 scripts/rust/goldens/gen_endpoint.py [--out contracts/golden/endpoint.tsv]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import sw_core.client as client
from _tsv import Row, write_tsv

CORPUS = [
    # 規格明列案例（plan line 1542）
    "tcp://127.0.0.1:7777",
    "tcp://LOCALHOST:1",
    "tcp://[::1]:5",
    "tcp://[::1",
    "tcp://h:1/",
    "tcp://h:",
    "tcp://h",
    "tcp://:5",
    "tcp://h:65536",
    "tcp://h:0",
    "tcp://h:-1",
    "tcp://h:１",
    "tcp://u@h:5",
    "tcp://h:5?x",
    "tcp://h:5#f",
    "TCP://h:5",
    " tcp://h:5",
    "tcp://h:5\t",
    "unix:///a/b",
    "unix://host/a",
    "unix:relative",
    "unix:///",
    "unix://",
    "foo://x",
    "/abs",
    "rel/sock",
    "",
    "a://b://c",

    # 更多 TCP 案例
    "tcp://127.0.0.1:48700",
    "tcp://127.0.0.1:80",
    "tcp://127.0.0.1:65535",
    "tcp://localhost:48700",
    "tcp://LocalHost:8080",
    "tcp://LOCALHOST:65535",
    "tcp://0.0.0.0:1234",
    "tcp://192.168.1.100:22",
    "tcp://10.0.0.1:8000",
    "tcp://[::1]:48700",
    "tcp://[::]:8080",
    "tcp://[fe80::1]:1024",
    "tcp://[2001:db8::1]:443",
    "tcp://[::ffff:192.0.2.128]:80",
    "tcp://my.domain.com:48700",
    "tcp://sub.domain.example.org:9999",
    "tcp://user:pass@127.0.0.1:8080",
    "tcp://u:p@host:1234",
    "tcp://u@127.0.0.1:8080",
    "tcp://host:1234/",
    "tcp://host:1234/path",
    "tcp://host:1234/path/to/resource",
    "tcp://host:1234?query=1",
    "tcp://host:1234?q=1&v=2",
    "tcp://host:1234#section",
    "tcp://host:1234?q=1#sec",
    "tcp://host:1234/?q=1#sec",
    "tcp://127.0.0.1:not_a_port",
    "tcp://127.0.0.1:999999",
    "tcp://127.0.0.1:65536",
    "tcp://127.0.0.1:-10",
    "tcp://127.0.0.1:0080",
    "tcp://127.0.0.1:0",
    "tcp://127.0.0.1: 80",
    "tcp://127.0.0.1:80 ",
    "tcp://:8080",
    "tcp://:",
    "tcp://",
    "tcp:///",
    "tcp://[::1]",
    "tcp://[::1]:",
    "tcp://[::1]:abc",
    "tcp://[::1]:65536",
    "tcp://[invalid_ipv6]:80",
    "tcp://[::1]:80/path",
    "tcp://[::1]:80?q=1",
    "tcp://[::1]:80#frag",
    "TCP://127.0.0.1:48700",
    "Tcp://LocalHost:1234",
    "tCp://[::1]:80",
    "  tcp://localhost:80",
    "\ttcp://localhost:80",
    "\ntcp://localhost:80",
    "tcp://localhost:80  ",
    "tcp://localhost:80\t",
    "tcp://localhost:80\n",
    "tcp://localhost:80\r\n",

    # 更多 Unix 案例
    "unix:///var/run/serialwrap/serialwrapd.sock",
    "unix:///tmp/test.sock",
    "unix:///tmp/a/b/c/d/e.sock",
    "unix:///path with spaces/sock",
    "unix:///sock",
    "unix://localhost/tmp/sock",
    "unix://127.0.0.1/tmp/sock",
    "unix:///tmp/sock?query=1",
    "unix:///tmp/sock#frag",
    "unix:///tmp/sock?query=1#frag",
    "UNIX:///tmp/sock",
    "Unix:///tmp/sock",
    "  unix:///tmp/sock",
    "unix:///tmp/sock  ",
    "unix:///tmp/sock\n",
    "unix:///tmp/sock\t",
    "unix://relative/path",
    "unix:relative/path",
    "unix:/not_three_slashes",
    "unix:///",
    "unix://",
    "unix:",

    # 裸路徑（相容舊行為）
    "/var/run/serialwrap/serialwrapd.sock",
    "/tmp/test.sock",
    "/tmp/a/b/c.sock",
    "/run/user/1000/serialwrap/serialwrapd.sock",
    "/",
    "//double_slash",
    "relative/path/to/sock",
    "./local.sock",
    "../parent.sock",
    "sock",
    ".sock",
    "my_socket_name",
    "/path with spaces/sock",
    "C:\\Users\\test\\.serialwrap\\serialwrapd.sock",
    "\\\\.\\pipe\\serialwrap",
    "COM1",
    "COM0",
    "COM99",

    # 不支援的 scheme / 語法錯誤
    "http://127.0.0.1:80",
    "https://example.com",
    "ws://127.0.0.1:8080",
    "wss://127.0.0.1:8080",
    "udp://127.0.0.1:53",
    "serial:///dev/ttyUSB0",
    "pipe:///tmp/pipe",
    "file:///tmp/file",
    "://empty_scheme",
    ":///path",
    "scheme://",
    "scheme://host",
    "scheme://host:1234",
    "://",
    ":",
    " ",
    "   ",
    "\t",
    "\n",
    "???",
    "###",
    "tcp://[::1]:9999999999999999999999999",
    "tcp://127.0.0.1:999999999999999999999",
    "tcp://host:0x10",
    "tcp://host:0o10",
    "tcp://host:0b10",
    "tcp://host:1e2",
    "tcp://host:NaN",
    "tcp://host:Inf",
]


def generate_rows() -> list[Row]:
    rows: list[Row] = []
    seen_inputs = set()

    for idx, raw in enumerate(CORPUS):
        if raw in seen_inputs:
            continue
        seen_inputs.add(raw)
        case_id = f"ep_{idx:04d}"
        try:
            transport, addr = client._parse_endpoint(raw)
            if transport == "unix":
                expected = f"ok:unix:{addr}"
            elif transport == "tcp":
                host, port = addr
                expected = f"ok:tcp:{host}:{port}"
            else:
                expected = f"ok:{transport}:{addr}"
        except ValueError as exc:
            expected = f"err:{exc}"

        rows.append((case_id, "parse_endpoint", raw, expected))

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "contracts/golden/endpoint.tsv",
        help="輸出路徑（預設 contracts/golden/endpoint.tsv）",
    )
    args = parser.parse_args()

    rows = generate_rows()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_tsv(args.out, rows)
    print(f"Wrote {len(rows)} endpoint cases to {args.out}")


if __name__ == "__main__":
    main()
