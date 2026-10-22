"""Task 5B（suite adapter）專屬替身：引擎報告樹與 release-gate.json 的產生器。

本模組不是測試檔（檔名不以 `test_` 開頭），由 `tests/test_stage3_suites.py` 以延遲 import 載入。
替身只產生「引擎應寫出的檔案」，不實作 adapter 的判定邏輯：

- L1.5：`func-test/runner.py` 的 `<report-dir>/<case>/bridge/<n>/` 樹（manifest.json、comparison.json、
  bridge-manifest.json、wire.ndjson、cli.ndjson、daemon.log、wal/）。
- L2：TestPilot 在 `~/b-log/regression-reports/tp-<n>/` 產生的 `report.json`。
- L3：`python3 -m realhw --report-dir …` 的 `report.json`，以及 `realhw.acceptance.gate` 的 `release-gate.json`。

已部署內容一律以 P2 `realhw.deployed_content.measure` 真實量測（R5-F4），不寫死 digest。
替身只依賴 Task 2 基礎替身（`stage3_fakes`）與既有 P2 模組；`tools.stage3` 的模組一律於函式內延遲 import。
"""
from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import yaml  # noqa: E402  PyYAML 為執行期依賴（testbed／realhw 設定模板為 YAML）

import stage3_fakes as fakes  # noqa: E402  Task 2 基礎替身（RecordingRunner、write_ab_manifest、tree_sha256）
from realhw import deployed_content as dc  # noqa: E402  P2 Task 10，既有實作

NOW = "2026-11-02T00:00:00+00:00"
DAEMON_VERSION = "0.9.0"


def _result(rc: int, stdout: str, stderr: str = "") -> Any:
    """建立 `tools.stage3.proc.CmdResult`（Task 1）；延遲 import，缺模組時於呼叫點失敗。"""
    from tools.stage3.proc import CmdResult

    return CmdResult(rc=rc, stdout=stdout, stderr=stderr, elapsed_s=0.0, timed_out=False)


def _ok(stdout: str) -> dict[str, Any]:
    return {"rc": 0, "stdout": stdout, "stderr": ""}


def _flag(argv: Sequence[str], flag: str) -> str | None:
    """回傳 argv 中 `flag` 之後的第一個值；缺旗標時回 None。"""
    items = list(argv)
    if flag not in items:
        return None
    idx = items.index(flag)
    return items[idx + 1] if idx + 1 < len(items) else None


def _flag_values(argv: Sequence[str], flag: str) -> list[str]:
    items = list(argv)
    return [items[index + 1] for index, token in enumerate(items[:-1]) if token == flag]


# ---------------------------------------------------------------------------
# 引擎替身執行器
# ---------------------------------------------------------------------------


class _TracedProc:
    """包住 `FakeProc`，把 terminate／kill 記入 `runner.calls`（kind="proc"），供驗證 observer 終止順序。"""

    def __init__(self, inner: Any, runner: "EngineRunner", argv: list[str], env: dict[str, str]) -> None:
        self._inner = inner
        self._runner = runner
        self._argv = argv
        self._env = env

    @property
    def pid(self) -> int:
        return self._inner.pid

    def poll(self) -> int | None:
        return self._inner.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self._inner.wait(timeout)

    def terminate(self) -> None:
        self._runner._note_proc("terminate", self._argv, self._env)
        self._inner.terminate()

    def kill(self) -> None:
        self._runner._note_proc("kill", self._argv, self._env)
        self._inner.kill()


