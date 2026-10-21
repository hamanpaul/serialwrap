"""建立可在工作目錄外執行的 stdlib Canary zipapp。"""

from __future__ import annotations

import zipfile
from pathlib import Path

COLLECTOR_MODULES: tuple[str, ...] = (
    "__init__",
    "jsonio",
    "proc",
    "reason_codes",
    "canary_policy",
    "canary_harvest",
    "canary_collect",
    "canary_classify",
    "canary_incidents",
    "collector_main",
)

_MAIN = "from tools.stage3.collector_main import main\nraise SystemExit(main())\n"


def _write_member(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (0o100644) << 16
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def pack(out: Path) -> Path:
    """封裝 collector 模組與原因碼資料，不包含產品程式或測試 harness。"""
    output = Path(out)
    output.parent.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    tools_init = root / "tools" / "__init__.py"
    if not tools_init.is_file():
        raise FileNotFoundError(tools_init)

    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            _write_member(archive, "__main__.py", _MAIN.encode("utf-8"))
            _write_member(archive, "tools/__init__.py", tools_init.read_bytes())
            for module in COLLECTOR_MODULES:
                name = "__init__.py" if module == "__init__" else f"{module}.py"
                source = root / "tools" / "stage3" / name
                if not source.is_file():
                    raise FileNotFoundError(source)
                _write_member(archive, f"tools/stage3/{name}", source.read_bytes())
            resource = root / "tools" / "stage3" / "stage3_reason_codes.json"
            if not resource.is_file():
                raise FileNotFoundError(resource)
            _write_member(archive, "tools/stage3/stage3_reason_codes.json", resource.read_bytes())
    except BaseException:
        try:
            output.unlink()
        except OSError:
            pass
        raise
    return output

