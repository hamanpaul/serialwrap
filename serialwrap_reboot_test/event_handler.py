"""Event handler module for serialwrap-event-handler."""

import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Iterator, TextIO


SELECTOR_PATTERN = re.compile(r"^COM[0-9]+$")

EVENT_MATCH_TEXT = {
    "brcm-therm": "brcm-therm",
    "link-down": "Link is Down",
    "Link is Down": "Link is Down",
    "pstate": "pstate",
    "kernel-panic": "Kernel panic",
    "Kernel panic": "Kernel panic",
    "smc-bootloader": "SMC bootloader",
    "SMC bootloader": "SMC bootloader",
}


@contextlib.contextmanager
def _locked_file(lock_path: Path):
    """Context manager for file locking.

    Args:
        lock_path: Path to lock file.

    Yields:
        None (lock is held during context).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


def write_text_atomic(file_path: Path, content: str) -> None:
    """Write text to file atomically using temp file and rename.

    Args:
        file_path: Path to target file.
        content: Text content to write.

    Raises:
        IOError: On write failure.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(
        dir=file_path.parent,
        prefix=f".tmp_{file_path.name}_",
        suffix=".tmp"
    )

    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)

        # Atomic rename
        os.replace(temp_path, file_path)
    except (IOError, OSError) as e:
        # Clean up temp file on error
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise IOError(f"Failed to write file atomically: {e}")


def _normalize_event_name(payload: Dict[str, Any]) -> Optional[str]:
    """Return report event name from old or current serialwrap payload fields."""
    event = payload.get("event")
    if event is not None:
        if not isinstance(event, str):
            return None
        return EVENT_MATCH_TEXT.get(event)

    candidates = [
        payload.get("matched_text"),
        payload.get("rule_name"),
    ]
    rule_id = payload.get("rule_id")
    if isinstance(rule_id, str) and "." in rule_id:
        candidates.append(rule_id.rsplit(".", 1)[1])

    for candidate in candidates:
        if isinstance(candidate, str):
            normalized = EVENT_MATCH_TEXT.get(candidate)
            if normalized is not None:
                return normalized
    return None


def _event_timestamp(payload: Dict[str, Any]) -> str:
    """Return ISO trigger timestamp from old or current serialwrap payload fields."""
    timestamp = payload.get("timestamp")
    if isinstance(timestamp, str) and timestamp:
        return timestamp

    matched_at = payload.get("matched_at", payload.get("trigger_ts"))
    if isinstance(matched_at, (int, float)):
        value = float(matched_at)
        if value > 10_000_000_000:
            value = value / 1000.0
        return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat()

    return datetime.now(timezone.utc).astimezone().isoformat()


def parse_event_payload(payload_str: str) -> Optional[Dict[str, Any]]:
    """Parse event payload from JSON string.

    Args:
        payload_str: JSON string containing event payload.

    Returns:
        Parsed payload dict if valid with selector and event, None otherwise.
    """
    if not payload_str or not payload_str.strip():
        return None

    try:
        payload = json.loads(payload_str)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None

    selector = payload.get("selector")
    if not isinstance(selector, str) or not SELECTOR_PATTERN.match(selector):
        return None

    normalized_event = _normalize_event_name(payload)
    if normalized_event is None:
        return None

    payload["event"] = normalized_event
    payload["timestamp"] = _event_timestamp(payload)
    return payload


def _is_path_within_directory(path: Path, directory: Path) -> bool:
    """Check if path is within directory (no path traversal).

    Args:
        path: Path to check.
        directory: Directory that should contain the path.

    Returns:
        True if path is within directory, False otherwise.
    """
    try:
        path_resolved = path.resolve()
        dir_resolved = directory.resolve()
        return path_resolved.is_relative_to(dir_resolved)
    except (ValueError, RuntimeError, OSError):
        return False


def derive_report_path_from_log(selector: str, log_path: Path) -> Optional[Path]:
    """Derive report path from minicom log path.

    Args:
        selector: COM selector (e.g., COM0, COM1).
        log_path: Path to minicom log file.

    Returns:
        Report path if log name is standard format, None otherwise.
    """
    log_name = log_path.name

    # Expected format: mini_<selector>_<timestamp>.log
    parts = log_name.split('_')
    if len(parts) < 3:
        return None

    if not log_name.startswith(f"mini_{selector}_"):
        return None

    if not log_name.endswith('.log'):
        return None

    # Extract timestamp part (everything after mini_<selector>_)
    prefix = f"mini_{selector}_"
    timestamp_part = log_name[len(prefix):].replace('.log', '')

    if not timestamp_part:
        return None

    report_name = f"event-triggered_{selector}_{timestamp_part}.md"
    return log_path.parent / report_name


