"""P9-T13 回滾演練使用的隔離 CLI 替身。"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Sequence

from tools.stage3.proc import CmdResult

import stage3_fakes


class RecordingRunner:
    """記錄 CLI 呼叫並模擬 Current／Previous 切換。"""

    def __init__(
        self,
        root: Path,
        *,
        boards: Sequence[str],
        current: str,
        previous: str,
        d1_generation: int,
        reacquire: bool,
        pairing_ok: bool,
        flashing: bool,
        smoke_ok: bool,
        corrupt_refused: bool,
        start_fail_reverts: bool,
        forward_sideload: bool,
    ) -> None:
        self.root = Path(root)
        self.boards = tuple(boards)
        self.current_version = current
        self.previous_version = previous
        self.d1_generation = d1_generation
        self.reacquire = reacquire
        self.pairing_ok = True
        self.rollback_pairing_ok = pairing_ok
        self.flashing = flashing
        self.smoke_ok = smoke_ok
        self.corrupt_refused = corrupt_refused
        self.start_fail_reverts = start_fail_reverts
        self.forward_sideload = forward_sideload
        self.calls: list[dict[str, Any]] = []
        self.bundle_dirs: dict[str, Path] = {}
        self.active_dir: Path | None = None
        self.layout_dir_override: Path | None = None
        self.pid: int | None = 100
        self.same_pid_after_rollback = False
        self.generation = d1_generation
        self.last_ready_at = "2026-11-02T00:00:00+00:00"
        self.rollback_done = False
        self.forward_done = False
        self.start_fail_attempted = False
        self.corrupt_attempted = False
        self.released_sessions: list[dict[str, Any]] = []
        self.extra_sessions: list[dict[str, Any]] = []
        self.not_ready_boards: set[str] = set()
        self.changed_bindings: set[str] = set()
        self.changed_bindings_after_rollback: set[str] = set()
        self.changed_aliases_after_rollback: set[str] = set()
        self.drop_sessions_after_rollback: set[str] = set()
        self.drop_boards_after_rollback: set[str] = set()
        self.drop_released_session = False
        self.echo_only = False
        self.doctor_checks: list[dict[str, Any]] | None = None
        self.command_markers: dict[str, str] = {}
        self.command_count = 0
        self.smoke_fail_attempts: set[int] = set()
        self.session_list_count = 0
        self.smoke_gate_start_after = 3
        self.smoke_gate_pending_remaining = 0
        self.foreign_holders: Any = {}
        self.multi_open = False
        self.cli_version_override: str | None = None
        self.status_version_override: str | None = None
        self.raise_setup_for: set[str] = set()
        self.corrupt_accepted_unchanged = False
        self.corrupt_mutates_current_tree = False
        self.corrupt_refused_but_mutates_state = False
        self.start_fail_reacquire = True

    def bind(self, current_dir: Path, previous_dir: Path) -> None:
        self.bundle_dirs = {
            self.current_version: Path(current_dir),
            self.previous_version: Path(previous_dir),
        }
        self.active_dir = self.bundle_dirs[self.current_version]

    def _switch(self, version: str, *, pid: int, ready_at: str | None = None) -> None:
        self.active_dir = self.bundle_dirs.get(version, self.active_dir)
        self.pid = pid
        self.generation = 1
        if ready_at is not None and self.reacquire:
            self.last_ready_at = ready_at
        self._write_layout()

    def _write_layout(self) -> None:
        active_dir = self.layout_dir_override or self.active_dir
        if active_dir is None:
            return
        if os.name == "nt":
            layout = self.root / "bin" / "current-release.json"
            layout.write_text(
                json.dumps(
                    {"bundle_version": active_dir.name, "dir": str(active_dir)},
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            for name in ("serialwrap.exe", "serialwrapd.exe"):
                source = active_dir / "bin" / name
                target = self.root / "bin" / name
                if source.is_file():
                    shutil.copyfile(source, target)
        else:
            link = self.root / "bin" / "serialwrap"
            source = active_dir / "bin" / "serialwrap"
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(source)

    @staticmethod
    def _sub(argv: Sequence[str]) -> str:
        if len(argv) < 2:
            return ""
        if argv[1] in {"setup"}:
            return "setup"
        if len(argv) >= 3 and argv[1] in {"daemon", "mcu", "session", "cmd", "device"}:
            return f"{argv[1]} {argv[2]}"
        return argv[1]

    def _current_version(self) -> str:
        if self.active_dir is None:
            return self.current_version
        try:
            manifest = json.loads((self.active_dir / "release-manifest.json").read_text(encoding="utf-8"))
            return str(manifest["bundle_version"])
        except (OSError, ValueError, KeyError, TypeError):
            return self.active_dir.name

    def _sessions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, board in enumerate(self.boards):
            if self.rollback_done and board in self.drop_boards_after_rollback:
                continue
            if self.drop_released_session and board in self.not_ready_boards:
                continue
            state = "ATTACHED" if board in self.not_ready_boards else "READY"
            binding = f"/dev/serial/by-id/{board}-fixture"
            if board in self.changed_bindings or (self.rollback_done and board in self.changed_bindings_after_rollback):
                binding += "-changed"
            alias = f"board-{index}"
            if self.rollback_done and board in self.changed_aliases_after_rollback:
                alias += "-changed"
            gate_pending = (
                self.session_list_count > self.smoke_gate_start_after
                and self.smoke_gate_pending_remaining > 0
            )
            if gate_pending:
                self.smoke_gate_pending_remaining -= 1
            rows.append(
                {
                    "alias": alias,
                    "attached_real_path": f"/dev/tty-fixture-{index}",
                    "bridge_generation": self.generation,
                    "boot_quiet_remaining_s": 1.0 if gate_pending else None,
                    "com": board,
                    "device_by_id": binding,
                    "last_ready_at": self.last_ready_at,
                    "ready_reconfirm_failed": False,
                    "ready_reconfirm_pending": gate_pending,
                    "state": state,
                }
            )
        rows.extend(
            dict(row)
            for row in self.extra_sessions
            if not (self.rollback_done and row.get("com") in self.drop_sessions_after_rollback)
        )
        if not (self.drop_released_session and self.rollback_done):
            rows.extend(dict(row) for row in self.released_sessions)
        return rows

    def __call__(self, argv, *, env=None, timeout=None, cwd=None) -> CmdResult:
        args = [str(value) for value in argv]
        sub = self._sub(args)
        self.calls.append({"argv": args, "cwd": str(cwd) if cwd else None, "kind": "cli", "sub": sub, "timeout": timeout})
        out: dict[str, Any]
        rc = 0
        if args[1:] == ["--version"]:
            stdout = f"serialwrap {self.cli_version_override or self._current_version()}\n"
            return CmdResult(rc=0, stdout=stdout, stderr="", elapsed_s=0.0, timed_out=False)
        if args[1:] == ["daemon", "status"]:
            out = {
                "foreign_holders": self.foreign_holders,
                "multi_open": self.multi_open,
                "ok": True,
                "pid": self.pid,
                "running": self.pid is not None,
                "version": self.status_version_override or self._current_version(),
            }
        elif args[1:] == ["mcu", "status"]:
            out = {"flashing": self.flashing, "ok": True}
        elif args[1:] == ["session", "list"]:
            self.session_list_count += 1
            out = {"ok": True, "sessions": self._sessions()}
        elif args[1:] == ["doctor"]:
            checks = self.doctor_checks or [
                {"check": "python", "ok": True},
                {"check": "version_pairing", "ok": self.pairing_ok},
            ]
            out = {
                "checks": checks,
                "ok": all(row.get("ok") is True for row in checks),
            }
        elif args[1:3] == ["cmd", "submit"]:
            self.command_count += 1
            command_id = f"{self.command_count:032x}"
            try:
                command = args[args.index("--cmd") + 1]
            except (ValueError, IndexError):
                command = ""
            match = re.search(r"SW''DRILL_([A-Za-z0-9]+)", command)
            self.command_markers[command_id] = match.group(1) if match else "abc123"
            out = {"cmd_id": command_id, "ok": True}
        elif args[1:3] == ["cmd", "status"]:
            try:
                command_id = args[args.index("--cmd-id") + 1]
            except (ValueError, IndexError):
                command_id = ""
            marker = self.command_markers.get(command_id, "abc123")
            if self.echo_only:
                out = {"command": {"status": "done", "stdout": f"echo SW''DRILL_{marker}\n"}, "ok": True}
                stdout = json.dumps(out, separators=(",", ":"))
            else:
                failed = self.command_count in self.smoke_fail_attempts
                output = f"SWDRILL_{marker}\n" if self.smoke_ok and not failed else "smoke output missing\n"
                command_status = "error" if failed else "done"
                out = {"command": {"status": command_status, "stdout": output}, "ok": True}
                stdout = json.dumps(out, separators=(",", ":"))
            return CmdResult(rc=0, stdout=stdout, stderr="", elapsed_s=0.0, timed_out=False)
        elif args[1:3] == ["setup", "--rollback"]:
            self.rollback_done = True
            self._switch(self.previous_version, pid=100 if self.same_pid_after_rollback else 101,
                         ready_at="2026-11-02T00:00:01+00:00")
            self.pairing_ok = self.rollback_pairing_ok
            out = {"ok": True}
        elif args[1] == "setup":
            bundle_cli = Path(args[0])
            bundle_dir = bundle_cli.parent.parent
            if bundle_dir.name in self.raise_setup_for:
                raise FileNotFoundError(f"fixture cannot execute {bundle_cli}")
            if bundle_cli.name.startswith("serialwrap") and bundle_dir.name == self.current_version and self.rollback_done:
                self.forward_done = True
                self._switch(self.current_version, pid=102, ready_at="2026-11-02T00:00:02+00:00")
                if self.forward_sideload:
                    (bundle_dir / "bin" / "version.dll").write_bytes(b"side-loaded runtime\n")
                out = {"ok": True}
            elif bundle_dir.name == "corrupt":
                self.corrupt_attempted = True
                if self.corrupt_refused:
                    rc = 2
                    out = {"ok": False, "reason": "manifest rejected"}
                    if self.corrupt_refused_but_mutates_state:
                        self.pid = 103
                        self.generation += 1
                elif self.corrupt_accepted_unchanged:
                    out = {"ok": True}
                    if self.corrupt_mutates_current_tree:
                        (self.bundle_dirs[self.current_version] / "bin" / "version.dll").write_bytes(
                            b"partial activation\n"
                        )
                else:
                    out = {"ok": True}
                    self.active_dir = bundle_dir
                    self.pid = 102
                    self.generation += 1
            elif bundle_dir.name == "startfail":
                self.start_fail_attempted = True
                rc = 2
                out = {"ok": False, "reason": "daemon failed to start"}
                if self.start_fail_reverts:
                    self._switch(self.current_version, pid=103,
                                 ready_at="2026-11-02T00:00:03+00:00" if self.start_fail_reacquire else None)
                    if not self.start_fail_reacquire:
                        self.last_ready_at = "2026-11-01T23:59:59+00:00"
                else:
                    self.active_dir = bundle_dir
                    self.pid = None
                    self._write_layout()
            else:
                out = {"ok": True}
        else:
            rc = 2
            out = {"ok": False, "error": "unknown fake command"}
        return CmdResult(rc=rc, stdout=json.dumps(out, separators=(",", ":")), stderr="", elapsed_s=0.0, timed_out=False)


def drill_runner(
    root: Path,
    *,
    boards: Sequence[str],
    current: str,
    previous: str,
    d1_generation: int = 3,
    reacquire: bool = True,
    pairing_ok: bool = True,
    flashing: bool = False,
    smoke_ok: bool = True,
    corrupt_refused: bool = True,
    start_fail_reverts: bool = True,
    forward_sideload: bool = False,
) -> RecordingRunner:
    """建立只記錄呼叫、不啟動 daemon 的 CLI 替身。"""
    return RecordingRunner(
        root,
        boards=boards,
        current=current,
        previous=previous,
        d1_generation=d1_generation,
        reacquire=reacquire,
        pairing_ok=pairing_ok,
        flashing=flashing,
        smoke_ok=smoke_ok,
        corrupt_refused=corrupt_refused,
        start_fail_reverts=start_fail_reverts,
        forward_sideload=forward_sideload,
    )


def drill_config(root: Path, runner: RecordingRunner, *, corrupt: bool = False, start_fail: bool = False, **over: Any):
    """建立具有真實 manifest／bundle 檔案樹的演練設定。"""
    root = Path(root)
    current = str(over.pop("expect_current", runner.current_version))
    previous = str(over.pop("expect_previous", runner.previous_version))
    boards = tuple(over.pop("boards", runner.boards))
    platform = str(over.pop("platform", "windows" if os.name == "nt" else "linux"))
    target = "x86_64-pc-windows-msvc" if platform == "windows" else "x86_64-unknown-linux-gnu"
    versions = root / "versions"
    current_dir = stage3_fakes.make_bundle(versions / current, version=current, target=target)
    previous_dir = stage3_fakes.make_bundle(versions / previous, version=previous, target=target)
    runner.bind(current_dir, previous_dir)

    bindir = root / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    if platform == "windows":
        cli = bindir / "serialwrap.exe"
        daemon = bindir / "serialwrapd.exe"
        shutil.copyfile(current_dir / "bin" / "serialwrap.exe", cli)
        shutil.copyfile(current_dir / "bin" / "serialwrapd.exe", daemon)
        (bindir / "current-release.json").write_text(
            json.dumps({"bundle_version": current, "dir": str(current_dir)}, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
    else:
        cli = bindir / "serialwrap"
        cli.symlink_to(current_dir / "bin" / "serialwrap")

    runner._write_layout()
    current_manifest = json.loads((current_dir / "release-manifest.json").read_text(encoding="utf-8"))
    current_cli = current_dir / current_manifest["components"]["cli"]["path"]
    forward_argv = tuple(over.pop("forward_argv", (str(current_cli), "setup")))
    cfg_values = {
        "cli": cli,
        "platform": platform,
        "boards": boards,
        "expect_current": current,
        "expect_previous": previous,
        "forward_argv": forward_argv,
        "corrupt_from": current_dir if corrupt else None,
        "start_fail_from": current_dir if start_fail else None,
        "ready_timeout_s": 6.0,
        "setup_timeout_s": 30.0,
        "out_dir": root / "out",
    }
    cfg_values.update(over)
    runner.root = root
    from tools.stage3.rollback_drill import DrillConfig

    return DrillConfig(**cfg_values)
