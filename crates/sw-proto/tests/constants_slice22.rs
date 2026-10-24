//! P6 Task 1：Slice 2.2 常數與 Python 基線一致（07 Task 1 Step 1，逐字）。
use sw_proto::constants::*;

#[test]
fn slice22_constants_match_python_baseline() {
    assert_eq!(CONSOLE_LINE_BUFFER_MAX, 16385);
    assert_eq!(CONSOLE_OUT_RING_BYTES, 1 << 20);
    assert_eq!(CONSOLE_CONTROL_MAX_BYTES, 65536);
    assert_eq!(TELNET_CONSOLES_PER_SESSION, 32);
    assert_eq!(HUMAN_TX_BACKLOG_MAX_BYTES, DEFERRED_INPUT_MAX_BYTES);
    assert_eq!(DEFERRED_INPUT_MAX_BYTES, 262144);
    assert_eq!(INTERACTIVE_TOMBSTONE_MAX, 16);
    assert_eq!(DEFAULT_INTERACTIVE_TIMEOUT_S, 60.0);
    assert_eq!(DEFAULT_SCREEN_CHARS, 2048);
    assert_eq!(HUMAN_CONSOLE_CMD_TIMEOUT_S, 30.0);
    assert_eq!(HUMAN_CONSOLE_CMD_PRIORITY, 100i128);
    assert_eq!(HUMAN_PEER_GRACE_S, 3.0);
    assert_eq!(STALE_CONSOLE_GRACE_S, 2.0);
    assert_eq!(HUMAN_ACTIVE_WINDOW_S, 60.0);
    assert_eq!(MAX_RECOVERY_LEASE_S, 120.0);
    assert_eq!(BOOTLOADER_RX_TAIL_BYTES, 512);
    assert_eq!(ATTACHED_CONSOLE_LEASE_TIMEOUT_S, 86400.0);
    let mut sorted = HUMAN_INTERACTIVE_COMMANDS.to_vec(); sorted.sort();
    assert_eq!(sorted, HUMAN_INTERACTIVE_COMMANDS.to_vec(), "清單維持字母序以便對照 service.py:49-68");
    assert_eq!(telnet::TELNET_GREETING, [0xFF,0xFB,0x01,0xFF,0xFB,0x03,0xFF,0xFD,0x03,0xFF,0xFB,0x00]);
}