def validate_report_path_for_log(
    selector: str,
    log_path: Path,
    report_path: Path,
    log_dir: Path
) -> bool:
    """Validate a state-provided report path against the active log.

    Args:
        selector: COM selector.
        log_path: Active minicom log path.
        report_path: State-provided report path.
        log_dir: Directory containing minicom logs and reports.

    Returns:
        True if report_path is safe and matches the active log timestamp.
    """
    if not _is_path_within_directory(report_path, log_dir):
        return False

    derived_path = derive_report_path_from_log(selector, log_path)
    if derived_path is None:
        return False

    try:
        return report_path.resolve() == derived_path.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def resolve_active_state(
    selector: str,
    state_root: Path = Path("/tmp"),
    log_dir: Optional[Path] = None
) -> Tuple[Optional[Path], Optional[Path]]:
    """Resolve active log and report path for selector.

    Resolution order:
    1. Current-run state in state_root/serialwrap-reboot-test.<selector>.<pid>/
       - Read active_minicom_log.txt and optionally report_path.txt
       - Validate log path is within log_dir (security check)
    2. Fallback to latest mini_<selector>_*.log in log_dir updated within 10 minutes.

    Args:
        selector: COM selector (e.g., COM0, COM1).
        state_root: Root directory for state files (default /tmp).
        log_dir: Directory containing minicom logs (default ~/b-log).

    Returns:
        Tuple of (active_log_path, report_path) or (None, None) if not found.
        report_path is from state or derived from log, may be None if derivation fails.
    """
    if log_dir is None:
        log_dir = Path.home() / "b-log"

    # Try current-run state
    pattern = f"serialwrap-reboot-test.{selector}.*"
    for state_dir in state_root.glob(pattern):
        if not state_dir.is_dir():
            continue

        active_log_file = state_dir / "active_minicom_log.txt"
        if not active_log_file.exists():
            continue

        log_path_str = active_log_file.read_text().strip()
        log_path = Path(log_path_str)

        # Security check: validate path is within log_dir
        if not log_path.exists():
            continue

        if not _is_path_within_directory(log_path, log_dir):
            print(
                f"Warning: Rejected log path outside log_dir: {log_path}",
                file=sys.stderr
            )
            continue

        # Check for report_path.txt in state
        report_path_file = state_dir / "report_path.txt"
        if report_path_file.exists():
            report_path_str = report_path_file.read_text().strip()
            report_path = Path(report_path_str)
            if validate_report_path_for_log(selector, log_path, report_path, log_dir):
                return log_path, report_path
            print(
                f"Warning: Rejected report path inconsistent with active log: {report_path}",
                file=sys.stderr
            )

        # No report_path.txt, derive it
        report_path = derive_report_path_from_log(selector, log_path)
        return log_path, report_path

    # Fallback to recent log in log_dir
    pattern = f"mini_{selector}_*.log"
    candidates = []

    for log_path in log_dir.glob(pattern):
        if not log_path.is_file():
            continue

        mtime = log_path.stat().st_mtime
        age_minutes = (time.time() - mtime) / 60.0

        if age_minutes <= 10:
            candidates.append((mtime, log_path))

    if candidates:
        candidates.sort(reverse=True)
        log_path = candidates[0][1]
        report_path = derive_report_path_from_log(selector, log_path)
        return log_path, report_path

    return None, None


def resolve_active_log(
    selector: str,
    state_root: Path = Path("/tmp"),
    log_dir: Optional[Path] = None
) -> Optional[Path]:
    """Resolve active minicom log for selector.

    Deprecated: Use resolve_active_state() instead.

    Resolution order:
    1. Current-run state in state_root/serialwrap-reboot-test.<selector>.<pid>/
    2. Fallback to latest mini_<selector>_*.log in log_dir updated within 10 minutes.

    Args:
        selector: COM selector (e.g., COM0, COM1).
        state_root: Root directory for state files (default /tmp).
        log_dir: Directory containing minicom logs (default ~/b-log).

    Returns:
        Path to active log if found and exists, None otherwise.
    """
    log_path, _ = resolve_active_state(selector, state_root=state_root, log_dir=log_dir)
    return log_path


def scan_log_for_events(
    log_file: TextIO,
    event_pattern: str,
    start_line: int = 0
) -> Iterator[Tuple[int, str]]:
    """Scan log file for event matches.

    Args:
        log_file: Open text file to scan.
        event_pattern: Text pattern to search for.
        start_line: Line number to start scanning from (0-indexed, exclusive).

    Yields:
        Tuples of (line_number, line_text) for matches (1-indexed line numbers).
    """
    for line_num, line in enumerate(log_file, start=1):
        if line_num <= start_line:
            continue

        if event_pattern in line:
            yield (line_num, line.rstrip('\n'))


