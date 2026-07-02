"""#120 live guard 判定純函式——每一個 adversarial review F1/F2 失敗模式一個 case。"""
from __future__ import annotations

import json

import liveguard


def _snap(content: bytes | None):
    if content is None:
        return liveguard.FileSnap(exists=False)
    return liveguard.FileSnap(exists=True, content=content, size=len(content))


def _state(aliases=None, bindings=None, released=None):
    return json.dumps(
        {"aliases": aliases or {}, "bindings": bindings or {},
         "released": released or {}, "profile_pins": {}, "profile_detected": {}},
        sort_keys=True, separators=(",", ":"),
    ).encode()


PRE = _state(aliases={"dut": {"session_id": "prpl-template:COM0"}},
             released={"p:COM9": {"by_id": "x"}})


# ---- Guard 1: state ----

def test_state_created_from_absent_fails():
    v, _ = liveguard.classify_state(_snap(None), _snap(_state()), mode="strict")
    assert v == "FAIL"


def test_state_byte_identical_passes():
    v, _ = liveguard.classify_state(_snap(PRE), _snap(PRE), mode="strict")
    assert v == "PASS"


def test_state_clean_overwrite_fails_strict():
    """乾淨覆寫（無污染特徵、released 消失）在 strict 必 FAIL。"""
    v, _ = liveguard.classify_state(_snap(PRE), _snap(_state()), mode="strict")
    assert v == "FAIL"


def test_state_released_cleared_fails_even_in_warn():
    """released entry 消失＝結構性破壞，warn 模式仍 FAIL。"""
    v, _ = liveguard.classify_state(_snap(PRE), _snap(_state(aliases={"dut": {"session_id": "prpl-template:COM0"}})), mode="warn")
    assert v == "FAIL"


def test_state_pollution_marker_fails_even_in_warn():
    post = _state(aliases={"dut": {"session_id": "prpl-template:COM0"}},
                  bindings={"prpl-template:COM0": "/tmp/sw-coexist-x/by-id/fake-uart0"},
                  released={"p:COM9": {"by_id": "x"}})
    v, _ = liveguard.classify_state(_snap(PRE), _snap(post), mode="warn")
    assert v == "FAIL"


def test_state_benign_addition_warns_in_warn_mode():
    """warn 模式下，非結構性、無特徵的變更（如 live daemon 合法新增 alias）→ WARN。"""
    post = _state(aliases={"dut": {"session_id": "prpl-template:COM0"},
                           "sta": {"session_id": "prpl-template:COM1"}},
                  released={"p:COM9": {"by_id": "x"}})
    v, _ = liveguard.classify_state(_snap(PRE), _snap(post), mode="warn")
    assert v == "WARN"


def test_state_deleted_fails():
    v, _ = liveguard.classify_state(_snap(PRE), _snap(None), mode="warn")
    assert v == "FAIL"


# ---- Guard 2: WAL ----

def test_wal_append_passes():
    v, _ = liveguard.classify_wal(
        liveguard.FileSnap(exists=True, size=100), liveguard.FileSnap(exists=True, size=200))
    assert v == "PASS"


def test_wal_shrink_fails():
    v, _ = liveguard.classify_wal(
        liveguard.FileSnap(exists=True, size=200), liveguard.FileSnap(exists=True, size=100))
    assert v == "FAIL"


def test_wal_deleted_fails():
    v, _ = liveguard.classify_wal(
        liveguard.FileSnap(exists=True, size=200), liveguard.FileSnap(exists=False))
    assert v == "FAIL"


def test_wal_absent_both_passes():
    v, _ = liveguard.classify_wal(liveguard.FileSnap(exists=False), liveguard.FileSnap(exists=False))
    assert v == "PASS"


def test_shell_wal_any_change_fails():
    """外層 shell SERIALWRAP_WAL_DIR 維度：任何變更（size 或存在性）→ FAIL。"""
    v, _ = liveguard.classify_shell_wal(
        liveguard.FileSnap(exists=True, size=100), liveguard.FileSnap(exists=True, size=101))
    assert v == "FAIL"


# ---- Guard 3: config ----

def test_config_change_fails():
    v, _ = liveguard.classify_config(_snap(b"a: 1\n"), _snap(b"a: 2\n"))
    assert v == "FAIL"


def test_config_identical_passes():
    v, _ = liveguard.classify_config(_snap(b"a: 1\n"), _snap(b"a: 1\n"))
    assert v == "PASS"


# ---- Guard 4: daemon ----

def _dsnap(**kw):
    base = dict(reachable=True, active=True, main_pid=1234,
                sessions={"prpl-template:COM0": ("2026-07-02T00:00:00+00:00", 1)})
    base.update(kw)
    return liveguard.DaemonSnap(**base)


def test_daemon_unreachable_pre_skips():
    v, _ = liveguard.classify_daemon(_dsnap(reachable=False), _dsnap())
    assert v == "SKIP"


def test_daemon_pid_change_fails():
    v, _ = liveguard.classify_daemon(_dsnap(), _dsnap(main_pid=5678))
    assert v == "FAIL"


def test_daemon_inactive_post_fails():
    v, _ = liveguard.classify_daemon(_dsnap(), _dsnap(active=False))
    assert v == "FAIL"


def test_daemon_tx_advance_fails():
    post = _dsnap(sessions={"prpl-template:COM0": ("2026-07-02T00:05:00+00:00", 1)})
    v, _ = liveguard.classify_daemon(_dsnap(), post)
    assert v == "FAIL"


def test_daemon_bridge_generation_change_fails():
    post = _dsnap(sessions={"prpl-template:COM0": ("2026-07-02T00:00:00+00:00", 2)})
    v, _ = liveguard.classify_daemon(_dsnap(), post)
    assert v == "FAIL"


def test_daemon_untouched_passes():
    v, _ = liveguard.classify_daemon(_dsnap(), _dsnap())
    assert v == "PASS"
