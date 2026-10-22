"""P9-T10 分類測試使用的合成 Canary 帳本資料。"""

from __future__ import annotations

import json
import base64
import hashlib
import os
import platform
import re
import shutil
import sys
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeVar

import yaml

from tools.stage3 import jsonio
from tools.stage3.proc import CmdResult


BTREE = "b" * 64
RTREE = "d" * 64
REPO_ROOT = Path(__file__).resolve().parents[1]

T = TypeVar("T")


def ts(day: int, hh: int, mm: int = 0, ss: int = 0) -> str:
    """建立固定 UTC 時區的 Canary 測試時間。"""
    return f"2026-11-{day:02d}T{hh:02d}:{mm:02d}:{ss:02d}+00:00"


def snapshot(
    t_wall: str,
    *,
    host: str = "h1",
    platform: str = "linux",
    boot: str = "b1",
    pid: int | None = 100,
    ok: bool = True,
    artifact: str | None = "art-1",
    artifact_check: str = "verified",
    version: str = "0.9.0",
    wal_healthy: bool = True,
    multi_open: bool = False,
    sessions: Sequence[tuple] = (("COM0", "READY", 1),),
    bundle_tree: str | None = BTREE,
    runtime_tree: str | None = RTREE,
) -> dict[str, Any]:
    """建立符合 Canary 快照帳本形狀的合成紀錄。"""
    session_rows = []
    session_fields = {
        "boot_quiet_remaining_s",
        "ready_reconfirm_pending",
        "ready_reconfirm_failed",
        "command_capable",
        "platform",
        "profile",
        "last_error",
        "last_ready_at",
    }
    for row in sessions:
        com, state, generation = row[:3]
        extra = dict(row[3]) if len(row) > 3 else {}
        session_rows.append(
            {
                "com": com,
                "state": state,
                "bridge_generation": generation,
                "boot_quiet_remaining_s": extra.get("boot_quiet_remaining_s"),
                "ready_reconfirm_pending": extra.get("ready_reconfirm_pending", False),
                "ready_reconfirm_failed": extra.get("ready_reconfirm_failed", False),
                "command_capable": extra.get("command_capable", True),
                "platform": extra.get("platform", "shell"),
                "profile": extra.get("profile"),
                "last_error": extra.get("last_error"),
                "last_ready_at": extra.get("last_ready_at"),
            }
        )
        if extra.keys() - session_fields:
            raise ValueError(f"不支援的 session 額外欄位：{sorted(extra.keys() - session_fields)}")

    record: dict[str, Any] = {
        "type": "snapshot",
        "host_id": host,
        "platform": platform,
        "t_wall": t_wall,
        "boot_id": boot,
        "daemon_ok": ok,
        "daemon_pid": pid,
        "daemon_version": version,
        "daemon_exe_sha256": None,
        "artifact_id": artifact,
        "artifact_check": artifact_check,
        "bundle_tree_sha256": bundle_tree,
        "runtime_tree_sha256": runtime_tree,
        "bundle_version": version,
        "daemon_implementation": "rust",
        "wal_dir": "/synthetic/wal",
        "wal_healthy": wal_healthy,
        "multi_open": multi_open,
        "sessions": session_rows,
        "error": None,
    }
    record["record_id"] = jsonio.record_id(record)
    return record


def policy_dict(**over: Any) -> dict[str, Any]:
    """讀取 repo Canary policy 並套用凍結版測試預設與 dotted-key 覆寫。"""
    path = Path(__file__).resolve().parent / "acceptance" / "canary_policy.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["status"] = "frozen"
    doc["frozen_by"] = "owner"
    doc["frozen_at"] = "2026-11-01T00:00:00+00:00"
    doc["decision_ref"] = "OD-10-C1"
    for quota in doc["platforms"].values():
        quota["evidence_owner"] = "owner"
        quota["utc_offset_minutes"] = 0
    for dotted_key, value in over.items():
        keys = dotted_key.split(".")
        node = doc
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return doc


