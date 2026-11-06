#!/usr/bin/env python3
"""P4-T1 黑箱 stub 共用實作（`stubs/bin/<name>` 皆為薄殼，呼叫 `stubtool.py <name> "$@"`）。

行為（計畫 Task 1 步驟 3）：
- 把 `{"tool": name, "argv": [...], "cwd": ...}` 以一行 JSON append 到 `$SW_STUB_LOG`（未設定則不記錄）。
- 依 `$SW_STUB_SCRIPT`（JSON：`{"<name> <argv join ' '>": {"rc": 0, "stdout": "", "stderr": ""}}`）回應；
  key 結尾 `*` 時做前綴比對；未命中回 rc 0、空輸出。
- `minicom` 另把收到的 `-D` 路徑寫進 log（欄位 `device`），並在 `$SW_STUB_MINICOM_HOLD_S` 秒後退出。
"""
from __future__ import annotations

import json
import os
import sys
import time


def _match(script: dict, cmdline: str) -> dict | None:
    exact = script.get(cmdline)
    if exact is not None:
        return exact
    for key, response in script.items():
        if key.endswith("*") and cmdline.startswith(key[:-1]):
            return response
    return None


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("stubtool: 缺少工具名稱\n")
        return 2
    name, args = argv[0], argv[1:]
    cmdline = " ".join([name, *args])

    row: dict = {"tool": name, "argv": args, "cwd": os.getcwd()}
    if name == "minicom" and "-D" in args:
        index = args.index("-D")
        if index + 1 < len(args):
            row["device"] = args[index + 1]

    log_path = os.environ.get("SW_STUB_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    script_text = os.environ.get("SW_STUB_SCRIPT", "")
    response = None
    if script_text:
        response = _match(json.loads(script_text), cmdline)
    rc = 0
    stdout = ""
    stderr = ""
    if response is not None:
        rc = int(response.get("rc", 0))
        stdout = str(response.get("stdout", ""))
        stderr = str(response.get("stderr", ""))

    if name == "minicom":
        time.sleep(float(os.environ.get("SW_STUB_MINICOM_HOLD_S", "0") or 0))

    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
