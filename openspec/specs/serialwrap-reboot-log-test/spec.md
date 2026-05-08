## Purpose
Provide a serialwrap-based reboot log test toolkit that can run per-COM reboot
soak controllers, correlate serialwrap event payloads back to minicom logs, and
install the COM1 boot-time fault injector used by abnormal reboot testing.

## Requirements

### Requirement: Serialwrap deployment readiness
The toolkit SHALL verify that `/home/paul_chen/.paul_tools/serialwrap` supports the `event` subcommand and that the serialwrap daemon is healthy before a controller starts a reboot loop.

#### Scenario: Controller validates serialwrap event support
- **WHEN** `serialwrap-reboot-controller --selector COM0` starts
- **THEN** it SHALL run `/home/paul_chen/.paul_tools/serialwrap event --help` before registering event rules

#### Scenario: Controller validates daemon health
- **WHEN** `serialwrap-reboot-controller --selector COM1` starts
- **THEN** it SHALL run `/home/paul_chen/.paul_tools/serialwrap daemon status` before sending marker or reboot commands

### Requirement: One selector per controller process
The controller SHALL require exactly one `--selector COMx` value per process and SHALL apply the same reboot control flow to COM0 and COM1.

#### Scenario: Selector is required
- **WHEN** the controller is invoked without `--selector`
- **THEN** it SHALL exit with an argument parsing error

#### Scenario: COM1 does not alter controller flow
- **WHEN** the controller is invoked with `--selector COM1`
- **THEN** it SHALL use the same readiness, reboot, fallback, event registration, and cleanup flow as COM0

### Requirement: Active minicom log detection
The controller SHALL identify the active minicom log for the selected COM by sending a dummy marker through serialwrap with source `agent:reboot-controller` and finding a recently updated `~/b-log/mini_COMx_*.log` that contains that marker.

#### Scenario: Marker identifies active log
- **WHEN** the controller starts for COM1
- **THEN** it SHALL submit an echo marker through serialwrap with source `agent:reboot-controller` and derive `~/b-log/event-triggered_COM1_<timestamp>.md` from the matching minicom log name

#### Scenario: No active marker log exists
- **WHEN** no `~/b-log/mini_COMx_*.log` updated within the active log window contains the marker
- **THEN** the controller SHALL fail before entering the reboot loop

### Requirement: Current-run state lifecycle
The controller SHALL store current-run state under `/tmp/serialwrap-reboot-test.<selector>.<pid>/` and SHALL remove that state on normal exit, SIGINT, or SIGTERM.

#### Scenario: State is created for a run
- **WHEN** the controller has resolved the active minicom log
- **THEN** it SHALL create a `/tmp` run directory containing enough state for the event handler to resolve the selector's active log and report path

#### Scenario: State is removed on termination
- **WHEN** the controller receives SIGINT or SIGTERM
- **THEN** it SHALL clean up the selector event state and remove its `/tmp` run directory before exiting

### Requirement: Shared event rule registration
The controller SHALL register shared reboot-test rules for selectors `COM0` and `COM1`, enable only its selected COM matcher at start, disable and reset only its selected COM matcher at exit, and delete shared rules only when no COM matcher still uses them.

#### Scenario: Rules contain required events
- **WHEN** the controller registers reboot-test event rules
- **THEN** it SHALL include matches for `brcm-therm`, `Link is Down`, lowercase case-sensitive `pstate`, `Kernel panic`, and `SMC bootloader`

#### Scenario: Concurrent controller cleanup preserves shared rules
- **WHEN** a COM0 controller exits while a COM1 matcher is still enabled
- **THEN** the COM0 controller SHALL disable/reset COM0 state and SHALL NOT delete the shared reboot-test rules

### Requirement: Reboot loop readiness and normal reboot
The controller SHALL poll serialwrap session readiness and submit a normal `reboot` command through serialwrap with source `agent:reboot-controller` when the selected session is ready and self-test reports `classification=OK` and `probe_ok=true`.

#### Scenario: Ready session reboots normally
- **WHEN** `session list` shows `state=READY` for COM0 and `session self-test --selector COM0 --probe-timeout 10` reports OK
- **THEN** the controller SHALL submit `reboot` through serialwrap command submission with source `agent:reboot-controller` and increment the reboot attempt count

### Requirement: Reboot loop fallback recovery
The controller SHALL attempt recovery no more often than every five minutes after the last reboot or fallback action, and fallback commands SHALL go through serialwrap broker console/raw input instead of direct TTY writes.

