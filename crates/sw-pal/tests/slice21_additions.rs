use std::time::{Duration, SystemTime, UNIX_EPOCH};

use sw_pal::{Clock, RpcServerStream};

#[cfg(unix)]
use sw_pal::clock::posix::NativeClock;
#[cfg(windows)]
use sw_pal::clock::windows::NativeClock;

#[test]
fn native_clock_wall_time_tracks_system_clock() {
    let before = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_micros() as i64;
    let actual = NativeClock.wall_unix_us();
    let after = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_micros() as i64;
    assert!((before..=after).contains(&actual));
}

#[test]
fn native_clock_local_offset_is_bounded() {
    let offset = NativeClock.local_utc_offset_s();
    assert!((-86_400..=86_400).contains(&offset));
}

#[test]
fn native_clock_sleep_advances_monotonic_clock() {
    let before = NativeClock.mono_ns();
    NativeClock.sleep(Duration::from_millis(2));
    assert!(NativeClock.mono_ns() > before);
}

#[cfg(unix)]
#[test]
fn unix_stream_implements_rpc_server_stream() {
    use std::os::unix::net::UnixStream;

    let (mut server, mut client) = UnixStream::pair().unwrap();
    let stream: &mut dyn RpcServerStream = &mut server;
    stream
        .set_read_timeout(Some(Duration::from_millis(50)))
        .unwrap();
    stream.write_all(b"rpc").unwrap();
    let mut received = [0; 3];
    std::io::Read::read_exact(&mut client, &mut received).unwrap();
    assert_eq!(&received, b"rpc");
    stream.shutdown_read().unwrap();
}

#[test]
fn tcp_stream_implements_rpc_server_stream() {
    use std::net::{TcpListener, TcpStream};

    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let mut client = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
    let (mut server, _) = listener.accept().unwrap();
    let stream: &mut dyn RpcServerStream = &mut server;
    stream
        .set_read_timeout(Some(Duration::from_millis(50)))
        .unwrap();
    stream.write_all(b"rpc").unwrap();
    let mut received = [0; 3];
    std::io::Read::read_exact(&mut client, &mut received).unwrap();
    assert_eq!(&received, b"rpc");
    stream.shutdown_read().unwrap();
}

#[cfg(feature = "test-fakes")]
#[test]
fn fake_clock_exposes_t0c_wall_values() {
    let fake = sw_pal::fakes::FakeClock::default();
    assert_eq!(fake.wall_unix_us(), 0);
    assert_eq!(fake.local_utc_offset_s(), 0);
}
