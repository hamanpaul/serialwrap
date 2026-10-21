#![cfg(feature = "test-fakes")]

use std::ffi::OsString;
use std::time::Duration;

use sw_pal::fakes::{fake_platform, FakeConn, StreamStep};
use sw_pal::traits::{Clock, EndpointSpec, SockTimeout};

#[test]
fn scripted_delay_advances_fake_clock_then_returns_reply() {
    let (platform, _, clock) = fake_platform(vec![FakeConn::stream(vec![
        StreamStep::Delay(Duration::from_millis(2600)),
        StreamStep::Reply(b"pong".to_vec()),
    ])]);
    let endpoint = EndpointSpec::Unix(OsString::from("scripted.sock"));
    let mut stream = platform
        .rpc
        .connect(&endpoint, SockTimeout::Blocking(Duration::from_secs(1)))
        .unwrap();
    let mut buffer = [0u8; 8];

    let count = stream.recv(&mut buffer).unwrap();

    assert_eq!(&buffer[..count], b"pong");
    assert_eq!(
        clock.mono_ns(),
        Duration::from_millis(2600).as_nanos() as u64
    );
}
