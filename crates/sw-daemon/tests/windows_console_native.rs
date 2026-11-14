//! S2-GP-gpx 第 11 項：TCP/Telnet console 原生 socket 端到端與選配 com0com 串列 console。
#![cfg(feature = "test-fakes")]

mod support;

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};

use support::*;
use sw_proto::pyjson::{dumps, PyDict, PyValue, RPC_WIRE};

const TELNET_GREETING: &[u8] = &[0xff, 0xfb, 0x01, 0xff, 0xfb, 0x03, 0xff, 0xfd, 0x03, 0xff, 0xfb, 0x00];

fn j(value: &PyDict) -> String {
    dumps(&PyValue::Dict(value.clone()), &RPC_WIRE)
}

fn text(value: &PyDict, key: &str) -> String {
    value.get(key).and_then(PyValue::as_str).unwrap_or_default().to_string()
}

fn attach(r: &ServiceRig) -> PyDict {
    r.call("session.console_attach", r#"{"selector":"COM0"}"#)
}

fn connect(address: &str) -> TcpStream {
    let (host, port) = address.rsplit_once(':').expect("host:port endpoint");
    assert_eq!(host, "127.0.0.1", "console listener 只能綁 loopback");
    TcpStream::connect((host, port.parse::<u16>().expect("TCP port"))).expect("連線到原生 TCP console")
}

fn greeting(stream: &mut TcpStream) {
    stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let mut bytes = [0u8; 12];
    stream.read_exact(&mut bytes).expect("讀取 Telnet greeting");
    assert_eq!(&bytes, TELNET_GREETING);
}

fn wait_for(what: &str, mut condition: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !condition() {
        assert!(Instant::now() < deadline, "等待逾時：{what}");
        std::thread::sleep(Duration::from_millis(5));
    }
}

fn wal_has_human_tx(r: &ServiceRig, bytes_b64: &str) -> bool {
    r.wal
        .tail_raw_with_meta(None, Some("COM0"), 10_000)
        .expect("讀取測試 WAL")
        .0
        .iter()
        .any(|row| {
            row.get("dir").and_then(PyValue::as_str) == Some("TX")
                && row.get("source").and_then(PyValue::as_str).is_some_and(|v| v.starts_with("human:"))
                && row.get("payload_b64").and_then(PyValue::as_str) == Some(bytes_b64)
        })
}

/// Service::rpc(session.console_attach) → ConsoleHub listener → native TCP socket → human TX pump → FakeSerial/WAL。
/// 同時核對 config 指定的 serial console 在 bridge 與 listener 建立後才交給同一個 hub。
#[test]
fn telnet_hub_end_to_end_with_fake_serial() {
    let r = ServiceRig::with_native_tcp_console_and_serial_console("COM22", "COM23", false);
    wait_for("COM22 serial console 交給 hub", || {
        r.console.as_ref().unwrap().opened_serial_consoles().len() == 1
    });
    assert_eq!(r.console.as_ref().unwrap().opened_serial_consoles(), ["COM22"]);

    let response = attach(&r);
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&response));
    assert_eq!(response.get("client_id"), Some(&PyValue::Null), "TCP attach 不建立 socket client");
    assert_eq!(text(&response, "transport"), "tcp");
    assert_eq!(text(&response, "protocol"), "telnet");
    let endpoint = text(&response, "endpoint");
    assert!(endpoint.starts_with("127.0.0.1:"), "端點只綁 loopback：{}", j(&response));
    let session = match response.get("session") {
        Some(PyValue::Dict(session)) => session,
        _ => panic!("session：{}", j(&response)),
    };
    let want = format!(
        r#"{{"ok":true,"client_id":null,"label":null,"vtty":"{endpoint}","endpoint":"{endpoint}","transport":"tcp","protocol":"telnet","session":{},"version":"{}"}}"#,
        j(session),
        r.version
    );
    assert_eq!(j(&response), want, "TCP attach 回應逐位元組對齊 Python 形狀");
    assert_eq!(session.get("interactive_session_id"), Some(&PyValue::Null), "OD-85：TCP attach 不開 human lease");
    let interactive = r.call("session.interactive_open", r#"{"selector":"COM0","owner":"agent"}"#);
    assert_eq!(interactive.get("ok"), Some(&PyValue::Bool(true)), "TCP attach 不得佔用 lease：{}", j(&interactive));
    let interactive_id = text(&interactive, "interactive_id");
    let close = r.call(
        "session.interactive_close",
        &format!(r#"{{"interactive_id":"{interactive_id}"}}"#),
    );
    assert_eq!(close.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&close));

    let address = text(&response, "endpoint");
    assert_eq!(text(&response, "vtty"), address);
    let mut socket = connect(&address);
    greeting(&mut socket);
    wait_for("daemon 登記已 accept 的 socket", || {
        r.call("session.console_list", r#"{"selector":"COM0"}"#)
            .get("consoles")
            .and_then(PyValue::as_array)
            .is_some_and(|rows| {
                rows.iter().any(|row| match row {
                    PyValue::Dict(row) => row.get("vtty").and_then(PyValue::as_str) == Some(address.as_str()),
                    _ => false,
                })
            })
    });
    let device = r.device(REAL_A);
    let _ = device.take_tx();
    socket.write_all(b"ab").expect("送出 Telnet client bytes");
    let mut uart_tx = Vec::new();
    wait_for("socket bytes 到 FakeSerial", || {
        uart_tx.extend(device.take_tx());
        uart_tx.windows(2).any(|window| window == b"ab")
    });
    wait_for("WAL 記錄 human TX", || wal_has_human_tx(&r, "YWI="));
    device.push_rx(b"uart-rx-from-fake-serial");
    let mut console_rx = Vec::new();
    wait_for("FakeSerial RX 經 native socket fan-out", || {
        let mut chunk = [0u8; 128];
        match socket.read(&mut chunk) {
            Ok(count) => console_rx.extend_from_slice(&chunk[..count]),
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) => {}
            Err(error) => panic!("讀取 TCP console：{error}"),
        }
        console_rx.windows(24).any(|window| window == b"uart-rx-from-fake-serial")
    });
    assert!(
        r.console.as_ref().unwrap().opened_serial_consoles().len() == 1,
        "單一 session 只開一次 serial console"
    );
}