def load_scan_cursors(cursor_file: Path) -> Dict[str, int]:
    """Load scan cursors from file.

    Args:
        cursor_file: Path to cursor state file.

    Returns:
        Dictionary mapping event names to last matched line numbers.
    """
    if not cursor_file.exists():
        return {}

    try:
        with cursor_file.open() as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"Warning: Failed to parse cursor file {cursor_file}: {e}", file=sys.stderr)
        return {}
    except IOError as e:
        print(f"Warning: Failed to read cursor file {cursor_file}: {e}", file=sys.stderr)
        return {}


def _save_scan_cursors_unlocked(cursor_file: Path, cursors: Dict[str, int]) -> None:
    """Save scan cursors to file WITHOUT locking (internal helper).

    Used when caller already holds a lock. Merges with existing data.

    Args:
        cursor_file: Path to cursor state file.
        cursors: Dictionary mapping event names to line numbers to save/update.

    Raises:
        IOError: On save failure.
    """
    cursor_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing cursors and merge
    existing = load_scan_cursors(cursor_file)
    existing.update(cursors)

    # Atomic write: write to temp file then rename
    fd, temp_path = tempfile.mkstemp(
        dir=cursor_file.parent,
        prefix=".tmp_cursors_",
        suffix=".json"
    )

    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(existing, f, indent=2)

        # Atomic rename
        os.replace(temp_path, cursor_file)
    except (IOError, OSError, json.JSONEncodeError) as e:
        # Clean up temp file on error
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise IOError(f"Failed to save cursors: {e}")


def save_scan_cursors(cursor_file: Path, cursors: Dict[str, int]) -> None:
    """Save scan cursors to file with locking, merging with existing data.

    Args:
        cursor_file: Path to cursor state file.
        cursors: Dictionary mapping event names to line numbers to save/update.

    Raises:
        IOError: On save failure.
    """
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = cursor_file.parent / f"{cursor_file.name}.lock"

    with _locked_file(lock_file):
        _save_scan_cursors_unlocked(cursor_file, cursors)


def load_report_data(report_path: Path) -> Dict[str, Any]:
    """Load existing report data.

    Args:
        report_path: Path to report markdown file.

    Returns:
        Dictionary with 'events' (list) and 'summary' (dict) keys.
    """
    data = {
        "events": [],
        "summary": {}
    }

    if not report_path.exists():
        return data

    content = report_path.read_text()

    # Parse summary table
    summary_match = re.search(
        r'## Summary.*?\| Event \| Count \|.*?\n\| --- \| ---: \| ---: \|\n(.*?)(?=\n##|\Z)',
        content,
        re.DOTALL
    )
    if summary_match:
        for line in summary_match.group(1).strip().split('\n'):
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 3:
                event_name = parts[1]
                try:
                    count = int(parts[2])
                    data["summary"][event_name] = count
                except ValueError as e:
                    print(f"Warning: Malformed summary row in {report_path}, skipping: {line.strip()}", file=sys.stderr)

    # Parse event table
    events_match = re.search(
        r'## Events.*?\| Log name \| Log line number \|.*?\n\| --- \| ---: \| --- \| --- \|\n(.*?)(?=\n##|\Z)',
        content,
        re.DOTALL
    )
    if events_match:
        for line in events_match.group(1).strip().split('\n'):
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 5:
                try:
                    event_entry = {
                        "log_name": parts[1],
                        "line_number": int(parts[2]),
                        "timestamp": parts[3],
                        "event": parts[4]
                    }
                    data["events"].append(event_entry)
                except (ValueError, IndexError) as e:
                    print(f"Warning: Malformed event row in {report_path}, skipping: {line.strip()}", file=sys.stderr)

    return data


def generate_report_content(
    log_name: str,
    events: List[Dict[str, Any]],
    summary: Dict[str, int]
) -> str:
    """Generate report markdown content.

    Args:
        log_name: Name of the minicom log file.
        events: List of event dictionaries with log_name, line_number, timestamp, event.
        summary: Dictionary mapping event names to counts.

    Returns:
        Report markdown content as string.
    """
    now = datetime.now(timezone.utc).astimezone().isoformat()

    # Calculate probabilities
    denominator = summary.get("SMC bootloader", 0)

    # Define event order
    event_order = [
        "brcm-therm",
        "Link is Down",
        "pstate",
        "Kernel panic",
        "SMC bootloader"
    ]

    lines = [
        "# Event Triggered Report",
        "",
        f"Log: {log_name}",
        f"Generated: {now}",
        "",
        "## Summary",
        "",
        f"Denominator: SMC bootloader = {denominator}",
        "",
        "| Event | Count | Probability vs SMC bootloader |",
        "| --- | ---: | ---: |",
    ]

    for event_name in event_order:
        count = summary.get(event_name, 0)
        if denominator == 0:
            # When denominator is 0, all events show N/A
            prob_str = "N/A"
        else:
            prob = (count / denominator) * 100
            prob_str = f"{prob:.2f}%"

        lines.append(f"| {event_name} | {count} | {prob_str} |")

    lines.extend([
        "",
        "## Events",
        "",
        "| Log name | Log line number | Event trigger time | Event |",
        "| --- | ---: | --- | --- |",
    ])

    for event in events:
        lines.append(
            f"| {event['log_name']} | {event['line_number']} | "
            f"{event['timestamp']} | {event['event']} |"
        )

    return '\n'.join(lines) + '\n'


