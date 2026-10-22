"""Slice 2.1 骨架的機械守衛（計畫 06 Task 0；P5 Task 0 雙審修正）。

- 有界資源常數：`contracts/resource_limits.yaml` 每一列都有 Rust 鏡像，且鏡像數值逐項相等（YAML 與鏡像集合完全相同）。
  `cmd_pending_max`／`cmd_history_max` 鏡像於 `sw_proto::constants`；`DEFAULT_WAL_ROTATE_BYTES` 由 Global Constraints／WAL-D7 固定。
- 方法目錄分割：`rpc_types.rs` 的 `KNOWN`（55 項）＝ served ∪ deferred ∪ unsupported_catalog（互斥）。
- AlwaysOpen 旁路：業務 crate（sw-core、sw-daemon）不得使用 `AlwaysOpen`，也不得以不帶 gate 的 `.write(…, cancel)` 繞過 Fence。
  守衛以整檔文字比對（容許 rustfmt 換行與任意以 `cancel` 開頭的變數名），並以自我測試確認樣式本身有效。
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

# 純檔案解析，與平台無關、不啟動行程（規則 2：模組層平台 marker，資源 marker 逐項標示）。
pytestmark = [pytest.mark.core_portable]

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIMITS = ROOT / "contracts" / "resource_limits.yaml"
SCOPE = ROOT / "tests" / "acceptance" / "slice21_scope.yaml"
RPC_TYPES = ROOT / "crates" / "sw-proto" / "src" / "rpc_types.rs"
RUST_SRC_DIRS = [ROOT / "crates" / "sw-core" / "src", ROOT / "crates" / "sw-daemon" / "src"]

# YAML 名稱 → (Rust 常數所在檔, 常數名)。涵蓋 `contracts/resource_limits.yaml` 全部 20 列（P5 Task 0 雙審修正：含 cmd_pending_max）。
MIRROR = {
    "mailbox_control": ("resources.rs", "MAILBOX_CONTROL_CAP"),
    "mailbox_data_chunks": ("resources.rs", "MAILBOX_DATA_CHUNKS_CAP"),
    "mailbox_data_chunk_bytes": ("resources.rs", "MAILBOX_DATA_CHUNK_BYTES"),
    "mailbox_stash_max": ("resources.rs", "MAILBOX_STASH_MAX"),
    "tx_writer_jobs": ("resources.rs", "TX_WRITER_JOBS_CAP"),
    "cmd_pending_max": ("constants.rs", "CMD_PENDING_MAX"),
    "cmd_history_max": ("constants.rs", "CMD_HISTORY_MAX"),
    "bg_capture_max_count": ("resources.rs", "BG_CAPTURE_MAX_COUNT"),
    "bg_capture_max_chars": ("resources.rs", "BG_CAPTURE_MAX_CHARS"),
    "rx_window_max_chars": ("resources.rs", "RX_WINDOW_MAX_CHARS"),
    "rx_rate_window_entries": ("resources.rs", "RX_RATE_WINDOW_ENTRIES"),
    "deferred_input_max_bytes": ("resources.rs", "DEFERRED_INPUT_MAX_BYTES"),
    "rpc_connections": ("resources.rs", "RPC_CONNECTIONS_MAX"),
    "rpc_request_line_bytes": ("resources.rs", "RPC_REQUEST_LINE_BYTES"),
    "interactive_tombstones": ("resources.rs", "INTERACTIVE_TOMBSTONES_MAX"),
    "drain_timeout_ms": ("resources.rs", "DRAIN_TIMEOUT_MS"),
    "tx_ledger_live_ops": ("tx_writer.rs", "TX_LEDGER_LIVE_MAX"),
    "tx_ledger_unresolved_gaps": ("tx_writer.rs", "TX_LEDGER_UNRESOLVED_GAPS_MAX"),
    "tx_op_wal_gaps": ("tx_writer.rs", "TX_OP_WAL_GAPS_MAX"),
    "tx_settle_queue": ("tx_writer.rs", "TX_SETTLE_QUEUE_MAX"),
}
MIRROR_FILES = {
    "resources.rs": ROOT / "crates" / "sw-core" / "src" / "resources.rs",
    "tx_writer.rs": ROOT / "crates" / "sw-core" / "src" / "tx_writer.rs",
    "constants.rs": ROOT / "crates" / "sw-proto" / "src" / "constants.rs",
}
# 不在 YAML 內、由 Global Constraints／WAL-D7（§1.1.3）固定的 Rust 常數。
EXTRA_MIRROR = {"DEFAULT_WAL_ROTATE_BYTES": ("constants.rs", 67108864)}
FRAMING = ROOT / "crates" / "sw-daemon" / "src" / "framing.rs"

CONST_RE = re.compile(r"pub const (\w+): \w+ = ([0-9_]+);")


def _rust_consts(path: pathlib.Path) -> dict[str, int]:
    return {name: int(value.replace("_", "")) for name, value in CONST_RE.findall(path.read_text(encoding="utf-8"))}


@pytest.mark.memory_only
def test_resource_limit_rust_mirrors_equal_yaml():
    limits = {row["name"]: row["value"] for row in yaml.safe_load(LIMITS.read_text(encoding="utf-8"))["limits"]}
    # 完整性：YAML 每一列都有鏡像，鏡像也不多列（P5 Task 0 雙審修正）。
    assert set(limits) == set(MIRROR), sorted(set(limits) ^ set(MIRROR))
    consts = {fname: _rust_consts(path) for fname, path in MIRROR_FILES.items()}
    bad = []
    for name, (fname, const) in MIRROR.items():
        if const not in consts[fname]:
            bad.append((name, "missing", fname, const))
        elif consts[fname][const] != limits[name]:
            bad.append((name, consts[fname][const], limits[name]))
    for const, (fname, expected) in EXTRA_MIRROR.items():
        if consts[fname].get(const) != expected:
            bad.append((const, consts[fname].get(const), expected))
    # framing 的請求行上限與 resources 的 RPC_REQUEST_LINE_BYTES 為同一契約值（R1 重複定義須保持一致）。
    framing = _rust_consts(FRAMING)
    if framing.get("MAX_LINE_BYTES") != limits["rpc_request_line_bytes"]:
        bad.append(("rpc_request_line_bytes", "framing.MAX_LINE_BYTES", framing.get("MAX_LINE_BYTES")))
    assert bad == []


def _block_names(text: str, header: str) -> list[str]:
    start = text.index(header)
    body = text[text.index("[", start) + 1: text.index("];", start)]
    return re.findall(r'"([a-z_.]+)"', body)


@pytest.mark.memory_only
def test_rust_method_catalog_partitions_into_served_deferred_unsupported():
    known = _block_names(RPC_TYPES.read_text(encoding="utf-8"), "static KNOWN:")
    assert len(known) == len(set(known)) == 55
    scope = yaml.safe_load(SCOPE.read_text(encoding="utf-8"))
    served = set(scope["served_methods"])
    deferred = {row["method"] for row in scope["deferred_methods"]}
    unsupported = {row["method"] for row in scope["unsupported_catalog_methods"]}
    assert served.isdisjoint(deferred) and served.isdisjoint(unsupported) and deferred.isdisjoint(unsupported)
    assert served | deferred | unsupported == set(known)


# `.write(` 呼叫且參數中出現以 `cancel` 開頭的識別字（含 `&self.cancel_token`）；`;` 結束搜尋以免跨語句誤判。
BYPASS_WRITE = re.compile(r"\.write\s*\([^;]*?\bcancel\w*", re.S)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


@pytest.mark.memory_only
def test_bypass_pattern_detects_rustfmt_layouts_and_ignores_gated_calls():
    assert BYPASS_WRITE.search("w.write(\n    &p,\n    &cancel,\n);")
    assert BYPASS_WRITE.search("w.write(&p, &self.cancel_token);")
    assert not BYPASS_WRITE.search("w.write_gated(&p, &cancel, &gate);")
    assert not BYPASS_WRITE.search("stream.write(buf);\nlet cancel = token();")


@pytest.mark.memory_only
def test_business_crates_cannot_bypass_fence_with_always_open():
    hits = []
    for src in RUST_SRC_DIRS:
        for path in sorted(src.rglob("*.rs")):
            text = path.read_text(encoding="utf-8")
            for match in BYPASS_WRITE.finditer(text):
                hits.append(f"{path.relative_to(ROOT)}:{_line_of(text, match.start())}")
            for lineno, line in enumerate(text.splitlines(), 1):
                if "AlwaysOpen" in line:
                    hits.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert hits == [], hits
