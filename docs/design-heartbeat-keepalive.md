# Design: Long-running Command Heartbeat / Keepalive

**Issue**: #24  
**Status**: Design draft  

## Problem

Long-running commands (e.g., `apt upgrade`, `python -m unittest`) produce no
prompt output during execution. The broker's prompt probe times out, causing
the session to transition from READY → ATTACHED + PROMPT_TIMEOUT even though
the command is still running successfully.

## Proposed Solution

### 1. Foreground Command Awareness

When a command is submitted via `command.submit`, the broker should track that
a foreground command is in-flight and **suspend prompt timeout** during execution.

```python
# In session_manager.py execute_command:
session.foreground_busy = True   # Already exists
session.fg_cmd_started_at = now_iso()
session.fg_cmd_timeout_s = timeout_s
```

The prompt health probe should skip sessions where `foreground_busy == True`
and `elapsed < fg_cmd_timeout_s`.

### 2. Expected Duration Hint

Add optional `expected_duration_s` parameter to `command.submit`:

```json
{"tool": "serialwrap_submit_command", "params": {
  "selector": "COM0",
  "cmd": "python3 -m unittest discover",
  "timeout_s": 120,
  "expected_duration_s": 60
}}
```

When `expected_duration_s` is provided:
- Prompt timeout is suspended for that duration
- After expiry, normal prompt probing resumes

### 3. Output-based Keepalive Detection

Monitor UART rx during command execution. If any bytes are received (even
non-prompt output like test progress dots), reset the silence timer.

```python
# In _wait_for_prompt:
while elapsed < timeout_s:
    if bridge.rx_snapshot_len() > last_rx_len:
        last_rx_len = bridge.rx_snapshot_len()
        silence_start = time.monotonic()  # Reset silence timer
    if time.monotonic() - silence_start > silence_timeout:
        break  # True silence
```

### 4. Distinguishing Silence Types

| Condition | Meaning | Action |
|-----------|---------|--------|
| foreground_busy + rx flowing | Command running, producing output | Wait |
| foreground_busy + rx silent + elapsed < expected | Command running silently | Wait |
| foreground_busy + rx silent + elapsed > expected | Possibly stuck | Warn |
| NOT foreground_busy + rx silent | Session may be lost | Probe |

## Implementation Phases

1. **Phase 1**: Skip prompt timeout when `foreground_busy == True` (minimal)
2. **Phase 2**: Add `expected_duration_s` parameter
3. **Phase 3**: Output-based silence detection

## Risks

- Phase 1 alone could mask real session loss if command crashes
- Need a hard upper bound (e.g., 10x expected_duration or 30 min) as safety net
