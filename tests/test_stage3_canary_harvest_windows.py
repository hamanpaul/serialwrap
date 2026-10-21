"""P9-T8 Windows WAL 開檔分享旗標測試。"""
from __future__ import annotations

import importlib
import os
import threading
from pathlib import Path

import pytest


pytestmark = [pytest.mark.windows_native, pytest.mark.memory_only]


def _rename(path: Path, destination: Path, errors: list[BaseException]) -> None:
    try:
        os.replace(path, destination)
    except BaseException as exc:
        errors.append(exc)


def test_rename_while_reading_windows(tmp_path):
    harvest = importlib.import_module("tools.stage3.canary_harvest")
    wal = tmp_path / "raw.wal.ndjson"
    archived = tmp_path / "raw.wal.ndjson.20261102-010000"
    wal.write_bytes(b'{"seq":1}\n')
    identity_before = harvest.file_identity(wal)

    errors: list[BaseException] = []
    with harvest.open_shared(wal):
        worker = threading.Thread(target=_rename, args=(wal, archived, errors))
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert errors == []
    assert harvest.file_identity(archived) == identity_before

    replacement = tmp_path / "raw.wal.ndjson"
    replacement.write_bytes(b'{"seq":2}\n')
    ordinary_archive = tmp_path / "raw.wal.ndjson.20261102-010001"
    errors = []
    with replacement.open("rb"):
        worker = threading.Thread(target=_rename, args=(replacement, ordinary_archive, errors))
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PermissionError)

