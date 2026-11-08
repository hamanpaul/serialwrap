"""Windows `exec_or_wait` Ctrl event regression guard."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "crates" / "sw-pal" / "tests" / "exec_delegate.rs"


def test_ctrl_event_parent_waits_for_live_delegated_process() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert 'pub const DELEGATE_READY_ENV: &str = "SW_PAL_CTRLC_DELEGATE_READY"' in source
    assert 'fn process_is_running(pid: u32) -> bool' in source
    assert 'probe.process.exec_or_wait(' in source
    assert 'std::env::current_exe().unwrap().into_os_string()' in source
    assert '["--ignored", "--exact", TEST, "--nocapture"]' in source
    assert 'win_ctrl::process_is_running(delegate_pid)' in source
    assert 'std::thread::sleep(std::time::Duration::from_secs(2))' not in source