/// S2-GP-gpx M1：release → attach → 新 bridge adoption，hub 已關閉的 serial console 須恰好重開一次。
#[test]
fn gpx_release_then_reattach_reopens_com0com_console_exactly_once() {
    let r = ServiceRig::with_fake_tcp_console_and_serial_console("COM22", "COM23");
    wait_for("初始 COM22 serial console 開啟命令", || {
        r.console.as_ref().unwrap().opened_serial_consoles().len() == 1
    });
    assert_eq!(r.console.as_ref().unwrap().opened_serial_consoles(), ["COM22"]);
    let initial_console = r.console.as_ref().unwrap().fake.com0com_opened()[0].1.clone();
    r.wait_state("COM0", "ATTACHED");
    r.clock.advance(Duration::from_secs(31));
    r.wait_state("COM0", "READY");

    let released = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(
        released.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&released)
    );
    assert_eq!(
        text(&r.session("COM0").expect("released session"), "state"),
        "RELEASED"
    );
    assert!(initial_console.closed(), "device.release 必須關閉既有 com0com console");

    let attached = r.call("device.attach", r#"{"selector":"COM0"}"#);
    assert_eq!(
        attached.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&attached)
    );
    r.wait_state("COM0", "ATTACHED");
    wait_for("reattach 後第二次 COM22 serial console 開啟命令", || {
        r.console.as_ref().unwrap().opened_serial_consoles().len() == 2
    });
    r.clock.advance(Duration::from_secs(31));
    r.wait_state("COM0", "READY");
    assert_eq!(
        r.console.as_ref().unwrap().opened_serial_consoles(),
        ["COM22", "COM22"],
        "每次 bridge 恢復只補一個已關閉的 serial console"
    );
    assert_eq!(
        r.console.as_ref().unwrap().fake.com0com_opened().len(),
        2,
        "重掛後恰好建立第二個成功的 fake com0com console"
    );
}

