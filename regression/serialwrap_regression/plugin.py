"""testpilot PluginBase 薄殼——邏輯集中 core.py／preflight.py。

契約要點（reliability 實證）：execute_step 恆 success=True、判決集中 evaluate；
remediation enabled:true 僅作 FailureSnapshot 通道；retry.max_attempts=1。
"""
from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from testpilot.api import PluginBase, PreparedRun

from serialwrap_regression import core, preflight


class PreflightRefused(RuntimeError):
    """suite-refuse：preflight 缺項（含 #154 版本 gate）時整場拒跑。"""


class Plugin(PluginBase):
    """serialwrap_regression：已修 bug 的實機回歸 plugin。"""

    api_version = "1.1"

    def __init__(self) -> None:
        self.run_results: list[tuple[str, Any]] = []
        self.run_meta: dict[str, Any] = {}
        self.ctx: Any = None
        self._cfg: dict[str, Any] | None = None
        self._missing_caps: dict[str, str] = {}
        self._benchlock_fd: int | None = None
        self._broken_by: str | None = None

    @property
    def name(self) -> str:
        return "serialwrap_regression"

    @property
    def version(self) -> str:
        return "0.1.0"

    def execution_policy(self, case: dict[str, Any]) -> dict[str, Any]:
        return {"mode": "sequential", "max_concurrency": 1}

    def _plugin_root(self) -> Path:
        return Path(getattr(self, "plugin_root", Path(__file__).resolve().parent))

    def _load_cfg(self) -> dict[str, Any]:
        if self._cfg is None:
            self._cfg = core.load_testbed(self._plugin_root() / "testbed.yaml.example")
        return self._cfg

    def discover_cases(self) -> list[dict[str, Any]]:
        return core.build_case_dicts(core.load_registry(), self._load_cfg())

    def prepare_run(self, case_ids: Sequence[str] | None) -> PreparedRun:
        cfg = self._load_cfg()
        pf = preflight.run_preflight(cfg)
        self._benchlock_fd = pf["benchlock_fd"]
        if not pf["ok"]:
            raise PreflightRefused("preflight 拒跑：" + "；".join(pf["problems"]))

        self._missing_caps = dict(pf["missing_caps"])
        requested = {str(cid).strip() for cid in (case_ids or []) if str(cid).strip()}
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
            "version": str(pf["deployed_version"]).strip(),
            "git": head,
            "tiers": "regression",
            "started_at": dt.datetime.now().strftime("%y%m%d-%H%M%S"),
            "preflight_notes": list(pf["problems"]) + list(pf["notes"]),
            "allow_destructive": bool(cfg.get("allow_destructive")),
            "boards": [
                {
                    "com": str(b.get("com", "")),
                    "alias": str(b.get("alias", "")),
                    "serial": str(b.get("serial", "")),
                    "platform": str(b.get("platform", "")),
                }
                for b in cfg.get("boards", [])
            ],
        }
        cases = core.filter_for_run(discovered, requested)
        return PreparedRun(
            cases=cases,
            artifacts={
                "regression_meta": dict(self.run_meta),
                "missing_caps": dict(self._missing_caps),
            },
        )

    def setup_env(self, case: dict[str, Any], topology: Any) -> bool:
        if self.ctx is None:
            ts = self.run_meta.get("started_at") or dt.datetime.now().strftime("%y%m%d-%H%M%S")
            report_dir = Path.home() / "b-log" / "regression-reports" / f"tp-{ts}"
            self.ctx = core.build_ctx(self._load_cfg(), report_dir)
        return True

    def execute_step(self, case: dict[str, Any], step: dict[str, Any], topology: Any) -> dict[str, Any]:
        case_id = str(case.get("id", "?"))
        meta = dict(case.get("metadata") or {})
        started = time.monotonic()
        skip = core.runtime_skip(
            meta,
            self._missing_caps,
            self._broken_by,
            allow_destructive=bool(self._load_cfg().get("allow_destructive")),
        )
        if skip is not None:
            reason_code, comment = skip
            result = core.make_skip_result(reason_code, comment)
        else:
            result = core.run_case_blackbox(case_id, self.ctx)
        self.run_results.append((case_id, result))
        return {
            "success": True,
            "output": str(getattr(result, "reason", "") or getattr(result, "verdict", "")),
            "captured": {"regression": core.result_to_dict(result)},
            "timing": time.monotonic() - started,
        }

    def evaluate(self, case: dict[str, Any], results: dict[str, Any]) -> bool:
        result_dict = None
        for step_result in (results.get("steps") or {}).values():
            captured = (step_result or {}).get("captured") or {}
            if "regression" in captured:
                result_dict = captured["regression"]
        if result_dict is None:
            case["_last_failure"] = {
                "category": "",
                "reason_code": "adapter_no_result",
                "comment": "execute_step 未產出 regression 結果",
                "evidence": [],
            }
            return False
        payload = core.failure_payload(result_dict)
        if payload is None:
            return True
        # SKIP 也走 payload（category=environment → FailEnv）：testpilot 無 Skip 分類，
        # 沿 reliability 實證慣例；report.md/json 內仍以 SKIP verdict 呈現。
        case["_last_failure"] = payload
        return False

    def teardown(self, case: dict[str, Any], topology: Any) -> None:
        if self.ctx is None:
            return
        boards = [str(b["com"]) for b in self.ctx.cfg.get("boards", [])]
        not_ready = core.recover_boards(self.ctx, boards)
        if not_ready and self._broken_by is None:
            self._broken_by = str(case.get("id", "?"))
        core.sweep_tmux(str(self.ctx.cfg.get("tmux_prefix") or "swreg"))

    def verify_install(self) -> list[tuple[bool, str]]:
        checks: list[tuple[bool, str]] = [
            (shutil.which("tmux") is not None, "tmux 可用"),
            (shutil.which("minicom") is not None, "minicom 可用"),
            ((self._plugin_root() / "agent-config.yaml").is_file(), "agent-config.yaml 存在"),
            ((self._plugin_root() / "testbed.yaml.example").is_file(), "testbed.yaml.example 存在"),
        ]
        try:
            cfg = self._load_cfg()
            exe = Path(str(cfg.get("serialwrap_exe", "")))
            checks.append((exe.is_file(), f"pinned serialwrap 存在（{exe}）"))
            checks.append((bool(cfg.get("boards")), "testbed 至少一塊板"))
        except Exception as exc:
            checks.append((False, f"testbed 載入失敗：{exc!r}"))
        try:
            count = len(core.load_registry())
            checks.append((count >= 10, f"regression registry 可載入（{count} cases）"))
        except Exception as exc:
            checks.append((False, f"registry 載入失敗：{exc!r}"))
        return checks

    def capture_dut_firmware_version(self, config: Any, cases: list[dict[str, Any]]) -> dict[str, Any]:
        deployed = str(self.run_meta.get("version", "")).strip()
        return {"git": deployed} if deployed else {}

    def create_reporter(self) -> Any:
        from serialwrap_regression.reporter import RegressionReporter

        return RegressionReporter(plugin=self)

    def report_formats(self) -> list[str]:
        return ["md", "json"]
