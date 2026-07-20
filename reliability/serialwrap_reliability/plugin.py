"""testpilot PluginBase glue——薄殼；邏輯集中在 core.py 與 testbed_loader.py。"""
from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

import yaml
from testpilot.api import PluginBase, PreparedRun

from serialwrap_reliability import core
from serialwrap_reliability.testbed_loader import testbed_to_cfg


class PreflightRefused(RuntimeError):
    """suite-refuse：preflight 缺項時整場拒跑。"""


class Plugin(PluginBase):
    """serialwrap-reliability：realhw 引擎的 testpilot 前端。"""

    api_version = "1.1"

    def __init__(self) -> None:
        self.run_results: list[tuple[str, Any]] = []
        self.run_meta: dict[str, Any] = {}
        self.ctx: Any = None
        self._cfg: dict[str, Any] | None = None
        self._missing_caps: dict[str, str] = {}
        self._benchlock_fd: int | None = None
        self._broken_by: str | None = None
        self._longruns: dict[str, core.LongrunRunner] = {}

    @property
    def name(self) -> str:
        return "serialwrap_reliability"

    def _plugin_root(self) -> Path:
        return Path(getattr(self, "plugin_root", Path(__file__).resolve().parent))

    def _load_cfg(self) -> dict[str, Any]:
        if self._cfg is None:
            raw = yaml.safe_load(
                (self._plugin_root() / "testbed.yaml.example").read_text(encoding="utf-8")
            ) or {}
            self._cfg = testbed_to_cfg(raw)
        return self._cfg

    def discover_cases(self) -> list[dict[str, Any]]:
        return core.build_case_dicts(core.load_registry(), self._load_cfg())

    def prepare_run(self, case_ids: Sequence[str] | None) -> PreparedRun:
        cfg = self._load_cfg()
        preflight = core.run_preflight(cfg)
        self._benchlock_fd = preflight["benchlock_fd"]
        if not preflight["ok"]:
            raise PreflightRefused("preflight 拒跑：" + "；".join(preflight["problems"]))

        self._missing_caps = dict(preflight["missing_caps"])
        requested = {str(case_id).strip() for case_id in (case_ids or []) if str(case_id).strip()}
        discovered = self.discover_cases()
        known = {str(case["id"]) for case in discovered}
        missing = sorted(requested - known)
        if missing:
            raise ValueError(f"未知 case id：{','.join(missing)}")

        head = subprocess.run(
            ["git", "-C", str(core.REPO_ROOT), "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.run_meta = {
            "version": str(preflight["deployed_version"]).strip(),
            "git": head,
            "tiers": "plugin",
            "started_at": dt.datetime.now().strftime("%y%m%d-%H%M%S"),
            "preflight_notes": list(preflight["problems"]),
            "boards": [
                {
                    "com": str(board.get("com", "")),
                    "alias": str(board.get("alias", "")),
                    "serial": str(board.get("serial", "")),
                    "platform": str(board.get("platform", "")),
                }
                for board in cfg.get("boards", [])
            ],
        }
        cases = core.filter_for_run(discovered, requested)
        return PreparedRun(
            cases=cases,
            artifacts={
                "realhw_meta": dict(self.run_meta),
                "missing_caps": dict(self._missing_caps),
            },
        )

    def setup_env(self, case: dict[str, Any], topology: Any) -> bool:
        if self.ctx is None:
            ts = self.run_meta.get("started_at") or dt.datetime.now().strftime("%y%m%d-%H%M%S")
            report_dir = Path.home() / "b-log" / "realhw-reports" / f"tp-{ts}"
            self.ctx = core.build_ctx(self._load_cfg(), report_dir)
        return True

    def execute_step(self, case: dict[str, Any], step: dict[str, Any], topology: Any) -> dict[str, Any]:
        case_id = str(case.get("id", "?"))
        meta = dict(case.get("metadata") or {})
        started = time.monotonic()
        if str(step.get("action") or "run_case") == "longrun_checkpoint":
            return self._execute_checkpoint(case, step, case_id, meta, started)

        skip = core.runtime_skip(meta, self._missing_caps, self._broken_by)
        if skip is not None:
            reason_code, comment = skip
            result = core.make_skip_result(reason_code, comment)
        else:
            result = core.run_case_blackbox(case_id, self.ctx)
        self.run_results.append((case_id, result))
        return {
            "success": True,
            "output": str(getattr(result, "reason", "") or getattr(result, "verdict", "")),
            "captured": {"realhw": core.result_to_dict(result)},
            "timing": time.monotonic() - started,
        }

    def _execute_checkpoint(
        self,
        case: dict[str, Any],
        step: dict[str, Any],
        case_id: str,
        meta: dict[str, Any],
        started: float,
    ) -> dict[str, Any]:
        runner = self._longruns.get(case_id)
        if runner is None:
            skip = core.runtime_skip(meta, self._missing_caps, self._broken_by)
            if skip is not None:
                reason_code, comment = skip
                runner = core.LongrunRunner.skipped(core.make_skip_result(reason_code, comment))
            else:
                runner = core.LongrunRunner(
                    run_fn=lambda: core.run_case_blackbox(case_id, self.ctx),
                    snapshots_path=self.ctx.report_dir / case_id / "snapshots.ndjson",
                    duration_s=int(self.ctx.cfg.get("duration_s") or 0),
                )
                runner.start()
            self._longruns[case_id] = runner

        total = len(case.get("steps") or []) or 1
        index = core.checkpoint_index(str(step.get("id", "")), fallback=total)
        progress = runner.wait_checkpoint(index, total)
        return {
            "success": True,
            "output": (
                f"checkpoint {index}/{total}"
                f"（snapshots={progress['snapshots_seen']}，finished={progress['finished']}）"
            ),
            "captured": {"progress": progress},
            "timing": time.monotonic() - started,
        }

    def evaluate(self, case: dict[str, Any], results: dict[str, Any]) -> bool:
        case_id = str(case.get("id", "?"))
        runner = self._longruns.pop(case_id, None)
        if runner is not None:
            result = runner.result()
            self.run_results.append((case_id, result))
            result_dict = core.result_to_dict(result)
        else:
            result_dict = None
            for step_result in (results.get("steps") or {}).values():
                captured = (step_result or {}).get("captured") or {}
                if "realhw" in captured:
                    result_dict = captured["realhw"]
            if result_dict is None:
                case["_last_failure"] = {
                    "category": "",
                    "reason_code": "adapter_no_result",
                    "comment": "execute_step 未產出 realhw 結果",
                    "evidence": [],
                }
                return False

        payload = core.failure_payload(result_dict)
        if payload is None:
            return True
        case["_last_failure"] = payload
        return False

    def teardown(self, case: dict[str, Any], topology: Any) -> None:
        case_id = str(case.get("id", "?"))
        runner = self._longruns.pop(case_id, None)
        if runner is not None:
            self.run_results.append((case_id, runner.result()))
        if self.ctx is None:
            return
        boards = [str(board["com"]) for board in self.ctx.cfg.get("boards", [])]
        not_ready = core.recover_boards(self.ctx, boards)
        if not_ready and self._broken_by is None:
            self._broken_by = case_id
        core.sweep_tmux(str(self.ctx.cfg.get("tmux_prefix") or "realhw"))

    def verify_install(self) -> list[tuple[bool, str]]:
        checks: list[tuple[bool, str]] = [
            (shutil.which("serialwrap") is not None, "serialwrap CLI 在 PATH"),
            (shutil.which("tmux") is not None, "tmux 可用"),
            ((self._plugin_root() / "agent-config.yaml").is_file(), "agent-config.yaml 存在"),
            ((self._plugin_root() / "testbed.yaml.example").is_file(), "testbed.yaml.example 存在"),
        ]
        try:
            checks.append((bool(self._load_cfg().get("boards")), "testbed 至少一塊板"))
        except Exception as exc:
            checks.append((False, f"testbed.yaml.example 載入失敗：{exc!r}"))
        try:
            count = len(core.load_registry())
            checks.append((count >= 29, f"realhw registry 可載入（{count} cases）"))
        except Exception as exc:
            checks.append((False, f"realhw 載入失敗：{exc!r}"))
        return checks
