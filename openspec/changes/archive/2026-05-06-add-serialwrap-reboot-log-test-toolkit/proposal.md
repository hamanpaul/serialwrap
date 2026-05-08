## Why

Long-running reboot soak tests currently need a repeatable way to drive serialwrap through its own reboot recovery path while preserving per-COM evidence from minicom logs. This change adds a deploy-first toolkit for COM0 normal reboot logs and COM1 reboot logs with boot-time random fault injection.

## What Changes

- Add a `serialwrap-reboot-controller` CLI that controls one selector per process, verifies serialwrap/minicom readiness, registers shared event rules, drives reboot loops, and cleans event/runtime state on exit.
- Add a `serialwrap-event-handler` that consumes serialwrap event payloads and maintains per-COM, per-minicom-log markdown reports with per-event scan cursors.
- Add a `serialwrap-fault-install` provisioning CLI plus target-side fault injector/init assets for COM1 boot-time random fault injection.
- Add documentation for deployment checks, minicom startup, COM0/COM1 controller operation, stop limits, cleanup, and report locations.
- Add local tests for argument parsing, event rule generation, report rewriting, scan cursor behavior, and installer fallback generation.

## Capabilities

### New Capabilities
- `serialwrap-reboot-log-test`: Deploy-first serialwrap reboot soak test toolkit covering controller flow, event handling/reporting, and COM1 target fault injector installation.

### Modified Capabilities

## Impact

- Adds executable toolkit scripts and target-side shell assets for serialwrap-driven reboot testing.
- Requires `/home/paul_chen/.paul_tools/serialwrap` to support the `event` subcommand before runtime use.
- Produces runtime state under `/tmp/serialwrap-reboot-test.COMx.<pid>/` and reports under `~/b-log/event-triggered_COMx_<timestamp>.md`.
