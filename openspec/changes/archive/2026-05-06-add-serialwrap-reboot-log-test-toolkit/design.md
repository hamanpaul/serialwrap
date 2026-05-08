## Context

The toolkit is a small deploy-first set of shell/Python utilities for long-running serialwrap reboot soak tests. Runtime serialwrap access is fixed to `/home/paul_chen/.paul_tools/serialwrap`, and that installed command must already support the `event` subcommand. Minicom wrapper logs are expected at `~/b-log/mini_COMx_*.log` and are the evidence source for per-event reporting.

The same controller flow must work for COM0 and COM1. COM1 abnormal behavior is provided by a target-side boot-time fault injector installed separately; the controller does not branch on normal versus abnormal targets.

## Goals / Non-Goals

**Goals:**
- Provide one-process-per-selector reboot control with explicit serialwrap daemon/event readiness checks.
- Drive normal reboot and fallback recovery commands through serialwrap so serialwrap observes the expected recovery lifecycle.
- Register shared reboot-test event rules for COM0/COM1 while enabling only the current selector for each controller process.
- Maintain current-run state only under `/tmp` and remove it on controller exit.
- Generate one event report per selector and minicom log with accurate physical log line numbers and per-event scan cursors.
- Provide a COM1 provisioning tool for installing target-side random boot-time fault injection.
- Cover local behavior with automated tests before production implementation.

**Non-Goals:**
- Do not update or build serialwrap itself; deployment only verifies the installed command supports required subcommands.
- Do not directly write to `/dev/ttyUSB*` or `/dev/ttyACM*`.
- Do not install the COM1 fault injector from the controller.
- Do not persist controller runtime state across process runs except the markdown report next to minicom logs.
- Do not force system crash injection during local smoke tests.

## Decisions

1. **Use shell entrypoints backed by a testable Python library.**
   - Rationale: The toolkit is operator-facing, but controller/reporting behavior needs deterministic unit tests.
   - Alternative considered: Pure shell scripts. Rejected because report rewriting, scan cursors, JSON payload parsing, and argument validation are harder to test safely.

2. **Represent serialwrap command execution behind a small runner boundary.**
   - Rationale: Tests can verify exact command construction without needing a live daemon, while production still shells out to `/home/paul_chen/.paul_tools/serialwrap`.
   - Alternative considered: Inline subprocess calls in every flow. Rejected to keep command generation and error handling reviewable.

3. **Use `/tmp/serialwrap-reboot-test.<selector>.<pid>/` for current-run state.**
   - Rationale: State is naturally scoped to one controller process and can be cleaned on SIGINT/SIGTERM/normal exit.
   - Alternative considered: State in `~/b-log`. Rejected because runtime coordination state should not outlive the process by default.

4. **Use shared rule IDs with selector-specific enable/disable state.**
   - Rationale: COM0 and COM1 controllers can run concurrently without overwriting each other's rules, and cleanup can delete shared rules only when no selector still uses them.
   - Alternative considered: Per-selector rule IDs. Rejected because it duplicates rule definitions and makes event statistics harder to compare.

5. **Resolve reports from the active minicom log timestamp.**
   - Rationale: A report belongs to the exact log file containing the controller marker; fallback latest-log resolution is only for handler invocations that cannot read current-run state.
   - Alternative considered: One rolling report per COM. Rejected because it mixes evidence across minicom sessions.

6. **Installer prefers serialwrap file transfer and falls back to short command writes.**
   - Rationale: File transfer is safer for full scripts, while the fallback avoids heredoc and large UART writes that are fragile over serial.
   - Alternative considered: Heredoc over UART. Rejected by design constraint.

## Risks / Trade-offs

- Serialwrap event CLI syntax may differ from assumptions → Centralize command construction and test generated argument vectors/JSON before live integration.
- Active minicom marker might be delayed in the log → Use an active log window and explicit marker search before entering the reboot loop.
- Concurrent controllers can race during shared rule cleanup → Check current event status for both selectors before removing shared rules and tolerate already-removed state as an explicit serialwrap result only if serialwrap reports it.
- Fallback recovery can send commands to the wrong prompt if stale logs are inspected → Inspect only the active minicom log tail associated with this run.
- Target-side `ps`, `ethctl`, or init layout can vary → Installer verifies required directories/files/symlink and the fault injector exits silently when a selected process target is unavailable.
