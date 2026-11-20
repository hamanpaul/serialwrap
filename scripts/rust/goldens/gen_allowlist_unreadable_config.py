#!/usr/bin/env python3
"""以 Python 基線實測產生 SC-19 壞 config.yaml 的例外類別對照 golden（P5-T19，parity-amendments #47）。

每個案例在隔離實例（STATE／RUN／CONFIG 三目錄皆明確設定且非預設）中寫入 ``config.yaml``，以本 repo 的
``serialwrapd.py`` 啟動 Python daemon，擷取 exit code 與 stderr 全文。期望為 exit 2、
``serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: invalid config.yaml: <Python 例外類別名>``（``test_allowlist.py:131-150``）。

輸出 ``contracts/golden/allowlist_unreadable_config.tsv``：每行
``<case_id>\\t<hex(config.yaml 位元組)>\\t<exit code>\\t<hex(stderr UTF-8)>\\n``（hex 避免讀取端依賴受測解析器）。
``PermissionError`` 列須以非 root 執行 daemon（root 讀檔不受 mode 限制）：產生器為 root 時改以
``setpriv --reuid=65534 --regid=65534 --clear-groups``（nobody）執行該案例；無 setpriv 時略過並於 stderr 註記。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "contracts/golden/allowlist_unreadable_config.tsv"

CASES: list[tuple[str, bytes]] = [
    ("utf8_invalid", b"\xff\xfe\x00bad\n"),
    ("scanner_tab", b"a:\n\tb: 1\n"),
    ("scanner_unterminated_quote", b'a: "abc\n'),
    ("scanner_mapping_values", b"a: 1\n b: 2\n"),
    ("parser_unclosed_flow", b"a: [1, 2\n"),
    ("parser_seq_after_map", b"a: 1\n- b\n"),
    ("composer_multi_doc", b"a: 1\n---\nb: 2\n"),
    ("composer_undefined_alias", b"a: *nope\n"),
    ("constructor_python_tag", b"a: !!python/object:os.system x\n"),
    ("constructor_unhashable_key", b"? [1]\n: x\n"),
    ("value_bad_date", b"d: 2024-13-45\n"),
    ("recursion_deep", b"[" * 5000 + b"]" * 5000 + b"\n"),
    ("permission_denied", b"windows:\n  test_device_allowlist: [COM9]\n"),
    # 審查 F80：PyYAML 實際例外類別與訊息猜測不同的 4 種輸入（追加於尾端，不動既有列）。
    ("attribute_bad_timestamp", b"a: !!timestamp foo\n"),
    ("key_bad_bool", b"a: !!bool maybe\n"),
    ("composer_duplicate_anchor", b"a: &x 1\nb: &x 2\nc: *x\n"),
    ("parser_incompatible_version", b"%YAML 2.0\n---\na: 1\n"),
]


def run_case(case_id: str, data: bytes) -> tuple[int, str] | None:
    with tempfile.TemporaryDirectory(prefix="sw-golden-") as d:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SERIALWRAP_")}
        for name in ("STATE", "RUN", "CONFIG", "WAL"):
            os.makedirs(f"{d}/{name}", exist_ok=True)
            env[f"SERIALWRAP_{name}_DIR"] = f"{d}/{name}"
        env["SERIALWRAP_TEST_SYSTEM_CONFIG_PATH"] = f"{d}/system/config.yaml"
        env["HOME"] = f"{d}/home"
        env["PYTHONPATH"] = str(ROOT)
        cfg = Path(d, "CONFIG", "config.yaml")
        cfg.write_bytes(data)
        argv = [sys.executable, str(ROOT / "serialwrapd.py")]
        if case_id == "permission_denied":
            cfg.chmod(0o000)
            if os.geteuid() == 0:
                setpriv = "/usr/bin/setpriv"
                if not os.path.exists(setpriv):
                    sys.stderr.write("skip permission_denied：root 且無 setpriv\n")
                    return None
                for root, dirs, _files in os.walk(d):
                    for name in dirs:
                        os.chmod(os.path.join(root, name), 0o777)
                os.chmod(d, 0o777)
                argv = [setpriv, "--reuid=65534", "--regid=65534", "--clear-groups", *argv]
        proc = subprocess.run(
            argv,
            env=env,
            capture_output=True,
            timeout=30,
        )
        return proc.returncode, proc.stderr.decode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT, help="輸出路徑（預設 contracts/golden/…）")
    args = parser.parse_args()
    rows: list[str] = []
    for case_id, data in CASES:
        result = run_case(case_id, data)
        if result is None:
            continue
        code, stderr = result
        rows.append(f"{case_id}\t{data.hex()}\t{code}\t{stderr.encode('utf-8').hex()}\n")
    args.out.write_text("".join(rows), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
