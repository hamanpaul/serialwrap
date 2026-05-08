# Serialwrap Reboot Log Test Design

Date: 2026-05-06

## Goal

Build a deploy-first serialwrap reboot log test toolkit that can run long
soak tests for one COM selector at a time.

The same reboot controller flow is used for COM0 and COM1. The difference
between the normal test and the abnormal test is only that the COM1 target
board has a boot-time fault injector installed.

The toolkit must:

- Generate normal reboot logs for COM0.
- Generate reboot logs with random injected faults for COM1.
- Drive reboots through serialwrap as an agent source so serialwrap enters its
  expected reboot recovery path.
- Maintain per-COM, per-minicom-log event reports.
- Keep controller runtime state limited to the current process run.
- Clean up serialwrap event lifecycle when the controller exits.

## Deployment Assumption

All runtime commands use:

```bash
/home/paul_chen/.paul_tools/serialwrap
```

Before the toolkit can be used, `/home/paul_chen/.paul_tools/serialwrap` must
be updated from `/home/paul_chen/prj_pri/serialwrap` or another equivalent
source so that this command supports the `event` subcommand.

The deployment must verify:

```bash
/home/paul_chen/.paul_tools/serialwrap event --help
/home/paul_chen/.paul_tools/serialwrap daemon status
/home/paul_chen/.paul_tools/serialwrap event status --selector COM0
/home/paul_chen/.paul_tools/serialwrap event status --selector COM1
```

## Components

### `serialwrap-reboot-controller`

Controls exactly one selector per process, for example `COM0` or `COM1`.

Responsibilities:

- Verify serialwrap daemon health.
- Verify the installed serialwrap supports `event`.
- Verify a minicom wrapper log exists and is active for the selected COM.
- Send a dummy echo marker to identify the active minicom log for this run.
- Store current-run state under `/tmp`.
- Register the shared reboot-test event rules and enable the selected COM.
- Drive the reboot loop.
- On exit, disable/reset this COM's event state, delete event rules if no other
  COM still uses them, and remove `/tmp` state.

The controller does not install the COM1 fault injector and does not know
whether the selected COM is the normal or abnormal target.

### `serialwrap-fault-install`

One-time provisioning tool for the COM1 target board.

Responsibilities:

- Push the target fault injector to `/usr/sbin/serialwrap-fault-injector`.
- Push the init script to `/etc/init.d/serialwrap-fault-injector`.
- Prefer serialwrap file transfer.
- Fall back to short command based writes if file transfer fails.
- Avoid heredoc and large UART writes in the fallback path.
- Set executable permissions.
- Create `/etc/rc.d/S50serialwrap-fault-injector`.
- Verify file existence, executable permissions, and symlink target.

If `/etc/init.d` or `/etc/rc.d` does not exist, the installer fails.

### `serialwrap-event-handler`

Handler process invoked by the serialwrap event engine.

Responsibilities:

- Read the serialwrap event JSON payload from stdin.
- Resolve the active minicom log for the payload selector.
- Update `~/b-log/event-triggered_COMx_<timestamp>.md`.
- Maintain per-event scan cursors so repeated events map to the next matching
  log line instead of an old line.
- Exit quickly and idempotently.

### Target Fault Injector

Installed on the COM1 target board only.

Files:

- `/usr/sbin/serialwrap-fault-injector`
- `/etc/init.d/serialwrap-fault-injector`
- `/etc/rc.d/S50serialwrap-fault-injector`

Behavior:

- Runs once per boot at S50 level.
- Has a 10 percent chance to trigger one fault.
- If triggered, chooses one of four events with equal probability.
- Produces no service startup/status/debug output.
- Redirects routine stdout/stderr to `/dev/null`.

Fault events:

1. Thermal notification:

   ```bash
   echo 'bcm_thermal_drv brcm-therm: Trip 0: threshold=105000 mC hysteresis=2000 mC' > /dev/console
   ```

2. 5G ethernet AN rerun:

   ```bash
   ethctl eth0 phy-reset
   ```