/// 第一個 TCP client 使用 raw ownership；第二個 client 走 line mode，CRLF 送出一筆 UART 命令。
#[test]
fn telnet_second_client_line_mode() {
    let r = ServiceRig::with_native_tcp_console();
    let response = attach(&r);
    let address = text(&response, "endpoint");
    let mut first = connect(&address);
    greeting(&mut first);
    let mut second = connect(&address);
    greeting(&mut second);
    wait_for("daemon 登記兩個 socket client", || {
        r.call("session.console_list", r#"{"selector":"COM0"}"#)
            .get("consoles")
            .and_then(PyValue::as_array)
            .is_some_and(|rows| rows.len() == 2)
    });
    let device = r.device(REAL_A);
    let _ = device.take_tx();
    second.write_all(b"ls\r\n").expect("送出 line-mode 輸入");
    let mut uart_tx = Vec::new();
    wait_for("第二 client 的行送到 FakeSerial", || {
        uart_tx.extend(device.take_tx());
        uart_tx.windows(3).any(|window| window == b"ls\n")
    });
    assert!(
        r.call("session.console_list", r#"{"selector":"COM0"}"#)
            .get("consoles")
            .and_then(PyValue::as_array)
            .is_some_and(|rows| rows.len() == 2),
        "兩個 socket client 都由 daemon 登記"
    );
}

/// clear 會重建 bridge listener；舊 socket 仍由相同 session hub 服務，新 endpoint 使用不同 ephemeral port。
#[test]
fn telnet_listener_ephemeral_per_bridge() {
    let r = ServiceRig::with_native_tcp_console();
    let response = attach(&r);
    let first_endpoint = text(&response, "endpoint");
    let mut old = connect(&first_endpoint);
    greeting(&mut old);
    wait_for("舊 socket client 已登記", || {
        r.call("session.console_list", r#"{"selector":"COM0"}"#)
            .get("consoles")
            .and_then(PyValue::as_array)
            .is_some_and(|rows| {
                rows.iter().any(|row| match row {
                    PyValue::Dict(row) => row.get("vtty").and_then(PyValue::as_str) == Some(first_endpoint.as_str()),
                    _ => false,
                })
            })
    });

    let cleared = r.call("session.clear", r#"{"selector":"COM0"}"#);
    assert_eq!(cleared.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&cleared));
    let mut next_endpoint = String::new();
    wait_for("clear 後新 bridge listener", || {
        r.clock.advance(Duration::from_millis(250));
        next_endpoint = r
            .session("COM0")
            .and_then(|row| row.get("console_endpoint").and_then(PyValue::as_str).map(str::to_string))
            .unwrap_or_default();
        next_endpoint != first_endpoint && r.session("COM0").is_some_and(|row| {
            row.get("state")
                .and_then(PyValue::as_str)
                .is_some_and(|state| state == "READY" || state == "ATTACHED")
        })
    });
    assert_ne!(first_endpoint, next_endpoint);
    let mut new = connect(&next_endpoint);
    greeting(&mut new);
    wait_for("新 endpoint client 已登記", || {
        r.call("session.console_list", r#"{"selector":"COM0"}"#)
            .get("consoles")
            .and_then(PyValue::as_array)
            .is_some_and(|rows| {
                rows.iter().any(|row| match row {
                    PyValue::Dict(row) => row.get("vtty").and_then(PyValue::as_str) == Some(next_endpoint.as_str()),
                    _ => false,
                })
            })
    });
    assert!(
        r.call("session.console_list", r#"{"selector":"COM0"}"#)
            .get("consoles")
            .and_then(PyValue::as_array)
            .is_some_and(|rows| {
                rows.iter().any(|row| match row {
                    PyValue::Dict(row) => row.get("vtty").and_then(PyValue::as_str) == Some(first_endpoint.as_str()),
                    _ => false,
                })
            }),
        "bridge listener 重建時既有 socket client 必須保留"
    );

}

/// 慢速 client 的出向 ring 維持 1 MiB 上限並記錄丟棄；大量 console RX 不阻塞 agent command。
#[test]
fn telnet_slow_client_bounded() {
    let r = ServiceRig::with_native_tcp_console();
    let response = attach(&r);
    let slow = connect(&text(&response, "endpoint"));
    wait_for("慢速 client 已被 hub accept", || {
        r.call("session.console_list", r#"{"selector":"COM0"}"#)
            .get("consoles")
            .and_then(PyValue::as_array)
            .is_some_and(|rows| !rows.is_empty())
    });
    let device = r.device(REAL_A);
    let chunk = vec![b'x'; 4096];
    for _ in 0..1024 {
        device.push_rx(&chunk);
        std::thread::sleep(Duration::from_millis(1));
    }
    wait_for("慢速 socket 出向 ring 超額後丟棄", || {
        r.console_hub_diagnostics("COM0")
            .iter()
            .any(|(_, displayed, dropped)| *displayed <= 1024 * 1024 && *dropped > 0)
    });

    device.on_tx_reply(b"echo OK\n", b"OK\r\nroot@t:/# ");
    let started = Instant::now();
    let submit = r.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"echo OK","timeout_s":5}"#,
    );
    assert_eq!(submit.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&submit));
    let command_id = text(&submit, "cmd_id");
    let mut command = PyDict::default();
    wait_for("RX 壓力期間 agent 命令完成", || {
        command = r
            .call("command.get", &format!(r#"{{"cmd_id":"{command_id}"}}"#))
            .get("command")
            .and_then(|value| match value {
                PyValue::Dict(command) => Some(command.clone()),
                _ => None,
            })
            .unwrap_or_default();
        command.get("status").and_then(PyValue::as_str) == Some("done")
    });
    assert!(started.elapsed() <= Duration::from_secs(5), "agent command 超過 5 秒");
    let diagnostics = r.console_hub_diagnostics("COM0");
    assert!(diagnostics.iter().any(|(_, displayed, dropped)| *displayed <= 1024 * 1024 && *dropped > 0));
    let _ = slow.shutdown(std::net::Shutdown::Both);
}

/// 唯一允許的原生 serial-console 能力列：daemon 開 COM22，外部 peer 僅開 COM23。
#[test]
#[ignore = "cap:windows_com"]
#[allow(non_snake_case)]
fn cap_windows_com__serial_console_end_to_end() {
    let pair = std::env::var("SW_TEST_CONSOLE_PAIR").expect("需由原生 runner 設定能力 pair");
    assert_eq!(pair, "COM22:COM23", "只允許本項核准的 Console-side pair");
    let r = ServiceRig::with_native_tcp_console_and_serial_console("COM22", "COM23", true);
    let response = attach(&r);
    assert_eq!(response.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&response));

    let mut peer = std::fs::OpenOptions::new()
        .write(true)
        .open(r"\\.\COM23")
        .expect("只開啟核准的 com0com peer COM23");
    peer.write_all(b"x").expect("從 COM23 寫入 console byte");
    let device = r.device(REAL_A);
    let mut uart_tx = Vec::new();
    wait_for("COM23 輸入經 daemon COM22 到 FakeSerial", || {
        uart_tx.extend(device.take_tx());
        uart_tx.contains(&b'x')
    });
    let devices = r.call("device.list", "{}");
    let listing = devices
        .get("devices")
        .map(|value| dumps(value, &RPC_WIRE))
        .unwrap_or_default();
    assert!(!listing.contains("COM22") && !listing.contains("COM23"), "console pair 不列為 DUT：{listing}");

    // COM23 對端持續不讀。大量 DUT RX 讓 COM22 寫入進入 overlapped pending 後，
    // 同一 hub 仍須 accept TCP、收取 TCP 輸入、顯示 UART RX，並能處理 release 關閉。
    let endpoint = text(&response, "endpoint");
    let flood = vec![b'r'; 8192];
    for _ in 0..32 {
        device.push_rx(&flood);
    }
    let mut tcp = connect(&endpoint);
    tcp.set_read_timeout(Some(Duration::from_millis(750)))
        .unwrap();
    let mut greeting_bytes = [0u8; 12];
    tcp.read_exact(&mut greeting_bytes)
        .expect("COM22 寫入 pending 時同一 hub 仍須接受 TCP client");
    assert_eq!(&greeting_bytes, TELNET_GREETING);
    wait_for(
        "TCP client 已由同一 console hub 登記",
        || {
            r.call("session.console_list", r#"{"selector":"COM0"}"#)
                .get("consoles")
                .and_then(PyValue::as_array)
                .is_some_and(|rows| {
                    rows.iter().any(|row| match row {
                        PyValue::Dict(row) => {
                            row.get("vtty").and_then(PyValue::as_str) == Some(endpoint.as_str())
                        }
                        _ => false,
                    })
                })
        },
    );
    tcp.write_all(b"tcp-input-after-com0com-pressure\r\n")
        .expect("送出 TCP line-mode human input");
    let mut tcp_uart_tx = Vec::new();
    wait_for(
        "COM22 peer 不讀時 TCP 輸入仍送到 FakeSerial",
        || {
            tcp_uart_tx.extend(device.take_tx());
            tcp_uart_tx
                .windows(b"tcp-input-after-com0com-pressure".len())
                .any(|window| window == b"tcp-input-after-com0com-pressure")
        },
    );
    device.push_rx(b"tcp-display-after-com0com-pressure");
    let mut displayed = Vec::new();
    wait_for(
        "COM22 peer 不讀時 TCP client 仍收到 UART display",
        || {
            let mut chunk = [0u8; 4096];
            match tcp.read(&mut chunk) {
                Ok(count) => displayed.extend_from_slice(&chunk[..count]),
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                    ) => {}
                Err(error) => panic!("讀取 TCP display：{error}"),
            }
            displayed
                .windows(b"tcp-display-after-com0com-pressure".len())
                .any(|window| window == b"tcp-display-after-com0com-pressure")
        },
    );
    let close_started = Instant::now();
    let released = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(
        released.get("ok"),
        Some(&PyValue::Bool(true)),
        "{}",
        j(&released)
    );
    assert!(
        close_started.elapsed() < Duration::from_secs(3),
        "release 關閉 console hub 不得等待 overlapped COM write"
    );
}
