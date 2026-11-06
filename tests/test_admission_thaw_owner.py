"""tests/test_admission_thaw_owner.py — AdmissionGate 凍結撤銷只限建立者（審查 sol 8 Low）。

交接（handoff_stop）若在 `freeze_if_idle` 之前已有其他路徑（例如 daemon stop()）呼叫 `freeze()`，
交接失敗時的 thaw 不得撤掉該凍結；只有由 `freeze_if_idle` 新建立的凍結才可被撤銷。
"""
from __future__ import annotations

import pytest

from sw_core.admission import AdmissionGate

pytestmark = [pytest.mark.memory_only]


@pytest.mark.core_portable
def test_thaw_revokes_freeze_created_by_freeze_if_idle():
    gate = AdmissionGate()
    token, busy = gate.freeze_if_idle("update")
    assert token is not None and busy == {}
    assert gate.thaw(token) is True
    assert gate.frozen is False


@pytest.mark.core_portable
def test_thaw_does_not_revoke_shutdown_freeze_taken_before_handoff_check():
    gate = AdmissionGate()
    gate.freeze()  # daemon stop() 搶先凍結
    token, busy = gate.freeze_if_idle("update")  # 交接的檢查發生在其後：非建立者
    assert token is not None and busy == {}
    assert gate.thaw(token) is False, "交接失敗時不得撤掉 stop() 建立的凍結"
    assert gate.frozen is True


@pytest.mark.core_portable
def test_shutdown_freeze_after_handoff_creation_cannot_be_thawed_by_handoff():
    gate = AdmissionGate()
    token, busy = gate.freeze_if_idle("update")
    assert token is not None and busy == {}
    gate.freeze()  # stop() 於 handoff freeze 與 TX fence 結果之間接手凍結
    assert gate.thaw(token) is False, "stop() 接手後不可被交接失敗路徑撤銷"
    assert gate.frozen is True


@pytest.mark.core_portable
def test_repeated_handoff_freeze_does_not_revoke_earlier_creator_freeze():
    gate = AdmissionGate()
    first, _ = gate.freeze_if_idle("update")
    second, _ = gate.freeze_if_idle("update")  # 重複呼叫：已凍結，非建立者
    assert second == first
    assert gate.thaw(second) is False
    assert gate.frozen is True


@pytest.mark.core_portable
def test_thaw_rejects_unknown_token():
    gate = AdmissionGate()
    token, _ = gate.freeze_if_idle("update")
    assert gate.thaw("not-the-token") is False
    assert gate.frozen is True
    assert gate.thaw(token) is True