class EngineRunner(fakes.RecordingRunner):
    """`RecordingRunner` 加上兩項引擎專用行為。

    - `cli daemon status`：依 `SW_STAGE3_SIDE` 回傳該側的 `pid`（TST-D-12：PID 取 `daemon status.pid`）。
    - `spawn`：回傳包裝後的程序，終止時寫入 `calls`（kind="proc"，sub="terminate"）。
    """

    def __init__(self, script: Mapping[str, list[dict]] | None = None, *,
                 daemon_pids: Mapping[str, int] | None = None) -> None:
        super().__init__(script)
        self.daemon_pids: dict[str, int] = dict(daemon_pids or {})

    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None,
                 timeout: float | None = None, cwd: Path | None = None) -> Any:
        res = super().__call__(argv, env=env, timeout=timeout, cwd=cwd)
        last = self.calls[-1]
        side = last.get("side")
        if last["kind"] == "functest" and res.rc == 1:
            report_value = _flag(argv, "--report-dir")
            if report_value:
                for comparison_path in Path(report_value).glob("*/bridge/*/comparison.json"):
                    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
                    if comparison.get("verdict") == "PASS":
                        comparison["verdict"] = "FAIL"
                        comparison_path.write_text(
                            json.dumps(comparison, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
                            encoding="utf-8",
                        )
        if last["kind"] == "cli" and last["sub"] == "daemon status" and side in self.daemon_pids:
            body = json.dumps({"ok": True, "pid": self.daemon_pids[side], "version": DAEMON_VERSION,
                               "state": "running"}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            res = _result(res.rc, body, res.stderr)
        return res

    def spawn(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None,
              cwd: Path | None = None) -> _TracedProc:
        proc = super().spawn(argv, env=env, cwd=cwd)
        kind, _ = self._classify(argv)
        if kind == "realhw.acceptance.observer":
            header_path = Path(_flag(argv, "--out") or "")
            boards = [item for item in (_flag(argv, "--boards") or "").split(",") if item]
            is_windows = "--excl-dir" in argv
            binding = {board: (f"COM{index + 20}" if is_windows else f"/dev/fake/{board}")
                       for index, board in enumerate(boards)}
            header_path.parent.mkdir(parents=True, exist_ok=True)
            header_path.write_text(json.dumps({"kind": "header", "device_binding": binding}) + "\n",
                                   encoding="utf-8")
        call_env = dict(env or {})
        kind, sub = self._classify(argv)
        self.calls.append({"kind": kind, "sub": sub, "argv": list(argv), "env": call_env,
                           "side": call_env.get("SW_STAGE3_SIDE"),
                           "round": call_env.get("SW_STAGE3_ROUND"), "cwd": cwd})
        return _TracedProc(proc, self, list(argv), call_env)

    def _note_proc(self, sub: str, argv: list[str], env: Mapping[str, str]) -> None:
        self.calls.append({"kind": "proc", "sub": sub, "argv": list(argv), "env": dict(env),
                           "side": env.get("SW_STAGE3_SIDE"), "round": env.get("SW_STAGE3_ROUND")})


# ---------------------------------------------------------------------------
# 可調整的引擎行為（各測試於 setup 後可直接修改）
# ---------------------------------------------------------------------------


@dataclass
class Knobs:
    """引擎替身的行為旋鈕；effect 於呼叫當下讀取，測試可在 run 之前任意修改。"""

    python_default: bool = False          # True：不論實際部署一律回報 Python 基線身分（R5 負向測試）
    deployed: str = "measure"             # "measure" | "omit"（不寫 deployed_content）| "unresolved" | "end_none"
    sideload_root: Path | None = None     # 設定時：該側 bundle 複製到 <root>/versions/v 並加寫旁載檔，量測該安裝
    sideload_side: str = "B"               # 只在指定側模擬旁載安裝
    sideload_extra: bytes = b"MZ"         # 旁載檔內容（bin/version.dll）
    between: Callable[[int], None] | None = None  # 每組 start／end 量測之間呼叫，參數為本 knobs 內第幾組（從 0 起）
    pinned: bool = True                   # CLI 身分 pinned 欄位
    case_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)  # L1.5 每案覆寫：closed_cleanly、dut_generation
    testpilot_verdicts: dict[str, str] = field(default_factory=dict)  # L2 依側覆寫逐案判定
    testpilot_new_dirs: int = 1           # TestPilot 每次呼叫新增的 tp-* 目錄數
    testpilot_authorization_override: dict[str, Any] | None = None
    baseline_gate_result: str = "NOT_APPLICABLE_BASELINE"
    candidate_gate_result: str = "PASS"
    baseline_engine_verdict: str = "PASS"
    candidate_engine_verdict: str = "PASS"
    baseline_result: str = "PASS"
    omit_realhw_report: bool = False
    pair_count: int = 0                   # 內部計數：已執行的 start／end 量測組數


def _pair(knobs: Knobs, cli: Path, *,
          daemon_live: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any] | None:
    """以 P2 deployed_content.bracket 的形狀回傳 start／end 量測；deployed="omit" 時回 None。"""
    if knobs.deployed == "omit":
        return None
    idx = knobs.pair_count
    knobs.pair_count += 1
    start = dc.measure(cli)
    if knobs.deployed == "unresolved":
        start = {**start, "error": "unresolved", "bundle_tree_sha256": None}
    if knobs.between is not None:
        knobs.between(idx)
    end = None if knobs.deployed == "end_none" else dc.measure(cli)
    return {"start": start, "end": end, "daemon_live": [dict(item) for item in daemon_live]}


def _cli_to_run(knobs: Knobs, label: str, sides: Mapping[str, Any], cli_cfg: Path) -> Path:
    """回傳引擎實際執行的 CLI 路徑。sideload 時為複製到 <sideload_root>/versions/v 的安裝。"""
    if knobs.sideload_root is None or label != knobs.sideload_side:
        return cli_cfg
    src = sides[label].bundle_dir
    dest = Path(knobs.sideload_root) / "versions" / "v"
    if not dest.exists():
        shutil.copytree(src, dest)
        (dest / "bin" / "version.dll").write_bytes(knobs.sideload_extra)
    return dest / "bin" / "serialwrap"


def _cli_identity(configured: str, resolved: Path, pinned: bool) -> dict[str, Any]:
    """與 realhw/preflight.resolve_serialwrap_exe 相同的形狀。"""
    return {"configured": configured, "resolved": str(resolved), "sha256": dc.file_sha256(resolved),
            "pinned": pinned}


def _artifacts(label: str, cli_run: Path, daemon: Path, sides: Mapping[str, Any], knobs: Knobs) -> dict[str, Any]:
    """L1.5 每案 manifest.json 的 `artifacts` 區塊（欄位名取自 func-test/lib/artifact_identity.py）。"""
    if knobs.python_default:
        # 模擬外層環境退回 Python repo daemon（非本側 bundle launcher）。
        actual_daemon = Path(__file__).resolve().parents[1] / "serialwrapd.py"
        daemon_block = {
            "argv": [sys.executable, str(actual_daemon)],
            "sha256": None,
            "implementation": "python",
            "package_tree_sha256": "f" * 64,
            "repo_tree_sha256": "e" * 64,
        }
    else:
        actual_daemon = daemon
        # func-test 只辨識 serialwrapd.py／-m sw_core.daemon；bundle Python launcher
        # 會走 executable 分支，實際輸出 implementation=rust 與 launcher SHA。
        daemon_block = {
            "argv": [str(actual_daemon)],
            "sha256": dc.file_sha256(actual_daemon),
            "implementation": "rust",
            "package_tree_sha256": None,
            "repo_tree_sha256": None,
        }
    return {
        "cli": {"path": str(cli_run), "sha256": dc.file_sha256(cli_run), "version_stdout": "serialwrap 0.9.0\n"},
        "daemon": daemon_block,
        "release_manifest_sha256": dc.file_sha256(cli_run.parent.parent / "release-manifest.json"),
    }


def _sides_by_label(m: Any) -> dict[str, Any]:
    return {s.label: s for s in m.sides}


# ---------------------------------------------------------------------------
# 各引擎的 effect（RecordingRunner.on 的 effect：(argv, env) -> None）
# ---------------------------------------------------------------------------


def l15_functest(case_ids: Sequence[str], sides: Mapping[str, Any], knobs: Knobs) -> Callable[..., None]:
    """`func-test/runner.py --mode bridge` 的報告樹產生器。"""

    def effect(argv: Sequence[str], env: Mapping[str, str]) -> None:
        report = Path(_flag(argv, "--report-dir") or "")
        label = env["SW_STAGE3_SIDE"]
        cli_run = _cli_to_run(knobs, label, sides, Path(env["SERIALWRAP_CLI_EXE"]))
        artifacts = _artifacts(label, cli_run, Path(env["SERIALWRAP_DAEMON_EXE"]), sides, knobs)
        selected_ids = [*_flag_values(argv, "--case"), *_flag_values(argv, "--scenario-case")]
        if not selected_ids:
            selected_ids = list(case_ids)
        for case in selected_ids:
            ov = dict(knobs.case_overrides.get(case, {}))
            case_artifacts = dict(artifacts)
            bracket = _pair(knobs, cli_run)
            if bracket is not None:
                case_artifacts["deployed_content"] = bracket
            case_dir = report / case / "bridge" / "1"
            case_dir.mkdir(parents=True, exist_ok=True)
            gen = int(ov.get("dut_generation", 1))
            manifest = {
                "schema_version": "l15-manifest.v1",
                "run_id": f"{env.get('SERIALWRAP_AB_LABEL', 'ab')}-{case}",
                "case_id": case,
                "created_at": NOW,
                "mode": "bridge",
                "topology": "dut_local_process_daemon_native",
                "transport": {
                    "mode": "virtual_pty",
                    "port_id": "uart0",
                    "daemon_side": None,
                    "bridge_side": None,
                },
                "artifacts": case_artifacts,
                "tool": {"name": "uart_stdio_bridge", "version": "0.1.0", "tree_sha256": "e" * 64,
                         "approved": False},
                "dut": {"mode": "local-process", "image": None, "image_id": None, "target_sha256": "a" * 64,
                        "scenario_sha256": None, "seed": 1},
                "repo_git_revision": None,
                "repo_dirty": False,
                "contract_revision": None,
                "contract_dirty": False,
                "cases_yaml_sha256": None,
                "case": {"source_path": f"func-test/cases/{case}.py", "sha256": "c" * 64, "revision": None},
                "platform": {"os": "Linux", "release": "6.6", "version": "x", "machine": "x86_64",
                             "python": "3.11", "evidence_insufficient": []},
                "bridge_manifest_sha256": None,
                "evidence": {"wire": None, "control": None, "correlation": None, "dut_stderr": None,
                             "wal_dir": "wal", "cli_log": None},
            }
            (case_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8")
            (case_dir / "comparison.json").write_text(
                json.dumps({"schema_version": "comparison.v1", "verdict": ov.get("verdict", "PASS"), "streams": [],
                            "invariants": []}, sort_keys=True), encoding="utf-8")
            (case_dir / "bridge-manifest.json").write_text(
                json.dumps({"closed_cleanly": bool(ov.get("closed_cleanly", True)), "failure": None},
                           sort_keys=True), encoding="utf-8")
            wire_lines = [json.dumps({"dut_generation": gen, "seq": seq}, sort_keys=True) for seq in (1, 2)]
            (case_dir / "wire.ndjson").write_text("\n".join(wire_lines) + "\n", encoding="utf-8")
            if not ov.get("omit_cli_log"):
                endpoint = str(case_dir / "run" / "serialwrapd.sock")
                (case_dir / "cli.ndjson").write_text(
                    json.dumps({"argv": [env["SERIALWRAP_CLI_EXE"], "--socket", endpoint,
                                        "daemon", "stop"], "rc": 0}, sort_keys=True) + "\n",
                    encoding="utf-8")
            (case_dir / "daemon.log").write_text("", encoding="utf-8")
            (case_dir / "wal").mkdir(exist_ok=True)

    return effect


def _identity_meta(knobs: Knobs, cli_cfg_str: str, cli_run: Path,
                   daemon_image: Path | None = None) -> dict[str, Any]:
    daemon_identity: dict[str, Any] = {"version": DAEMON_VERSION}
    if daemon_image is not None:
        daemon_identity.update({"image_path": str(daemon_image),
                                "image_sha256": dc.file_sha256(daemon_image)})
    return {"cli_identity": _cli_identity(cli_cfg_str, cli_run, knobs.pinned),
            "daemon_identity": daemon_identity}


def testpilot(sides: Mapping[str, Any], knobs: Knobs, case_ids: Sequence[str] = ()) -> Callable[..., None]:
    """TestPilot `serialwrap_regression`：在 ~/b-log/regression-reports/ 新增 tp-<n>/report.json。"""

    def effect(argv: Sequence[str], env: Mapping[str, str]) -> None:
        label = env["SW_STAGE3_SIDE"]
        tb = yaml.safe_load(Path(env["SERIALWRAP_REGRESSION_TESTBED"]).read_text(encoding="utf-8")) or {}
        cli_cfg = Path(tb["serialwrap_exe"])
        cli_run = _cli_to_run(knobs, label, sides, cli_cfg)
        base = Path(env["HOME"]) / "b-log" / "regression-reports"
        base.mkdir(parents=True, exist_ok=True)
        for _ in range(knobs.testpilot_new_dirs):
            existing = [int(p.name.split("-", 1)[1]) for p in base.glob("tp-*")
                        if p.name.split("-", 1)[1].isdigit()]
            new_dir = base / f"tp-{(max(existing) + 1) if existing else 0}"
            new_dir.mkdir()
            meta = _identity_meta(knobs, str(cli_cfg), cli_run)
            meta["allow_destructive"] = bool(tb.get("allow_destructive"))
            meta["authorized_boards"] = list(tb.get("authorized_boards") or [])
            if knobs.testpilot_authorization_override is not None:
                meta.update(knobs.testpilot_authorization_override)
            daemon_component = sides[label].manifest_doc.get("components", {}).get("daemon", {})
            live: list[dict[str, Any]] = []
            if daemon_component.get("implementation") == "rust":
                daemon_image = cli_run.parent / Path(sides[label].daemon_exe).name
                live = [{
                    "pid": 4101 if label == "A" else 4202,
                    "image_path": str(daemon_image.resolve()),
                    "image_sha256": dc.file_sha256(daemon_image),
                    "in_install": True,
                    "error": None,
                }]
            bracket = _pair(knobs, cli_run, daemon_live=live)
            if bracket is not None:
                meta["deployed_content"] = bracket
            (new_dir / "report.json").write_text(
                json.dumps({
                    "marker": "new",
                    "meta": meta,
                    "results": [
                        {
                            "id": case_id,
                            "verdict": knobs.testpilot_verdicts.get(label, "PASS"),
                            "reason": None,
                            "category": None,
                            "reason_code": None,
                            "evidence": {},
                        }
                        for case_id in case_ids
                    ],
                }, ensure_ascii=False, indent=1),
                encoding="utf-8")

    return effect


def realhw(sides: Mapping[str, Any], knobs: Knobs) -> Callable[..., None]:
    """`python3 -m realhw --config … --report-dir …` 的 report.json（meta.cli_identity／meta.deployed_content）。"""

    def effect(argv: Sequence[str], env: Mapping[str, str]) -> None:
        label = env["SW_STAGE3_SIDE"]
        cfg = json.loads(Path(_flag(argv, "--config") or "").read_text(encoding="utf-8"))
        cli_cfg = Path(cfg["serialwrap_exe"])
        cli_run = _cli_to_run(knobs, label, sides, cli_cfg)
        report_dir = Path(_flag(argv, "--report-dir") or "")
        report_dir.mkdir(parents=True, exist_ok=True)
        if knobs.omit_realhw_report:
            return
        meta = _identity_meta(knobs, str(cli_cfg), cli_run, sides[label].daemon_exe)
        meta["started_at_utc"] = NOW
        bracket = _pair(knobs, cli_run)
        if bracket is not None:
            meta["deployed_content"] = bracket
        (report_dir / "report.json").write_text(
            json.dumps({"meta": meta, "results": []}, ensure_ascii=False, indent=1), encoding="utf-8")

    return effect


def gate(knobs: Knobs, *, platform: str = "linux") -> Callable[..., None]:
    """`realhw.acceptance.gate`：依同目錄 report.json 寫出 release-gate.v1（run.cli_identity／deployed_content）。"""

    def effect(argv: Sequence[str], env: Mapping[str, str]) -> None:
        out = Path(_flag(argv, "--out") or "")
        report_json = Path(_flag(argv, "--report-dir") or "") / "report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        if not report_json.is_file():
            doc = {"schema_version": "release-gate.v1", "release_gate_result": "INSUFFICIENT_EVIDENCE",
                   "run": {}}
        else:
            meta = json.loads(report_json.read_text(encoding="utf-8"))["meta"]
            mode = _flag(argv, "--mode")
            verdict = knobs.baseline_gate_result if mode == "baseline" else knobs.candidate_gate_result
            engine_verdict = (knobs.baseline_engine_verdict if mode == "baseline"
                              else knobs.candidate_engine_verdict)
            doc = {
                "schema_version": "release-gate.v1",
                "platform": platform,
                "mode": mode,
                "engine_result": {"verdict": engine_verdict},
                "release_gate_result": verdict,
                "baseline_result": knobs.baseline_result,
                "acceptance": {"revision": "2026-10-15.1"},
                "run": {
                    "cli_identity": meta.get("cli_identity"),
                    "deployed_content": meta.get("deployed_content"),
                    "daemon_version": (meta.get("daemon_identity") or {}).get("version"),
                },
                "checks": [
                    {"id": "evidence_present", "group": "evidence", "result": "PASS"},
                    {"id": "safety", "group": "safety", "result": "PASS"},
                    *([{"id": "baseline_compatible", "group": "evidence", "result": "PASS"}]
                      if mode == "candidate" else []),
                ],
            }
        out.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")

    return effect


def register_engines(runner: EngineRunner, m: Any, knobs: Knobs) -> None:
    """把各引擎的 effect 掛到 runner；effect 讀取同一個 knobs，測試可於 run 前修改。"""
    sides = _sides_by_label(m)
    runner.on("functest:", l15_functest(tuple(m.case_ids), sides, knobs))
    runner.on("testpilot:", testpilot(sides, knobs, tuple(m.case_ids)))
    runner.on("realhw:", realhw(sides, knobs))
    runner.on("realhw.acceptance.gate:", gate(knobs, platform=m.platform))


def default_script(layer: str, platform: str) -> dict[str, list[dict]]:
    """L3 監管檢查的預設回應：on-demand、無已註冊監管（Windows 另無 schtasks 工作）。"""
    script: dict[str, list[dict]] = {}
    if layer == "l3":
        script["cli:supervision-mode"] = [_ok("on-demand\n")]
        script["cli:service status"] = [{"rc": 2, "stdout": json.dumps(
            {"ok": False, "error_code": "NO_SYSTEMD", "mode": "on-demand"}, sort_keys=True),
            "stderr": ""}]
        # RecordingRunner 分類略過 `--user` 旗標：system 與 user 查詢共用此鍵
        script["systemctl:list-unit-files"] = [_ok("")]
        if platform == "windows":
            script["schtasks:"] = [_ok("")]
    return script


def rc_entry(rc: int) -> dict[str, Any]:
    return {"rc": rc, "stdout": "", "stderr": ""}


# ---------------------------------------------------------------------------
# 測試用組裝輔助
# ---------------------------------------------------------------------------


@dataclass
class SuiteEnv:
    """一個 suite 測試的組裝結果。`instance`／`run` 只做組裝，不做判定。"""

    root: Path
    m: Any
    runner: EngineRunner
    knobs: Knobs
    made: list[Any] = field(default_factory=list)

    def instance(self, idx: int, *, round: int = 1) -> Any:
        from tools.stage3 import dut_state

        inst = dut_state.fresh_instance(self.m, self.m.sides[idx], round=round, base_dir=self.root / "out")
        self.made.append(inst)
        return inst

    def run(self, idx: int, *, round: int = 1, probe: Any = None) -> tuple[Any, Any]:
        """以 `suites.run_side` 執行第 idx 側；probe 未給時使用 FakeProbe（不掃描真實程序）。"""
        from tools.stage3 import suites

        inst = self.instance(idx, round=round)
        suite_run = suites.run_side(
            self.m, self.m.sides[idx], inst,
            run=self.runner, spawn=self.runner.spawn, now=lambda: NOW,
            probe=probe if probe is not None else fakes.FakeProbe(),
            repo_root=self.root,
        )
        return inst, suite_run

    def expected(self, idx: int) -> dict[str, str]:
        """第 idx 側 manifest 宣告的 bundle／runtime 樹指紋（Task 3 演算法）。"""
        from tools.stage3 import ab_manifest

        side = self.m.sides[idx]
        return {
            "bundle_tree_sha256": ab_manifest.bundle_tree_sha256(side.bundle_dir),
            "runtime_tree_sha256": ab_manifest.runtime_tree_sha256(side.bundle_dir),
        }

    def calls(self, kind: str | None = None, side: str | None = None) -> list[dict[str, Any]]:
        out = list(self.runner.calls)
        if kind is not None:
            out = [c for c in out if c["kind"] == kind]
        if side is not None:
            out = [c for c in out if c.get("side") == side]
        return out

    def cleanup(self) -> None:
        from tools.stage3 import dut_state

        for inst in self.made:
            dut_state.cleanup_instance(inst)
        self.made.clear()


def make_suite(root: Path, *, layer: str = "l1.5", platform: str = "linux",
               script: Mapping[str, list[dict]] | None = None,
               daemon_pids: Mapping[str, int] | None = None,
               knobs: Knobs | None = None, **overrides: Any) -> SuiteEnv:
    """建立假 repo、A/B bundle 與 ab-manifest，載入 manifest 並掛好所有引擎替身。"""
    from tools.stage3 import ab_manifest

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = fakes.write_ab_manifest(root, layer=layer, platform=platform, **overrides)
    m = ab_manifest.load_manifest(manifest_path)
    knobs = knobs if knobs is not None else Knobs()
    merged = {**default_script(layer, platform), **dict(script or {})}
    runner = EngineRunner(merged, daemon_pids=daemon_pids or {"A": 4101, "B": 4202})
    register_engines(runner, m, knobs)
    return SuiteEnv(root=root, m=m, runner=runner, knobs=knobs)


@dataclass
class AbEnv:
    """Task 6（ab_runner）使用的組裝結果。"""

    repo: Path
    manifest: Path
    m: Any
    runner: EngineRunner
    a_daemon_pid: int
    b_daemon_pid: int
    knobs: Knobs

    def recorded(self) -> list[dict[str, Any]]:
        return list(self.runner.calls)


def ab_env(root: Path, *, layer: str = "l1.5", a_functest_rc: int = 0, b_functest_rc: int = 0,
           functest_identity: str = "per_env", a_pid: int = 4101, b_pid: int = 4202,
           **manifest_overrides: Any) -> AbEnv:
    """建立 Task 6 用的 A/B 組裝。

    `functest_identity="python_default"` 時，兩側的 L1.5 functest 一律寫 Python 基線身分（不論 env）；
    `"per_env"` 時依 env 指向的實際 artifact 寫身分。`functest:` 的 rc 依呼叫順序（A 先、B 後）回傳。
    """
    from tools.stage3 import ab_manifest

    if functest_identity not in {"per_env", "python_default"}:
        raise ValueError(f"unknown functest_identity: {functest_identity}")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest_overrides.setdefault("reverify.enabled", False)
    if layer == "l3":
        manifest_overrides.setdefault("authorization.allow_destructive", True)
        manifest_overrides.setdefault("authorization.decision_ref", "FAKE-L3-TEST-AUTH")
        manifest_overrides.setdefault("authorization.boards", ["TEST0", "TEST1"])
    manifest_path = fakes.write_ab_manifest(root, layer=layer, **manifest_overrides)
    normalization = root / "contracts" / "ab_normalization.yaml"
    normalization.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "contracts" / "ab_normalization.yaml", normalization)
    m = ab_manifest.load_manifest(manifest_path)
    knobs = Knobs(python_default=(functest_identity == "python_default"))
    script = default_script(layer, m.platform)
    script["cli:session list"] = [_ok(json.dumps({
        "ok": True,
        "sessions": [{"com": board, "state": "READY", "bridge_generation": 1}
                     for board in m.dut.boards],
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))]
    script.update({
        # verify_frozen 與 ab-report 都會查 Git revision；合成 repo 不含 .git，故以
        # manifest 的凍結契約 revision 與固定 40 位測試 revision 回應。
        "git:rev-parse HEAD:contracts": [{"rc": 0, "stdout": m.contract_revision, "stderr": ""}],
        "git:rev-parse HEAD": [{"rc": 0, "stdout": "1" * 40, "stderr": ""}],
        "functest:": [rc_entry(a_functest_rc), rc_entry(b_functest_rc)],
    })
    runner = EngineRunner(script, daemon_pids={"A": a_pid, "B": b_pid})
    register_engines(runner, m, knobs)
    return AbEnv(repo=root, manifest=manifest_path, m=m, runner=runner, a_daemon_pid=a_pid,
                 b_daemon_pid=b_pid, knobs=knobs)


def add_late_file(bundle: Path, idx: int) -> None:
    """模擬 suite 執行期間有人在實際部署樹加檔（用於「期間變動」負向測試）。"""
    target = bundle / "assets" / f"late-{idx}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"late {idx}\n", encoding="utf-8")


__all__ = [
    "AbEnv",
    "EngineRunner",
    "Knobs",
    "NOW",
    "SuiteEnv",
    "ab_env",
    "add_late_file",
    "default_script",
    "gate",
    "l15_functest",
    "make_suite",
    "realhw",
    "rc_entry",
    "register_engines",
    "testpilot",
]