def write_policy(path: Path, **over: Any) -> Path:
    """將 Canary 測試政策寫至指定臨時檔。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy_dict(**over), ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


def wal_record(
    seq: int,
    *,
    dir: str = "TX",
    com: str = "COM0",
    source: str = "agent:a",
    cmd_id: str | None = None,
    wall_ts: str = "2026-11-02T01:00:00+00:00",
    meta: dict | None = None,
    loss_flag: bool = False,
    payload: bytes = b"x",
) -> dict[str, Any]:
    """建立 CRC32 與長度依真實 payload 計算的 WAL 紀錄。"""
    body = bytes(payload)
    return {
        "cmd_id": cmd_id,
        "com": com,
        "crc32": f"{zlib.crc32(body) & 0xFFFFFFFF:08x}",
        "dir": dir,
        "len": len(body),
        "loss_flag": bool(loss_flag),
        "meta": dict(meta or {}),
        "mono_ts_ns": max(0, int(seq)) * 1_000_000_000,
        "payload_b64": base64.b64encode(body).decode("ascii"),
        "seq": int(seq),
        "source": source,
        "wall_ts": wall_ts,
    }


def uuid_cmd_id(n: int) -> str:
    """回傳固定寬度的小寫十六進位命令識別碼。"""
    return f"{int(n):032x}"


def write_wal(path: Path, records, *, trailing_newline: bool = True, mode: str = "w") -> None:
    """以穩定排序 JSON 寫出 WAL 行。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [jsonio.dumps_compact(record) for record in records]
    data = "\n".join(lines)
    if trailing_newline and lines:
        data += "\n"
    if mode == "a" and path.exists() and path.stat().st_size:
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                data = "\n" + data
    with path.open(mode, encoding="utf-8", newline="\n") as fh:
        fh.write(data)


def sha256_text(text: str) -> str:
    """計算 UTF-8 文字內容的 SHA-256。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    """轉呼叫 Stage 3 共用檔案雜湊工具。"""
    return jsonio.sha256_file(Path(path))


def tree_sha256(root: Path) -> str:
    """依相對 POSIX 路徑排序，雜湊執行期樹中的一般檔案。"""
    root = Path(root)
    rows: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError(f"不接受執行期樹中的 symlink：{rel.as_posix()}")
        if path.is_file():
            rows.append(f"{rel.as_posix()}\0{sha256_file(path)}\n")
    return hashlib.sha256("".join(rows).encode("utf-8")).hexdigest()


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(jsonio.dumps_report(value), encoding="utf-8", newline="\n")
    return path


def make_bundle(
    root: Path,
    *,
    version: str = "0.9.0",
    stage: str = "3",
    cli_impl: str = "rust",
    daemon_impl: str = "rust",
    retirement_marker: bool | None = None,
    target: str = "x86_64-unknown-linux-gnu",
) -> Path:
    """建立檔案雜湊可重算的 Stage 3 測試 bundle。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli = bin_dir / ("serialwrap.exe" if "windows" in target or "win32" in target else "serialwrap")
    daemon = bin_dir / ("serialwrapd.exe" if "windows" in target or "win32" in target else "serialwrapd")
    cli.write_text("#!/usr/bin/env python3\n# 測試用 CLI stub\n", encoding="utf-8", newline="\n")
    daemon.write_text("#!/usr/bin/env python3\n# 測試用 daemon stub\n", encoding="utf-8", newline="\n")
    if os.name != "nt":
        cli.chmod(0o755)
        daemon.chmod(0o755)

    runtime = root / "py"
    if daemon_impl == "python":
        (runtime / "sw_core").mkdir(parents=True, exist_ok=True)
        (runtime / "python").write_text("#!/usr/bin/env python3\n", encoding="utf-8", newline="\n")
        (runtime / "serialwrapd.py").write_text("# 測試用 Python daemon\n", encoding="utf-8", newline="\n")
        (runtime / "sw_core" / "__init__.py").write_text("\"\"\"測試用套件。\"\"\"\n", encoding="utf-8", newline="\n")
        (runtime / "sw_core" / "cli.py").write_text("# 測試用 Python CLI\n", encoding="utf-8", newline="\n")

    assets_dir = root / "assets"
    assets_dir.mkdir(exist_ok=True)
    skill = assets_dir / "SKILL.md"
    skill_win = assets_dir / "SKILL_WINDOWS.md"
    profiles = assets_dir / "profiles.yaml"
    supervisor = assets_dir / "serialwrap.service"
    for path, text in ((skill, "# 測試技能\n"), (skill_win, "# Windows 測試技能\n"),
                       (profiles, "defaults: {}\n"), (supervisor, "[Service]\n")):
        path.write_text(text, encoding="utf-8", newline="\n")

    revision = "0" * 40
    daemon_component: dict[str, Any] = {
        "implementation": daemon_impl,
        "version": version,
        "git_revision": revision,
        "launcher": {"path": daemon.relative_to(root).as_posix(), "sha256": sha256_file(daemon)},
    }
    if daemon_impl == "python":
        daemon_component.update(
            {
                "entry": ["py/python", "-I", "-B", "py/serialwrapd.py"],
                "runtime": {"path": "py/python", "python_version": "3.12.0", "sha256": sha256_file(runtime / "python")},
                "package": {
                    "name": "serialwrap",
                    "version": version,
                    "wheel_sha256": sha256_text("fixture wheel"),
                    "tree_sha256": tree_sha256(runtime),
                },
            }
        )
    include_retirement = stage == "3" if retirement_marker is None else retirement_marker
    components: dict[str, Any] = {
        "cli": {
            "implementation": cli_impl,
            "version": version,
            "git_revision": revision,
            "path": cli.relative_to(root).as_posix(),
            "sha256": sha256_file(cli),
        },
        "daemon": daemon_component,
        "assets": {
            "skill_sha256": sha256_file(skill),
            "skill_windows_sha256": sha256_file(skill_win),
            "profiles_sha256": sha256_file(profiles),
        },
        "supervisor": {"default_mode": "on-demand", "definition_sha256": sha256_file(supervisor)},
    }
    if not include_retirement:
        components["python_cli_delegate"] = {
            "entry": ["py/python", "-I", "-B", "-m", "sw_core.cli"],
            "tree_sha256": tree_sha256(runtime) if runtime.exists() else sha256_text("empty runtime"),
        }

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "bundle_version": version,
        "stage": stage,
        "created_at": "2026-11-01T00:00:00+00:00",
        "target": target,
        "git_revision": revision,
        "contract_revision": revision,
        "components": components,
        "pairing": {"rule": "exact", "cli_version": version, "daemon_version": version},
    }
    if include_retirement:
        manifest["retirement"] = {"python_daemon": "removed"}
    _write_json(root / "release-manifest.json", manifest)
    sums = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_file() and path.name not in {"SHA256SUMS", "release-manifest.json"}:
            sums.append(f"{sha256_file(path)}  {path.relative_to(root).as_posix()}\n")
    (root / "SHA256SUMS").write_text("".join(sums), encoding="utf-8", newline="\n")
    return root