3. Process coredump:

   Select a random process from `ps aux` whose PID is greater than 4000, then:

   ```bash
   kill -SIGABRT <PID>
   ```

   If no process qualifies, exit 0 silently.

4. System crash coredump:

   ```bash
   echo c > /proc/sysrq-trigger
   ```

The thermal event writes to `/dev/console` so the serial log looks like a board
generated notification, not a tool-generated message.

## Controller Flow

### Startup

1. Parse arguments.
2. Require `--selector COMx`.
3. Accept optional stop limits:
   - `--hours N`, where N is hours. Example: 4 days is `--hours 96`.
   - `--count N`, where N is completed reboot attempts.
4. Validate `.paul_tools/serialwrap event --help`.
5. Validate serialwrap daemon health.
6. Send a dummy marker command through serialwrap, for example:

   ```bash
   echo __SW_REBOOT_TEST_COM1_<runid>__
   ```

   Source must be `agent:reboot-controller`.

7. Find the active minicom log:
   - Pattern: `~/b-log/mini_COMx_*.log`
   - Must be updated within the active log window, default 10 minutes.
   - Must contain the dummy marker.
8. Derive report path from the minicom log timestamp:

   ```text
   ~/b-log/mini_COM1_260506-152744.log
   ~/b-log/event-triggered_COM1_260506-152744.md
   ```

9. Create current-run state under `/tmp`, for example:

   ```text
   /tmp/serialwrap-reboot-test.COM1.<pid>/
   ```

10. Register the shared reboot-test event rules and enable this selector.
11. Enter the reboot loop.

### Reboot Loop

The controller uses the same loop for COM0 and COM1.

READY check:

1. `session list` must show `state=READY` for the selector.
2. `session self-test --selector COMx --probe-timeout 10` must report
   `classification=OK` and `probe_ok=true`.

If READY:

1. Submit a normal reboot through serialwrap:

   ```bash
   serialwrap cmd submit --selector COMx --source agent:reboot-controller --mode line --cmd reboot
   ```

2. Record the reboot timestamp.
3. Increment the controller's reboot attempt count.

If not READY:

1. If less than 5 minutes have elapsed since the last reboot or fallback action,
   wait and poll again.
2. If 5 minutes have elapsed, run `session recover`.
3. If recover returns to READY, the next loop sends a normal `reboot`.
4. If recover does not return to READY, inspect the tail of the active minicom
   log only:
   - If the tail contains `=>`, send raw broker console input:

     ```text
     reset
     ```

   - If the tail contains `root@prplOS:/#`, send raw broker console input:

     ```text
     reboot -f
     ```

   - If neither prompt is present, wait until the next 5 minute rescue cycle.
5. If `reset` or `reboot -f` is sent, record that fallback timestamp as the new
   5 minute reference point.

Fallback commands must go through serialwrap broker console/raw input and must
not write directly to `/dev/ttyUSB*` or `/dev/ttyACM*`.

### Stop Conditions

Default behavior is an infinite run until stopped by the user.

Optional stop conditions:

- `--hours N`
- `--count N`

The controller must handle SIGINT and SIGTERM. Foreground infinite runs stop
with `Ctrl-C`. Background runs should stop with SIGTERM, for example:

```bash
pkill -TERM -f 'serialwrap-reboot-controller --selector COM1'
```

On normal exit, SIGINT, or SIGTERM, the controller must:

1. Disable this selector's event matcher.
2. Reset this selector's event counters/state.
3. Check whether another COM matcher is still enabled for the reboot-test
   rules.
4. Delete the shared reboot-test rules only if this selector was the last
   active user.
5. Remove the `/tmp/serialwrap-reboot-test.COMx.<pid>/` state directory.

## Event Rules

Rules are registered by the controller at test start and cleaned up by the
controller at test exit.

The rule IDs are shared across COM0 and COM1. Each rule has selectors
`["COM0", "COM1"]`. A controller only enables or disables its own COM matcher.
This avoids rule overwrite when two controllers run at the same time.

The same event set is used for COM0 and COM1:

| Rule name | Match |
| --- | --- |
| `brcm-therm` | `brcm-therm` |
| `link-down` | `Link is Down` |
| `pstate` | `pstate` |
| `kernel-panic` | `Kernel panic` |
| `smc-bootloader` | `SMC bootloader` |

`pstate` is case-sensitive and matches lowercase `pstate` only.

`SMC bootloader` is an event statistic only. The controller's fallback prompt
check for U-Boot still uses `=>`, but `=>` is not counted as `SMC bootloader`.

Each rule invokes `serialwrap-event-handler`.

## Event Report

Reports are stored next to minicom logs:

```text
~/b-log/event-triggered_COMx_<timestamp>.md
```

There is one report per COM and per minicom log. COM0 and COM1 do not share a
report.

The handler resolves the active minicom log in this order:

1. Current-run `/tmp` state for the selector.
2. Fallback to latest `~/b-log/mini_COMx_*.log` updated within 10 minutes.

Line numbers are actual physical file line numbers in the minicom log.

For repeated matches, the handler maintains per-event scan cursors. For each
event, it scans from the previous matched line plus one and records the first
new matching line.

Report format:

```markdown
# Event Triggered Report

Log: mini_COM1_260506-152744.log
Generated: 2026-05-06T16:10:23+08:00

## Summary

Denominator: SMC bootloader = 12

| Event | Count | Probability vs SMC bootloader |
| --- | ---: | ---: |
| brcm-therm | 3 | 25.00% |
| Link is Down | 1 | 8.33% |
| pstate | 4 | 33.33% |
| Kernel panic | 1 | 8.33% |
| SMC bootloader | 12 | 100.00% |

## Events

| Log name | Log line number | Event trigger time | Event |
| --- | ---: | --- | --- |
| mini_COM1_260506-152744.log | 1832 | 2026-05-06T16:10:23+08:00 | brcm-therm |
```

If the `SMC bootloader` count is zero, probability values for other events are
shown as `N/A`.

## README Requirements

The toolkit README must document:

- Required serialwrap deployment check.
- How to start `minicom COM0 -O timestamp=extended`.
- How to start `minicom COM1 -O timestamp=extended`.
- How to install the COM1 fault injector.
- How to run COM0 and COM1 controllers separately.
- Infinite run behavior.
- `--hours N` and `--count N`.
- How to stop a foreground controller with `Ctrl-C`.
- How to stop a background controller with SIGTERM.
- What cleanup happens on controller exit.
- Where reports are written.

## Verification Plan

### Local Checks

- Shell syntax checks for scripts.
- Argument parsing checks.
- Event rule JSON generation check.
- Report rewrite check using sample minicom log snippets.
- Scan cursor check for repeated event lines.

### Serialwrap Integration Checks

- Verify `.paul_tools/serialwrap event --help`.
- Verify `event status`.
- Verify rule add/list/show/enable/disable/reset/rm.
- Verify daemon status.

### Target Installer Checks

- Verify `/etc/init.d` and `/etc/rc.d` exist.
- Verify `file.push` path.
- Verify fallback short write path.
- Verify target file permissions.
- Verify `/etc/rc.d/S50serialwrap-fault-injector` symlink.

### Controller Smoke

Run a short test with count limit:

```bash
serialwrap-reboot-controller --selector COM0 --count 1
serialwrap-reboot-controller --selector COM1 --count 1
```

Confirm:

- Active minicom log marker detection.
- Report path derivation.
- Event rules are registered and enabled at start.
- Event rules are disabled/reset and possibly removed at stop.
- `/tmp` state is removed at stop.
- Reboot command source is `agent:reboot-controller`.

The smoke test must not directly force `echo c > /proc/sysrq-trigger`.
System crash injection is left to the COM1 random boot-time path during long
soak testing.

## Open Constraints

- This design assumes the serialwrap source version with `event` support can be
  deployed to `/home/paul_chen/.paul_tools/serialwrap`.
- This design assumes minicom wrapper logs follow `~/b-log/mini_COMx_*.log`.
- This design assumes the target is OpenWrt-like and supports `/etc/init.d` and
  `/etc/rc.d`.
