#!/usr/bin/env python3
"""檢查 Windows PE 匯入 DLL 是否符合 serialwrap 的 allowlist。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


DLL_LINE = re.compile(r"^\s*DLL Name:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
ALLOWED_EXACT = {
    "kernel32.dll",
    "advapi32.dll",
    "ws2_32.dll",
    "ntdll.dll",
    "bcryptprimitives.dll",
    "bcrypt.dll",
    "userenv.dll",
    "msvcrt.dll",
}
FORBIDDEN = {"libgcc_s_seh-1.dll", "libwinpthread-1.dll", "libstdc++-6.dll"}
OBJDUMP = "x86_64-w64-mingw32-objdump"


def _allowed(name: str) -> bool:
    lowered = name.lower()
    return lowered in ALLOWED_EXACT or (
        lowered.endswith(".dll")
        and (lowered.startswith("api-ms-win-crt-") or lowered.startswith("api-ms-win-core-"))
    )


def _inspect(executable: str, objdump: str | None) -> tuple[dict[str, Any], int]:
    path = Path(executable)
    if objdump is None:
        return ({"exe": executable, "imports": [], "unexpected": [], "forbidden": [], "ok": False,
                 "error": f"{OBJDUMP} 不存在，證據不足"}, 2)
    if not path.is_file():
        return ({"exe": executable, "imports": [], "unexpected": [], "forbidden": [], "ok": False,
                 "error": "PE 檔案不存在，證據不足"}, 2)
    try:
        proc = subprocess.run([objdump, "-p", str(path)], capture_output=True, text=True, check=False)
    except OSError as exc:
        return ({"exe": executable, "imports": [], "unexpected": [], "forbidden": [], "ok": False,
                 "error": f"無法執行 objdump：{exc}"}, 2)
    if proc.returncode != 0:
        return ({"exe": executable, "imports": [], "unexpected": [], "forbidden": [], "ok": False,
                 "error": proc.stderr.strip() or f"objdump exit {proc.returncode}"}, 2)

    imports = DLL_LINE.findall(proc.stdout)
    if not imports:
        return (
            {
                "exe": executable,
                "imports": [],
                "unexpected": [],
                "forbidden": [],
                "ok": False,
                "error": "objdump 未回報任何 DLL Name，證據不足",
            },
            2,
        )
    lowered_forbidden = {name.lower() for name in FORBIDDEN}
    forbidden = sorted({name for name in imports if name.lower() in lowered_forbidden}, key=str.lower)
    unexpected = sorted({name for name in imports if not _allowed(name) and name.lower() not in lowered_forbidden}, key=str.lower)
    ok = not forbidden and not unexpected
    return ({"exe": executable, "imports": imports, "unexpected": unexpected, "forbidden": forbidden, "ok": ok},
            0 if ok else 1)


def main(argv: Sequence[str] | None = None) -> int:
    paths = list(sys.argv[1:] if argv is None else argv)
    if not paths:
        print("用法：check_win_imports.py <exe>…", file=sys.stderr)
        return 2
    objdump = shutil.which(OBJDUMP)
    inspected = [_inspect(path, objdump) for path in paths]
    rows = [item[0] for item in inspected]
    codes = [item[1] for item in inspected]
    if len(rows) == 1:
        document: Any = rows[0]
    else:
        document = {"results": rows, "ok": all(row["ok"] for row in rows)}
    print(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if 1 in codes:
        return 1
    return 2 if 2 in codes else 0


if __name__ == "__main__":
    raise SystemExit(main())