def write_fake_repo(root: Path, *, layer: str = "l1.5") -> Path:
    """建立含 P2 凍結資料與各 adapter stub 的最小假 repo。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    files = (
        "contracts/behavior_changes.yaml",
        "tests/acceptance/cases.yaml",
        "tests/acceptance/cases.lock.json",
        "realhw/acceptance/longrun_acceptance.yaml",
        "realhw/acceptance/longrun_acceptance.schema.json",
    )
    for rel in files:
        source = REPO_ROOT / rel
        if source.is_file():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    stubs = {
        "tools/stage3/__init__.py": "# Stage 3 測試替身\n",
        "tools/uart_stdio_bridge/bridge.py": "# UART bridge 測試替身\n",
        "func-test/runner.py": "# func-test 測試替身\n",
        "regression/plugin.py": "# regression 測試替身\n",
        "realhw/runner.py": "# realhw 測試替身\n",
        "profiles/default.yaml": "defaults: {}\nprofiles: {}\n",
        "regression/testbed.template.yaml": (
            "serialwrap_exe: serialwrap\nbench_lock_path: fake-bench-lock\n"
            "allow_destructive: false\nauthorized_boards: []\n"
            "boards:\n  - com: TEST0\n    alias: fake-dut\n    serial: fake-0\n    platform: shell\n"
            "timeouts: {ready_wait_s: 1}\n"
        ),
    }
    for rel, contents in stubs.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8", newline="\n")
    bridge_src = REPO_ROOT / "tools" / "uart_stdio_bridge" / "src"
    if str(bridge_src) not in sys.path:
        sys.path.insert(0, str(bridge_src))
    from uart_stdio_bridge import TOOL_NAME, __version__
    from uart_stdio_bridge.selftest.checks import CHECK_IDS

    approved = json.loads((REPO_ROOT / "tools" / "uart_stdio_bridge" / "APPROVED_REVISION.json")
                          .read_text(encoding="utf-8"))
    selftest = {
        "schema_version": "bridge-selftest.v1",
        "created_at": "2026-10-21T00:00:00Z",
        "complete": True,
        "not_selected": [],
        "tool": {"name": TOOL_NAME, "version": __version__, "tree_sha256": approved["tree_sha256"]},
        "platform": {"os": platform.system(), "evidence_layer": "posix-pty", "com_pair": None,
                     "evidence_insufficient": []},
        "endpoint": "posix-pty",
        "transport_mode": "virtual_pty",
        "capabilities": {"transport_disconnect": True},
        "checks": [{"id": check_id, "verdict": "PASS", "reason": "合成測試報告",
                    "evidence_dir": None} for check_id in CHECK_IDS],
        "verdict": "PASS",
        "test_overrides": {},
    }
    _write_json(root / "reports" / "bridge-selftest.json", selftest)
    _write_json(root / "realhw" / "config.json", {
        "boards": [{"com": "TEST0", "alias": "fake-dut", "serial": "fake-0", "platform": "shell"}],
        "serialwrap_exe": "serialwrap", "bench_lock_path": "fake-bench-lock",
        "human_frontend": "tmux", "timeouts": {}, "longrun": {},
    })
    return root


def _set_dotted(doc: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    node: Any = doc
    for key in keys[:-1]:
        node = node[int(key)] if isinstance(node, list) else node[key]
    final = keys[-1]
    if isinstance(node, list):
        node[int(final)] = value
    else:
        node[final] = value


def write_ab_manifest(root: Path, *, layer: str = "l1.5", platform: str = "linux", stage: str = "3", **overrides: Any) -> Path:
    """建立含凍結雜湊與兩側 bundle 的 schema-valid A/B manifest。"""
    root = write_fake_repo(root, layer=layer)
    examples = {"l1.5": "ab_manifest.l15.example.json", "l2": "ab_manifest.l2.example.json", "l3": "ab_manifest.l3.example.json"}
    if layer not in examples:
        raise ValueError(f"不支援的測試層級：{layer}")
    example = REPO_ROOT / "tests" / "acceptance" / "examples" / examples[layer]
    doc = json.loads(example.read_text(encoding="utf-8"))
    doc["run_id"] = f"test-{layer.replace('.', '-')}-{platform}"
    doc["layer"] = layer
    doc["platform"] = platform
    doc["stage"] = stage
    doc["contract_revision"] = "0" * 40

    lock = json.loads((root / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))
    case_set = "slice_2.4"
    doc["required_case_set"] = case_set
    case_rows = yaml.safe_load((root / "tests/acceptance/cases.yaml").read_text(encoding="utf-8"))["cases"]
    case_by_id = {row["case_id"]: row for row in case_rows}
    doc["case_ids"] = [
        case_id for case_id in lock["sets"][case_set]
        if case_id in case_by_id
        and str(case_by_id[case_id].get("layer", "")).casefold() == layer
        and platform in case_by_id[case_id].get("platforms", [])
    ]

    def frozen_file(rel: str) -> dict[str, str]:
        path = root / rel
        return {"path": rel, "sha256": sha256_file(path)}

    doc["case_list"] = frozen_file("tests/acceptance/cases.yaml")
    doc["case_lock"] = frozen_file("tests/acceptance/cases.lock.json")
    doc["allowed_differences"] = frozen_file("contracts/behavior_changes.yaml")

    tree_paths = {
        "l1.5": ["tools/stage3", "tools/uart_stdio_bridge", "func-test"],
        "l2": ["tools/stage3", "regression"],
        "l3": ["tools/stage3", "realhw"],
    }[layer]
    doc["tools"] = [{"path": rel, "tree_sha256": tree_sha256(root / rel)} for rel in tree_paths]
    profile = frozen_file("profiles/default.yaml")
    doc["profile_files"] = [profile]

    if layer == "l3":
        doc["acceptance"] = frozen_file("realhw/acceptance/longrun_acceptance.yaml")
    else:
        doc["acceptance"] = None

    use_exe = ".exe" if platform == "windows" else ""
    side_rows = []
    for label, role, daemon_impl in (("A", "baseline", "python"), ("B", "candidate", "rust")):
        bundle = make_bundle(root / "bundles" / label, version="0.9.0", stage=stage,
                             daemon_impl=daemon_impl, target=("x86_64-pc-windows-gnu" if platform == "windows" else "x86_64-unknown-linux-gnu"))
        rel_bundle = bundle.relative_to(root).as_posix()
        cli_rel = f"{rel_bundle}/bin/serialwrap{use_exe}"
        daemon_rel = f"{rel_bundle}/bin/serialwrapd{use_exe}"
        side_rows.append(
            {
                "label": label,
                "role": role,
                "bundle_dir": rel_bundle,
                "release_manifest": frozen_file(f"{rel_bundle}/release-manifest.json"),
                "cli_exe": cli_rel,
                "daemon_exe": daemon_rel,
                "activate_argv": [cli_rel, "setup"],
                "deactivate_argv": [cli_rel, "setup", "--rollback"],
            }
        )
    doc["sides"] = side_rows

    if layer == "l1.5":
        doc["suite"] = {"adapter": "l15_functest", "timeout_s": 3600,
                        "selftest_report": frozen_file("reports/bridge-selftest.json"),
                        "testbed_template": None, "realhw_config_template": None, "tier": None, "extra_args": []}
    elif layer == "l2":
        doc["suite"] = {"adapter": "l2_regression", "timeout_s": 7200, "selftest_report": None,
                        "testbed_template": frozen_file("regression/testbed.template.yaml"), "realhw_config_template": None,
                        "tier": None, "extra_args": []}
    else:
        doc["suite"] = {"adapter": "l3_realhw", "timeout_s": 180000, "selftest_report": None,
                        "testbed_template": None, "realhw_config_template": frozen_file("realhw/config.json"),
                        "tier": "longrun", "extra_args": []}
    dut_kind = "local-process" if layer == "l1.5" else "hardware"
    dut_transport = "posix-pty" if layer == "l1.5" else "physical-usb-uart"
    boards = [] if layer == "l1.5" else ["TEST0", "TEST1"]
    doc["dut"] = {"kind": dut_kind, "transport": dut_transport, "com_pair": None,
                  "image": None, "image_digest": None, "boards": boards, "reset_argv": [],
                  "fingerprint_argv": [], "expected_fingerprint": {}}
    doc["authorization"] = {
        "allow_destructive": layer == "l2",
        "decision_ref": "FAKE-L2-TEST-AUTH" if layer == "l2" else None,
        "boards": ["TEST0"] if layer == "l2" else [],
    }
    doc["release"] = {"timeout_s": 120, "poll_s": 1, "endpoints": [], "device_paths": [],
                      "lock_paths": [], "device_open_probe": layer == "l1.5", "accept_unreadable_holders": False}
    doc["endpoint"] = None if layer != "l3" else "explicit-endpoint-placeholder"
    doc["failure_retention"] = "on-demand" if layer == "l3" else None
    for dotted, value in overrides.items():
        _set_dotted(doc, dotted, value)
    out = root / "ab-manifest.json"
    _write_json(out, doc)
    return out


def make_stub_cli(dir: Path, scenario: Mapping[str, list[dict[str, Any]]], *, name: str = "serialwrap") -> Path:
    """寫入只依 scenario 回應的 stdlib CLI stub，並記錄呼叫與序列位置。"""
    dir = Path(dir)
    dir.mkdir(parents=True, exist_ok=True)
    scenario_path = dir / "stub-scenario.json"
    _write_json(scenario_path, {key: list(value) for key, value in scenario.items()})
    script_path = dir / f"{name}.py"
    script = '''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
scenario = json.loads((root / "stub-scenario.json").read_text(encoding="utf-8"))
state_path = root / "stub-state.json"
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
argv = sys.argv[1:]
with (root / "calls.ndjson").open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": argv}, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\\n")
global_flags = {"--socket", "--endpoint", "--retries", "--timeout"}
filtered = []
skip = False
for arg in argv:
    if skip:
        skip = False
        continue
    if arg in global_flags:
        skip = True
        continue
    if arg == "-v":
        continue
    filtered.append(arg)
key = " ".join(filtered)
rows = scenario.get(key, [{"rc": 0, "stdout": "{}", "stderr": ""}])
idx = state.get(key, 0)
item = rows[min(idx, len(rows) - 1)] if rows else {"rc": 0, "stdout": "{}", "stderr": ""}
state[key] = idx + 1
state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
out = item.get("stdout", "")
if not isinstance(out, str):
    out = json.dumps(out, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
err = item.get("stderr", "")
if not isinstance(err, str):
    err = str(err)
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(int(item.get("rc", 0)))
'''
    script_path.write_text(script, encoding="utf-8", newline="\n")
    if os.name == "nt":
        cmd = dir / f"{name}.cmd"
        cmd.write_text(f'@echo off\r\npy -3 "%~dp0{name}.py" %*\r\n', encoding="utf-8", newline="")
        return cmd
    script_path.chmod(0o755)
    return script_path


def read_calls(dir: Path) -> list[dict[str, Any]]:
    """讀取 CLI stub 所追加的呼叫紀錄。"""
    path = Path(dir) / "calls.ndjson"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class FakeClock:
    """可同時推進單調與 UTC 時鐘的測試時鐘。"""

    def __init__(
        self,
        start_mono: float = 1000.0,
        start_wall: str = "2026-11-02T00:00:00+00:00",
    ) -> None:
        self._mono = float(start_mono)
        self._wall = datetime.fromisoformat(start_wall)

    def now(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        delta = float(seconds)
        self._mono += delta
        self._wall += timedelta(seconds=delta)

    def iso(self) -> str:
        return self._wall.isoformat()


class FakeProbe:
    """依序回傳預設探測值的 ReleaseProbe 替身。"""

    def __init__(
        self,
        *,
        pid_alive: dict[int, list[bool | None]] | None = None,
        descendants: dict[int, list[tuple[int, ...] | None]] | None = None,
        endpoint: dict[str, list[bool | None]] | None = None,
        lock: dict[str, list[bool | None]] | None = None,
        holders: dict[str, list[tuple[tuple[int, ...], int] | None]] | None = None,
        exclusive: dict[str, list[bool | None]] | None = None,
        by_exe: dict[str, list[tuple[int, ...] | None]] | None = None,
    ) -> None:
        self._sequences = {
            "pid_alive": pid_alive or {},
            "descendants": descendants or {},
            "endpoint": endpoint or {},
            "lock": lock or {},
            "holders": holders or {},
            "exclusive": exclusive or {},
            "by_exe": by_exe or {},
        }
        self._indices: dict[tuple[str, object], int] = {}
        self.calls: list[tuple[str, object]] = []

    def _next(self, method: str, key: object, default: T) -> T:
        self.calls.append((method, key))
        sequence = self._sequences[method].get(key)
        if not sequence:
            return default
        index_key = (method, key)
        index = self._indices.get(index_key, 0)
        self._indices[index_key] = index + 1
        return sequence[min(index, len(sequence) - 1)]

    def pid_alive(self, pid: int) -> bool | None:
        return self._next("pid_alive", pid, False)

    def descendants(self, pid: int) -> tuple[int, ...] | None:
        return self._next("descendants", pid, ())

    def pids_by_exe(self, match_paths: Sequence[str]) -> tuple[int, ...] | None:
        key = match_paths[0] if match_paths else ""
        return self._next("by_exe", key, ())

    def endpoint_connectable(self, endpoint: str) -> bool | None:
        return self._next("endpoint", endpoint, False)

    def lock_acquirable(self, path: str) -> bool | None:
        return self._next("lock", path, True)

    def device_holders(self, path: str) -> tuple[tuple[int, ...], int] | None:
        return self._next("holders", path, ((), 0))

    def device_exclusive_open(self, path: str) -> bool | None:
        return self._next("exclusive", path, True)


class RecordingRunner:
    """記錄外部命令並依命令分類回傳合成結果。"""

    _VALUE_FLAGS = {
        "--selector", "--cmd", "--cmd-id", "--cmd-timeout", "--instance", "--report-dir",
        "--seed", "--side", "--round", "--source", "--reason", "--a", "--b", "--out",
        "--tier", "--mode", "--dut-mode", "--selftest-report", "--endpoint", "--socket",
        "--case-dir", "--acceptance", "--platform", "--timeout", "--retries", "-C",
    }

    def __init__(self, script: Mapping[str, list[dict[str, Any]]] | None = None) -> None:
        self.script = {key: list(value) for key, value in (script or {}).items()}
        self.calls: list[dict[str, Any]] = []
        self._positions: dict[str, int] = {}
        self._effects: list[tuple[str, Callable[[list[str], Mapping[str, str]], None]]] = []
        self._raises: list[tuple[str, BaseException]] = []

    @staticmethod
    def _program_index(argv: Sequence[str]) -> int:
        if len(argv) > 1 and re.fullmatch(r"(?:python[0-9.]*|py)(?:\.exe)?", Path(argv[0]).name, re.IGNORECASE):
            return 1
        return 0

    @classmethod
    def _classify(cls, argv: Sequence[str]) -> tuple[str, str]:
        if not argv:
            return "other", ""
        index = cls._program_index(argv)
        program = Path(argv[index]).name.casefold()
        if program == "-m" and index + 1 < len(argv):
            kind = argv[index + 1]
            index += 2
        else:
            base = program.removesuffix(".exe")
            if base in {"serialwrap", "serialwrapd"}:
                kind = "cli"
            elif base in {"schtasks", "systemctl", "git", "docker", "testpilot"}:
                kind = base
            elif base == "runner.py":
                kind = "functest"
            elif base == "python" or base.startswith("python") or base == "py":
                kind = "other"
            else:
                kind = "other"
            index += 1

        words: list[str] = []
        skip_value = False
        for token in argv[index:]:
            if skip_value:
                skip_value = False
                continue
            if token in cls._VALUE_FLAGS:
                skip_value = True
                continue
            if token.startswith("-"):
                continue
            if "/" in token or "\\" in token:
                continue
            words.append(token)
            if len(words) == 2:
                break
        return kind, " ".join(words)

    def raise_on(self, prefix: str, exc: BaseException) -> None:
        self._raises.append((prefix, exc))

    def on(self, prefix: str, effect: Callable[[list[str], Mapping[str, str]], None]) -> None:
        self._effects.append((prefix, effect))

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        cwd: Path | None = None,
    ) -> CmdResult:
        call_argv = list(argv)
        call_env = dict(env or {})
        kind, sub = self._classify(call_argv)
        key = f"{kind}:{sub}"
        call = {
            "kind": kind,
            "sub": sub,
            "argv": call_argv,
            "env": call_env,
            "side": call_env.get("SW_STAGE3_SIDE"),
            "round": call_env.get("SW_STAGE3_ROUND"),
            "timeout": timeout,
            "cwd": cwd,
        }
        self.calls.append(call)
        for prefix, exc in self._raises:
            if key.startswith(prefix):
                raise exc
        for prefix, effect in self._effects:
            if key.startswith(prefix):
                effect(call_argv, call_env)

        script_key = next((name for name in self.script if key.startswith(name)), None)
        if script_key is None:
            result = {"rc": 0, "stdout": "{}", "stderr": ""}
            position = 0
        else:
            sequence = self.script[script_key]
            position = self._positions.get(script_key, 0)
            result = sequence[min(position, len(sequence) - 1)] if sequence else {"rc": 0, "stdout": "{}", "stderr": ""}
            self._positions[script_key] = position + 1
        return CmdResult(
            rc=int(result.get("rc", 0)),
            stdout=str(result.get("stdout", "")),
            stderr=str(result.get("stderr", "")),
            elapsed_s=float(result.get("elapsed_s", 0.0)),
            timed_out=bool(result.get("timed_out", False)),
        )

    def spawn(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> "FakeProc":
        return FakeProc(argv, env=env, cwd=cwd)


class FakeProc:
    """提供可控制狀態的程序句柄替身。"""

    _next_pid = 10000

    def __init__(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> None:
        self.argv = tuple(argv)
        self.env = dict(env or {})
        self.cwd = cwd
        self.pid = FakeProc._next_pid
        FakeProc._next_pid += 1
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._returncode = -15

    def kill(self) -> None:
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self._returncode is None:
            self._returncode = 0
        return self._returncode


def empty_index() -> dict[str, Any]:
    """Task 14 證據索引的最小形狀（無任何 entry），供消費端測試使用。"""
    return {"entries": [], "schema_version": 2, "stage_targets": {}, "target_history": []}


def index_with_entry(item_id: str, *, platform: str, started_at: str, result: str = "PASS") -> dict[str, Any]:
    """只含單一合成 entry 的證據索引；entry 僅帶消費端判定所需的鍵（item_id、platform、started_at、result）。"""
    index = empty_index()
    index["entries"].append({
        "entry_id": f"{item_id}-{platform}-{started_at}",
        "item_id": item_id,
        "platform": platform,
        "result": result,
        "started_at": started_at,
    })
    return index
