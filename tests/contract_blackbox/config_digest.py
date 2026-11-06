"""黑箱 A/B 設定身分摘要（config_digest，G4-ab1b-config-digest）。

夾具（`isolated_env` 登記的隔離根）建立設定之後，conftest 於 call 階段結束時對每個隔離根呼叫 `record_items`，
把「實際設定內容」的雜湊以 JSON Lines 追加到 `SW_AB_CONFIGLOG`：

- 隔離根內所有 YAML（config.yaml、各 profile 目錄）的內容雜湊，鍵為相對路徑；
- 測試控制的 env 摘要。隔離根路徑正規化為 `<ROOT>`，使同一設定於不同 tmp 根得到相同摘要；
  認證類鍵只記錄存在與否，不記值；PATH 僅保留 `<ROOT>`／`<REPO>`／`<host>` 標記，不含主機路徑。
- A／B 選擇器與證據路徑（CLI／daemon 路徑、label、pid／junit 紀錄）不進入摘要；它們已由身分的
  cli_*、daemon_* 欄位與證據檔記錄。

`aggregate` 把整輪紀錄彙整為 `digest`（整輪）與 `cases`（逐案例、逐夾具）。紀錄缺失或為空即 ValueError，
不得以空摘要放行（fail-closed）。本模組 stdlib-only，且不 import 任何 repo 模組。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

LOG_ENV = "SW_AB_CONFIGLOG"
ROOT_TOKEN = "<ROOT>"
REPO_TOKEN = "<REPO>"
HOST_TOKEN = "<host>"
REPO_ROOT = Path(__file__).resolve().parents[2]

_ENV_PREFIXES = ("SERIALWRAP_", "SW_")
_ENV_EXTRA = frozenset({
    "PATH", "HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "LANG", "LC_ALL", "COLUMNS", "PYTHONDONTWRITEBYTECODE",
})
# A／B 選擇器與證據路徑：不進入設定摘要（改由身分欄位與證據檔記錄）。
_SELECTOR_ENV = frozenset({
    "SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE", "SERIALWRAP_AB_LABEL", "SERIALWRAP_RELEASE_MANIFEST",
    "SW_BASELINE_PYTHON", "SW_BUNDLE", "SW_AB_PIDLOG", "SW_AB_CONFIGLOG", "SW_RESOURCE_REPORT",
})
_SECRET_MARKERS = ("PASS", "SECRET", "TOKEN", "CRED", "PRIVATE", "_KEY")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _root_variants(root: Path) -> list[str]:
    """隔離根在文字中可能出現的各種寫法；由長到短替換，避免部分取代。"""
    variants = {str(root), root.as_posix()}
    try:
        resolved = root.resolve()
        variants.update({str(resolved), resolved.as_posix()})
    except OSError:
        pass
    return sorted((v for v in variants if v), key=len, reverse=True)


def _normalize_text(text: str, root: Path) -> str:
    for variant in _root_variants(root):
        text = text.replace(variant, ROOT_TOKEN)
    return text


def _normalize_bytes(data: bytes, root: Path) -> bytes:
    for variant in _root_variants(root):
        data = data.replace(variant.encode("utf-8"), ROOT_TOKEN.encode("utf-8"))
    return data


def _relative_to(path_text: str, base: Path) -> str | None:
    try:
        rel = Path(path_text).resolve().relative_to(base.resolve()).as_posix()
    except (ValueError, OSError):
        return None
    return "" if rel == "." else rel


def _token(prefix: str, rel: str) -> str:
    return prefix if not rel else f"{prefix}/{rel}"


def _path_value(value: str, root: Path) -> str:
    parts: list[str] = []
    for entry in value.split(os.pathsep):
        if not entry:
            continue
        rel = _relative_to(entry, root)
        if rel is not None:
            parts.append(_token(ROOT_TOKEN, rel))
            continue
        rel = _relative_to(entry, REPO_ROOT)
        if rel is not None:
            parts.append(_token(REPO_TOKEN, rel))
            continue
        parts.append(HOST_TOKEN)
    return os.pathsep.join(parts)


def _is_secret(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in _SECRET_MARKERS)


def _env_summary(root: Path, env: Mapping[str, str]) -> dict[str, str]:
    summary: dict[str, str] = {}
    for key in sorted(env):
        if key in _SELECTOR_ENV or not (key.startswith(_ENV_PREFIXES) or key in _ENV_EXTRA):
            continue
        value = str(env[key])
        if _is_secret(key):
            summary[key] = "<set>" if value else "<empty>"
        elif key == "PATH":
            summary[key] = _path_value(value, root)
        else:
            summary[key] = _normalize_text(value, root)
    return summary


def _yaml_files(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith((".yaml", ".yml")):
                continue
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            found[rel] = _sha256(_normalize_bytes(path.read_bytes(), root))
    return found


def fixture_digest(root: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """單一隔離根的設定摘要。`digest` 只由 YAML 內容雜湊與正規化後的 env 摘要決定。"""
    root = Path(root)
    config_files = _yaml_files(root)
    env_summary = _env_summary(root, env)
    digest = _sha256(_canonical({"config_files": config_files, "env": env_summary}))
    return {"digest": digest, "config_files": config_files, "env": env_summary}


def record_items(
    log_path: Path | None,
    nodeid: str,
    isolations: Sequence[tuple[Path, Mapping[str, str]]],
) -> int:
    """把一個案例的各隔離根摘要追加到紀錄檔；未指定紀錄路徑時不寫入（一般 pytest 執行不受影響）。"""
    if log_path is None or not isolations:
        return 0
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, (root, env) in enumerate(isolations):
        rows.append({"nodeid": nodeid, "isolation": index, **fixture_digest(root, env)})
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)


def aggregate(log_path: Path) -> dict[str, Any]:
    """彙整整輪紀錄：`digest` 為整輪設定摘要，`cases` 為逐案例摘要與各夾具摘要。"""
    path = Path(log_path)
    if not path.is_file():
        raise ValueError(f"設定摘要紀錄不存在：{path}")
    raw = path.read_bytes()
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"設定摘要紀錄第 {number} 行無效：{exc}") from exc
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("nodeid"), str)
            or not isinstance(row.get("digest"), str)
            or not isinstance(row.get("isolation"), int)
        ):
            raise ValueError(f"設定摘要紀錄第 {number} 行欄位不完整")
        rows.append(row)
    if not rows:
        raise ValueError(f"設定摘要紀錄為空，證據不足：{path}")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["nodeid"], []).append({"isolation": row["isolation"], "digest": row["digest"]})
    cases: dict[str, dict[str, Any]] = {}
    for nodeid in sorted(grouped):
        fixtures = sorted(grouped[nodeid], key=lambda item: (item["isolation"], item["digest"]))
        cases[nodeid] = {"digest": _sha256(_canonical(fixtures)), "fixtures": fixtures}
    digest = _sha256(_canonical([[nodeid, cases[nodeid]["digest"]] for nodeid in sorted(cases)]))
    return {"digest": digest, "entries": len(rows), "log_sha256": _sha256(raw), "cases": cases}
