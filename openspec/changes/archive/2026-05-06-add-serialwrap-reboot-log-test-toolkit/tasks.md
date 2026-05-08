## 1. Project scaffolding and test harness

- [x] 1.1 Create toolkit package/script structure with executable entrypoints for `serialwrap-reboot-controller`, `serialwrap-event-handler`, and `serialwrap-fault-install`
- [x] 1.2 Add an automated test harness and baseline tests for CLI argument parsing and command path constants before production logic

## 2. Controller behavior

- [x] 2.1 Add failing tests then implement controller argument parsing for required `--selector`, optional `--hours`, optional `--count`, and infinite default behavior
- [x] 2.2 Add failing tests then implement serialwrap readiness checks, daemon checks, marker submission, active minicom log detection, report path derivation, and `/tmp` run-state creation
- [x] 2.3 Add failing tests then implement shared event rule JSON/command generation, selected-COM enablement, selected-COM disable/reset cleanup, and last-active shared rule removal logic
- [x] 2.4 Add failing tests then implement reboot loop decisions for READY/self-test normal reboot submission, five-minute recovery throttling, and broker raw fallback commands from active log tail prompts
- [x] 2.5 Add failing tests then implement SIGINT/SIGTERM/normal-exit cleanup sequencing for event lifecycle and `/tmp` state removal

## 3. Event reporting

- [x] 3.1 Add failing tests then implement event payload parsing, active log resolution from current-run state with recent-log fallback, and fast idempotent handler exit
- [x] 3.2 Add failing tests then implement report rewriting with summary counts, SMC bootloader denominator probabilities, `N/A` zero-denominator handling, event rows, and physical log line numbers
- [x] 3.3 Add failing tests then implement per-event scan cursors so repeated payloads advance to the next matching log line

## 4. COM1 fault injector installer and target assets

- [x] 4.1 Add failing tests then implement target fault injector script content for 10 percent boot-time random selection across the four specified fault events with silent normal output
- [x] 4.2 Add failing tests then implement init script content and `serialwrap-fault-install` directory checks, preferred file-transfer path, short-write fallback path, executable permissions, symlink creation, and verification commands

## 5. Documentation and validation

- [x] 5.1 Add README documentation for deployment checks, minicom startup, COM1 injector installation, controller runs, stop limits, foreground/background stop behavior, cleanup, and reports
- [x] 5.2 Run the local test suite and shell syntax checks for all scripts
- [x] 5.3 Run OpenSpec status/validation for `add-serialwrap-reboot-log-test-toolkit`