#### Scenario: U-Boot prompt fallback
- **WHEN** recovery does not return to READY and the active minicom log tail contains `=>`
- **THEN** the controller SHALL send raw broker console input `reset` through serialwrap and record that fallback timestamp

#### Scenario: Linux prompt fallback
- **WHEN** recovery does not return to READY and the active minicom log tail contains `root@prplOS:/#`
- **THEN** the controller SHALL send raw broker console input `reboot -f` through serialwrap and record that fallback timestamp

#### Scenario: No fallback prompt
- **WHEN** recovery does not return to READY and the active minicom log tail contains neither fallback prompt
- **THEN** the controller SHALL wait until the next five-minute rescue cycle without sending direct TTY input

### Requirement: Stop conditions
The controller SHALL run indefinitely by default and SHALL support optional `--hours N` and `--count N` stop limits.

#### Scenario: Count limit stops loop
- **WHEN** the controller is invoked with `--count 1`
- **THEN** it SHALL exit after one completed reboot attempt and perform normal cleanup

#### Scenario: Infinite default
- **WHEN** the controller is invoked without `--hours` or `--count`
- **THEN** it SHALL continue running until stopped by SIGINT or SIGTERM

### Requirement: Event handler report generation
The event handler SHALL read a serialwrap event JSON payload from stdin, resolve the active minicom log for the payload selector, update `~/b-log/event-triggered_COMx_<timestamp>.md`, and exit quickly and idempotently.

#### Scenario: Handler uses current-run state first
- **WHEN** current-run state exists for the payload selector
- **THEN** the handler SHALL use the active log and report path recorded in that state

#### Scenario: Handler falls back to recent minicom log
- **WHEN** current-run state is absent for the payload selector
- **THEN** the handler SHALL use the latest `~/b-log/mini_COMx_*.log` updated within ten minutes

### Requirement: Event report contents
The event report SHALL include a summary table with counts and probability versus `SMC bootloader`, plus an events table containing log name, physical log line number, event trigger time, and event name.

#### Scenario: Probability denominator exists
- **WHEN** the `SMC bootloader` count is greater than zero
- **THEN** non-denominator event probabilities SHALL be formatted as percentages against the `SMC bootloader` count

#### Scenario: Probability denominator missing
- **WHEN** the `SMC bootloader` count is zero
- **THEN** probabilities for other events SHALL be shown as `N/A`

### Requirement: Event scan cursors
The event handler SHALL maintain per-event scan cursors so repeated events map to the next matching physical log line instead of an earlier match.

#### Scenario: Repeated event advances cursor
- **WHEN** two `brcm-therm` payloads are handled for a log containing two matching lines
- **THEN** the report SHALL record the first payload at the first line and the second payload at the second line

### Requirement: COM1 fault injector installation
The `serialwrap-fault-install` tool SHALL install the target fault injector to `/usr/sbin/serialwrap-fault-injector`, install the init script to `/etc/init.d/serialwrap-fault-injector`, create `/etc/rc.d/S50serialwrap-fault-injector`, set executable permissions, and verify the installed files and symlink.

#### Scenario: Required target directories missing
- **WHEN** `/etc/init.d` or `/etc/rc.d` does not exist on the target
- **THEN** the installer SHALL fail before attempting installation

#### Scenario: File transfer fallback
- **WHEN** serialwrap file transfer fails
- **THEN** the installer SHALL fall back to short command based writes without heredoc or large UART writes

### Requirement: Target fault injector behavior
The target fault injector SHALL run once per boot at S50 level, silently choose a fault with 10 percent probability, and choose equally among thermal notification, 5G ethernet AN rerun, process coredump, and system crash coredump when triggered.

#### Scenario: Thermal notification uses console
- **WHEN** the thermal fault is selected
- **THEN** the injector SHALL write the thermal notification text to `/dev/console`

#### Scenario: No eligible process for coredump
- **WHEN** the process coredump fault is selected and no `ps aux` process has PID greater than 4000
- **THEN** the injector SHALL exit 0 silently

### Requirement: Toolkit documentation
The toolkit README SHALL document serialwrap deployment checks, minicom startup for COM0 and COM1, COM1 fault injector installation, separate controller execution, infinite/default stop behavior, `--hours`, `--count`, foreground/background stopping, cleanup behavior, and report locations.

#### Scenario: Operator can follow README
- **WHEN** an operator reads the README
- **THEN** it SHALL include the commands and explanations needed to deploy serialwrap, start minicom logs, install COM1 faults, run COM0/COM1 controllers, stop them, and find reports