def handle_event(
    payload: Dict[str, Any],
    state_root: Path = Path("/tmp"),
    log_dir: Optional[Path] = None
) -> int:
    """Handle a single event.

    Args:
        payload: Parsed event payload dictionary.
        state_root: Root directory for state files.
        log_dir: Directory containing minicom logs.

    Returns:
        0 on success, non-zero on error.
    """
    selector = payload["selector"]
    event_name = payload["event"]
    event_timestamp = payload.get("timestamp", datetime.now(timezone.utc).astimezone().isoformat())

    if log_dir is None:
        log_dir = Path.home() / "b-log"

    try:
        # Resolve active log and report path
        log_path, report_path = resolve_active_state(selector, state_root=state_root, log_dir=log_dir)
        if log_path is None:
            print(f"Error: Could not resolve active log for selector {selector}", file=sys.stderr)
            return 1

        # Validate or derive report path
        if report_path is None:
            report_path = derive_report_path_from_log(selector, log_path)
            if report_path is None:
                print(
                    f"Error: Could not derive report path from log: {log_path.name}",
                    file=sys.stderr
                )
                return 1

        # Derive cursor file path from log name
        log_basename = log_path.name
        # Extract timestamp part safely
        parts = log_basename.split('_')
        if len(parts) >= 3:
            timestamp_part = log_basename.split('_', 2)[2].replace('.log', '')
        else:
            print(
                f"Error: Cannot derive cursor file from non-standard log name: {log_basename}",
                file=sys.stderr
            )
            return 1

        cursor_file = log_dir / f".event-cursors_{selector}_{timestamp_part}.json"

        # Use a lock file based on report path to coordinate report+cursor updates
        lock_file = report_path.parent / f".{report_path.name}.lock"

        with _locked_file(lock_file):
            # Load cursors inside lock to prevent race
            cursors = load_scan_cursors(cursor_file)
            start_line = cursors.get(event_name, 0)

            # Scan log for next match (streaming - only consume first match)
            try:
                with log_path.open() as f:
                    match_iter = scan_log_for_events(f, event_name, start_line=start_line)
                    try:
                        line_number, line_text = next(match_iter)
                    except StopIteration:
                        print(f"Warning: No new matches for event '{event_name}' in {log_path}", file=sys.stderr)
                        return 0
            except (IOError, OSError) as e:
                print(f"Error: Failed to read log file {log_path}: {e}", file=sys.stderr)
                return 1

            # Load existing report data
            report_data = load_report_data(report_path)

            # Add new event
            report_data["events"].append({
                "log_name": log_basename,
                "line_number": line_number,
                "timestamp": event_timestamp,
                "event": event_name
            })

            # Update summary
            report_data["summary"][event_name] = report_data["summary"].get(event_name, 0) + 1

            # Generate and write report atomically
            content = generate_report_content(
                log_basename,
                report_data["events"],
                report_data["summary"]
            )

            try:
                write_text_atomic(report_path, content)
            except IOError as e:
                print(f"Error: Failed to write report {report_path}: {e}", file=sys.stderr)
                return 1

            # Update cursor after successful report write (under same lock, no nested lock)
            try:
                _save_scan_cursors_unlocked(cursor_file, {event_name: line_number})
            except IOError as e:
                print(f"Error: Failed to save cursor: {e}", file=sys.stderr)
                return 1

        return 0

    except PermissionError as e:
        print(f"Error: Permission denied: {e}", file=sys.stderr)
        return 1
    except (IOError, OSError) as e:
        print(f"Error: I/O error: {e}", file=sys.stderr)
        return 1


def main():
    """Main entry point for the event handler."""
    try:
        # Read payload from stdin
        payload_str = sys.stdin.read()

        # Parse payload
        payload = parse_event_payload(payload_str)
        if payload is None:
            print("Error: Invalid event payload", file=sys.stderr)
            return 1

        # Handle event
        return handle_event(payload)

    except KeyboardInterrupt:
        return 130
    except IOError as e:
        print(f"Error: I/O error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
