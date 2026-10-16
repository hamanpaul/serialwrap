#!/usr/bin/env python3
"""解析 Windows pytest 的 JUnit XML，輸出可供 Stage 0 gate 判讀的摘要。"""
from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def _nodeid(classname: str, name: str) -> str:
    """將 JUnit 的 classname/name 還原為 pytest 慣用 nodeid。"""
    parts = [part for part in classname.split(".") if part]
    module_parts = parts
    class_parts: list[str] = []
    root = Path.cwd()
    for index in range(len(parts), 0, -1):
        module_path = root.joinpath(*parts[:index]).with_suffix(".py")
        if module_path.is_file():
            module_parts = parts[:index]
            class_parts = parts[index:]
            break
    else:
        # cwd 不含測試來源時，保留 pytest 預設 Test* 類別命名的舊式回退。
        for index, part in enumerate(parts):
            if part.startswith("Test"):
                module_parts = parts[:index]
                class_parts = parts[index:]
                break

    module_path = "/".join(module_parts)
    if module_path:
        module_path += ".py"
    else:
        module_path = "<unknown>.py"
    return "::".join([module_path, *class_parts, name])


def summarize(
    junit: str | Path,
    *,
    pytest_exit: int,
    require_native: bool,
) -> dict[str, Any]:
    """解析收集、結果、skip、重複 nodeid 與最終退出碼。"""
    path = Path(junit)
    root: ET.Element | None = None
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        pass

    collected: list[str] = []
    passed: list[str] = []
    failed: list[str] = []
    skipped: list[dict[str, str]] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    duplicate_seen: set[str] = set()

    if root is not None:
        for case in root.iter("testcase"):
            nodeid = _nodeid(case.get("classname", ""), case.get("name", "<unknown>"))
            collected.append(nodeid)
            if nodeid in seen and nodeid not in duplicate_seen:
                duplicates.append(nodeid)
                duplicate_seen.add(nodeid)
            seen.add(nodeid)

            if case.find("failure") is not None or case.find("error") is not None:
                failed.append(nodeid)
            elif (skip := case.find("skipped")) is not None:
                reason = skip.get("message") or skip.get("type") or (skip.text or "").strip()
                skipped.append({"nodeid": nodeid, "reason": reason})
            else:
                passed.append(nodeid)

    normal_pytest_exit = 0 <= pytest_exit <= 5
    finished = root is not None and root.tag in {"testsuite", "testsuites"} and normal_pytest_exit
    rc = pytest_exit if 0 <= pytest_exit <= 255 else 125
    if not finished and rc == 0:
        rc = 4
    elif finished and require_native and skipped and rc == 0:
        # 呼叫端以 -m windows_native 或 windows_com 執行時，任何 skip 都表示原生證據不完整。
        rc = 5

    return {
        "collected": collected,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "rc": rc,
        "pytest_exit": pytest_exit,
        "finished": finished,
        "duplicates": duplicates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--pytest-exit", type=int, required=True)
    parser.add_argument("--require-native", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    result = summarize(
        args.junit,
        pytest_exit=args.pytest_exit,
        require_native=args.require_native,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"summary={args.output}")
    return int(result["rc"])


if __name__ == "__main__":
    raise SystemExit(main())
