"""乾淨 DUT 狀態與 A/B 實例隔離（P9-T5，stdlib only）。

L1.5 的 DUT 與 daemon 由 func-test runner 每個 case 自行啟停（P2 Task 10），本模組**不**啟動模擬器；
只負責：每側的全新實例目錄（A、B 永不共用）、artifact 選定 env（arch §9.2），以及 L2／L3 bench 的乾淨狀態檢查。

依 §15.9.1：B 必須是「相同初始條件的全新實例」，不是沿用 Python 跑過後的 DUT 狀態。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import jsonio
from .ab_manifest import AbManifest, AbSide, render_argv
from .proc import Runner, run_cmd
from .reason_codes import HarnessError

ISOLATED_DIR_KEYS: tuple[str, ...] = (
    "SERIALWRAP_STATE_DIR", "SERIALWRAP_RUN_DIR", "SERIALWRAP_WAL_DIR", "SERIALWRAP_CONFIG_DIR",
    "SERIALWRAP_LOG_DIR", "SERIALWRAP_EVENTS_DIR", "SERIALWRAP_EVENTS_RUNTIME_DIR",
    "SERIALWRAP_BY_ID_DIR", "SERIALWRAP_BY_PATH_DIR",
)  # 與 tests/conftest.py 強制覆寫的 9 個一致
EXPLICIT_KEYS: tuple[str, ...] = (
    "SERIALWRAP_PROFILE_DIR", "SERIALWRAP_DATA_DIR", "SERIALWRAP_TTYMCU_PATH", "SERIALWRAP_EVENTS_LOG_PATH",
)
ARTIFACT_KEYS: tuple[str, ...] = ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE", "SERIALWRAP_AB_LABEL")  # arch §9.2
POPPED_KEYS: tuple[str, ...] = ("SERIALWRAP_ENDPOINT", "SERIALWRAP_TEST_DEVICE_ALLOWLIST")

# 以下為本模組的實作常數（非計畫列出的公開介面）
# l1.5 額外移除的外層覆寫：tests/conftest.py 同樣會 pop；SERIALWRAP_CONFIG 優先於 CONFIG_DIR，
# RELEASE_MANIFEST 會換掉 bundle 的 manifest，兩者若由外層帶入即穿透實例隔離。
_L15_EXTRA_POPPED_KEYS: tuple[str, ...] = (
    "SERIALWRAP_CONFIG", "SERIALWRAP_RELEASE_MANIFEST",
    "SERIALWRAP_TEST_KILL_AFTER_SAVE", "SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE",
)
_L15_SUBDIRS: tuple[str, ...] = (
    "state", "run", "wal", "config", "log", "events", "events-run", "by-id", "by-path", "profiles", "data",
)
_RUN_DIR_RECORD = "run-dir.txt"
_RUN_DIR_PREFIX = "sw-ab-"
_SHORT_TMP = Path("/tmp")          # AF_UNIX sun_path 107 字元上限（比照 regression throwaway_env）
_RESET_TIMEOUT_S = 120.0
_QUERY_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class Instance:
    """單側的一次性實例：root 不得重用，env 為該側執行時的完整環境。"""

    side: str
    round: int
    root: Path
    env: Mapping[str, str] = field(repr=False)   # 可能帶有外層認證類變數，不進 repr／日誌
    report_dir: Path


def side_env(base_env: Mapping[str, str], *, side: str, round: int) -> dict[str, str]:
    """回傳 base_env 副本並加上 `SW_STAGE3_SIDE`、`SW_STAGE3_ROUND`（供 suite 證據標記側別）。"""
    out = dict(base_env)
    out["SW_STAGE3_SIDE"] = side
    out["SW_STAGE3_ROUND"] = str(round)
    return out


def artifact_env(m: AbManifest, side: AbSide) -> dict[str, str]:
    """選定本側 artifact 的 CLI／daemon 執行檔與報告標籤（arch §9.2）。"""
    return {
        "SERIALWRAP_CLI_EXE": str(side.cli_exe),
        "SERIALWRAP_DAEMON_EXE": str(side.daemon_exe),
        "SERIALWRAP_AB_LABEL": f"{m.run_id}-{side.label}",
    }


def fresh_instance(m: AbManifest, side: AbSide, *, round: int, base_dir: Path,
                   base_env: Mapping[str, str] | None = None,
                   repo_root: Path | None = None) -> Instance:
    """建立本側的全新實例目錄與 env。

    `root = base_dir/r{round}-{label}` 已存在即拒絕（`HARNESS_DUT_NOT_CLEAN`），保證 B 不沿用 A 的目錄。
    env 由 base_env（移除 POPPED_KEYS）加上 side 標記與 artifact 選定三層組成，避免任一層退回預設 Python 基線。
    base_env 為 None 時取行程環境（os.environ）。l1.5 另建立隔離目錄並複製凍結 profile；l2／l3 使用 bench 已安裝實例，不隔離目錄。
    `repo_root` 為 profile_files 相對路徑的基準（resolve 後），預設為 manifest 所在目錄。
    """
    root = Path(base_dir) / f"r{round}-{side.label}"
    if root.exists():
        raise HarnessError("HARNESS_DUT_NOT_CLEAN", "instance root reused")
    source = os.environ if base_env is None else base_env
    base = {k: v for k, v in source.items() if k not in POPPED_KEYS}
    env = side_env(base, side=side.label, round=round)
    env.update(artifact_env(m, side))
    try:
        root.mkdir(parents=True)
    except FileExistsError as exc:
        raise HarnessError("HARNESS_DUT_NOT_CLEAN", "instance root reused") from exc
    report_dir = root / "report"
    report_dir.mkdir()
    if m.layer == "l1.5":
        try:
            _prepare_l15(m, root, env, repo_root=repo_root)
        except BaseException:
            _cleanup_run_dir(root)   # 失敗時不留下已建立的暫存 RUN_DIR；root 保留供事後檢視
            raise
    return Instance(side=side.label, round=round, root=root, env=env, report_dir=report_dir)


def verify_clean(m: AbManifest, inst: Instance, side: AbSide, *, run: Runner = run_cmd,
                 repo_root: Path | None = None, out_dir: Path | None = None) -> list[str]:
    """回傳違規清單；空清單表示實例／DUT 在執行前乾淨。

    l2／l3 會執行 `reset_argv`（有副作用），每側只能呼叫一次。`repo_root` 供模板 `{repo_root}` 與
    profile 相對路徑使用（預設為 manifest 所在目錄）；`out_dir` 供模板 `{out_dir}`（預設為實例的父目錄，即 base_dir）。
    """
    if m.layer == "l1.5":
        return _verify_l15(inst)
    out = inst.root.parent if out_dir is None else Path(out_dir)
    return _verify_hardware(m, inst, side, run, repo_root=_repo_root(m, repo_root), out_dir=out)


def cleanup_instance(inst: Instance) -> list[str]:
    """刪除 POSIX 暫存 RUN_DIR（依 root 內的記錄檔）；失敗回 `run dir cleanup failed`。"""
    return _cleanup_run_dir(inst.root)


# ---------------------------------------------------------------------------
# 內部實作
# ---------------------------------------------------------------------------


def _isolated_paths(root: Path) -> dict[str, str]:
    """9 個 ISOLATED_DIR_KEYS 中除 RUN_DIR 以外的目錄（RUN_DIR 另行處理）。"""
    return {
        "SERIALWRAP_STATE_DIR": str(root / "state"),
        "SERIALWRAP_WAL_DIR": str(root / "wal"),
        "SERIALWRAP_CONFIG_DIR": str(root / "config"),
        "SERIALWRAP_LOG_DIR": str(root / "log"),
        "SERIALWRAP_EVENTS_DIR": str(root / "events"),
        "SERIALWRAP_EVENTS_RUNTIME_DIR": str(root / "events-run"),
        "SERIALWRAP_BY_ID_DIR": str(root / "by-id"),
        "SERIALWRAP_BY_PATH_DIR": str(root / "by-path"),
    }


def _prepare_l15(m: AbManifest, root: Path, env: dict[str, str], *, repo_root: Path | None) -> None:
    for sub in _L15_SUBDIRS:
        (root / sub).mkdir(parents=True)
    env.update(_isolated_paths(root))
    for key in _L15_EXTRA_POPPED_KEYS:
        env.pop(key, None)
    env["SERIALWRAP_TEST_SYSTEM_CONFIG_PATH"] = str(root / "config" / "system-config.yaml")   # 不落到主機 /etc
    if os.name == "nt":
        run_dir = root / "run"
    else:
        # AF_UNIX 路徑上限：RUN_DIR 必須是獨立短路徑，不可巢狀在實例 root 之下
        run_dir = Path(tempfile.mkdtemp(prefix=_RUN_DIR_PREFIX, dir=str(_SHORT_TMP)))
        (root / _RUN_DIR_RECORD).write_text(f"{run_dir}\n", encoding="utf-8")
    env["SERIALWRAP_RUN_DIR"] = str(run_dir)
    env["SERIALWRAP_PROFILE_DIR"] = str(root / "profiles")
    env["SERIALWRAP_DATA_DIR"] = str(root / "data")
    env["SERIALWRAP_TTYMCU_PATH"] = str(root / "run" / "dev" / "ttyMCU")
    env["SERIALWRAP_EVENTS_LOG_PATH"] = str(root / "events" / "events.ndjson")
    if m.platform == "windows" and m.test_device_allowlist:
        env["SERIALWRAP_TEST_DEVICE_ALLOWLIST"] = ",".join(m.test_device_allowlist)   # SC-19／OD-11
    _copy_profiles(m, root / "profiles", repo_root=repo_root)


def _repo_root(m: AbManifest, repo_root: Path | None) -> Path:
    """與 `suites._repo_root` 同規則（resolve 後；未給時取 manifest 所在目錄）。

    此處不 import suites 套件：suites 之後才會消費本模組，避免 dut_state 反向依賴 suites。
    """
    if repo_root is not None:
        return Path(repo_root).resolve()
    return Path(m.source_path).resolve().parent


def _copy_profiles(m: AbManifest, dest: Path, *, repo_root: Path | None) -> None:
    base = _repo_root(m, repo_root)
    for frozen in m.profile_files:
        src = Path(frozen.path)
        if not src.is_absolute():
            src = base / src
        if not src.is_file():
            raise HarnessError("HARNESS_MANIFEST_DRIFT", f"profile file missing: {frozen.path}")
        data = src.read_bytes()   # 只讀一次：雜湊與寫入同一份位元組，避免讀寫之間被替換
        if jsonio.sha256_bytes(data) != frozen.sha256:
            raise HarnessError("HARNESS_MANIFEST_DRIFT", f"profile sha256 mismatch: {frozen.path}")
        target = dest / src.name
        if target.exists():
            raise HarnessError("HARNESS_MANIFEST_INVALID", f"duplicate profile name: {src.name}")
        target.write_bytes(data)


def _verify_l15(inst: Instance) -> list[str]:
    violations: list[str] = []
    if os.path.lexists(inst.root / "state" / "state.json"):   # lexists：懸空 symlink 亦視為殘留
        violations.append("state.json present")
    if _has_files(inst.root / "wal"):
        violations.append("wal not empty")
    if any(_run_dir_has_entries(d) for d in _run_dirs(inst)):
        violations.append("run dir not empty")
    return violations


def _verify_hardware(m: AbManifest, inst: Instance, side: AbSide, run: Runner, *,
                     repo_root: Path, out_dir: Path) -> list[str]:
    # 先於任何 DUT 命令編譯 pattern：壞 pattern 不得在 reset 已執行之後才被發現
    expected = _compile_expected(m.dut.expected_fingerprint)
    env = dict(inst.env)
    values = _template_values(m, inst, side, repo_root=repo_root, out_dir=out_dir)

    if m.dut.reset_argv:
        res = run(render_argv(m.dut.reset_argv, values), env=env, timeout=_RESET_TIMEOUT_S)
        if res.rc != 0:
            # 重置失敗即無法確立乾淨狀態，後續查詢不再判讀
            return [f"dut reset failed rc={res.rc}"]

    violations: list[str] = []
    doc: dict[str, Any] | None = None
    if m.dut.fingerprint_argv:
        res = run(render_argv(m.dut.fingerprint_argv, values), env=env, timeout=_QUERY_TIMEOUT_S)
        doc = _json_object(res.stdout) if res.rc == 0 else None
        if doc is None and not expected:
            # 有指紋命令卻取不到結果（rc≠0、逾時、非 JSON object）：無證據即不得判為乾淨
            violations.append("fingerprint unavailable")
    # expected 非空而無有效指紋時，doc 為 None，每鍵皆逐一判為 mismatch（fail closed）
    for key, pattern in expected.items():
        if not _fingerprint_matches(pattern, doc, key):
            violations.append(f"fingerprint mismatch: {key}")

    argv = [str(side.cli_exe)]
    if m.endpoint:
        argv += ["--endpoint", m.endpoint]
    argv += ["session", "list"]
    res = run(argv, env=env, timeout=_QUERY_TIMEOUT_S)
    listing = _json_object(res.stdout) if res.rc == 0 else None
    states = _session_states(listing)
    for board in m.dut.boards:
        if states.get(board) != "READY":
            violations.append(f"board not READY: {board}")

    if _state_polluted(inst.env):
        violations.append("state polluted")
    return violations


def _template_values(m: AbManifest, inst: Instance, side: AbSide, *, repo_root: Path, out_dir: Path) -> dict[str, str]:
    """提供 TEMPLATE_KEYS 中本模組可得的全部值（render_argv 遇缺值即 HARNESS_MANIFEST_INVALID）。"""
    return {
        "cli_exe": str(side.cli_exe),
        "daemon_exe": str(side.daemon_exe),
        "bundle_dir": str(side.bundle_dir),
        "report_dir": str(inst.report_dir),
        "instance_dir": str(inst.root),
        "seed": str(m.seed),
        "side": side.label,
        "round": str(inst.round),
        "endpoint": m.endpoint or "",
        "out_dir": str(out_dir),
        "repo_root": str(repo_root),
    }


def _compile_expected(expected: Mapping[str, str]) -> dict[str, re.Pattern[str]]:
    """依鍵排序編譯 expected_fingerprint；壞 pattern 即 HARNESS_MANIFEST_INVALID。"""
    compiled: dict[str, re.Pattern[str]] = {}
    for key in sorted(expected):
        try:
            compiled[key] = re.compile(expected[key])
        except re.error as exc:
            raise HarnessError("HARNESS_MANIFEST_INVALID", f"invalid fingerprint pattern for {key}") from exc
    return compiled


def _fingerprint_matches(pattern: re.Pattern[str], doc: dict[str, Any] | None, key: str) -> bool:
    if doc is None or doc.get(key) is None:
        return False
    return pattern.fullmatch(str(doc[key])) is not None


def _session_states(doc: dict[str, Any] | None) -> dict[str, str]:
    states: dict[str, str] = {}
    rows = (doc or {}).get("sessions") or []
    if not isinstance(rows, list):
        return states
    for row in rows:
        if isinstance(row, dict):
            states[str(row.get("com"))] = str(row.get("state"))
    return states


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return doc if isinstance(doc, dict) else None


def _state_dir(env: Mapping[str, str]) -> Path:
    configured = (env.get("SERIALWRAP_STATE_DIR") or "").strip()
    if configured:
        return Path(os.path.expanduser(configured))
    base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(os.path.expanduser(base)) / "serialwrap"


def _state_polluted(env: Mapping[str, str]) -> bool:
    """state.json 的 bindings 含 `/tmp/` 哨兵即視為污染（與 realhw `_state_polluted` 同規則）。"""
    path = _state_dir(env) / "state.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    bindings = data.get("bindings") or {}
    return "/tmp/" in json.dumps(bindings, ensure_ascii=False)


def _has_files(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    return any(p.is_file() or p.is_symlink() for p in directory.rglob("*"))


def _run_dirs(inst: Instance) -> list[Path]:
    """本側實際使用的 RUN_DIR（實例內 `run/` 與 env 指向者；POSIX 時兩者不同）。"""
    candidates = [inst.root / "run"]
    effective = inst.env.get("SERIALWRAP_RUN_DIR")
    if effective:
        candidates.append(Path(effective))
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _run_dir_has_entries(directory: Path) -> bool:
    """`dev/` 子目錄（ttyMCU 位置）不計入殘留。"""
    if not directory.is_dir():
        return False
    return any(child.name != "dev" for child in directory.iterdir())


def _cleanup_run_dir(root: Path) -> list[str]:
    record = root / _RUN_DIR_RECORD
    if not record.exists():
        return []   # Windows，或尚未建立暫存 RUN_DIR
    try:
        target = Path(record.read_text(encoding="utf-8").strip())
        if not target.exists():
            return []   # 已清除（可重入）
        # 防呆：只刪除本模組建立的 /tmp/sw-ab-* 真目錄，避免記錄檔被竄改時誤刪其他路徑
        if (target.is_symlink() or not target.is_dir() or target.parent != _SHORT_TMP
                or not target.name.startswith(_RUN_DIR_PREFIX)):
            return ["run dir cleanup failed"]
        shutil.rmtree(target)
    except (OSError, ValueError):   # ValueError 涵蓋 UnicodeDecodeError（損毀的記錄檔）
        return ["run dir cleanup failed"]
    return []
