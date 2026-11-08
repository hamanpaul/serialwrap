//! `SysEnv` 的平台實作放在 `posix`／`windows` 子模組，依目標平台擇一編譯。
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::process::{Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(unix)]
pub mod posix;
#[cfg(windows)]
pub mod windows;

pub(crate) enum CapturedOutput {
    Exited {
        status: ExitStatus,
        stdout: Vec<u8>,
        stderr: Vec<u8>,
    },
    TimedOut,
}

pub(crate) struct MergedCapturedOutput {
    pub status: Option<ExitStatus>,
    pub output: Vec<u8>,
}

/// 執行子行程並同時排空兩條輸出管線，避免大量輸出填滿 pipe 後造成死鎖。
pub(crate) fn capture_command(
    mut command: Command,
    timeout: Option<Duration>,
) -> io::Result<CapturedOutput> {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn()?;
    let stdout = child.stdout.take().expect("stdout 已設為 piped");
    let stderr = child.stderr.take().expect("stderr 已設為 piped");
    let stdout_reader = start_pipe_reader(stdout);
    let stderr_reader = start_pipe_reader(stderr);
    let deadline = timeout.and_then(|duration| Instant::now().checked_add(duration));

    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) => {
                if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
                    let _ = child.kill();
                    let _ = child.wait();
                    break None;
                }
                thread::sleep(Duration::from_millis(5));
            }
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        }
    };

    let Some(stdout) = receive_pipe(&stdout_reader, deadline)? else {
        return Ok(CapturedOutput::TimedOut);
    };
    let Some(stderr) = receive_pipe(&stderr_reader, deadline)? else {
        return Ok(CapturedOutput::TimedOut);
    };
    match status {
        Some(status) => Ok(CapturedOutput::Exited {
            status,
            stdout,
            stderr,
        }),
        None => Ok(CapturedOutput::TimedOut),
    }
}

/// 將兩個標準輸出指向同一個暫存檔，保留子行程寫入順序且不會填滿 pipe。
pub(crate) fn capture_command_merged(
    mut command: Command,
    timeout: Option<Duration>,
) -> io::Result<MergedCapturedOutput> {
    let (mut output, _temporary) = create_merged_output_file()?;
    command
        .stdout(Stdio::from(output.try_clone()?))
        .stderr(Stdio::from(output.try_clone()?));
    let mut child = command.spawn()?;
    drop(command);
    let deadline = timeout.and_then(|duration| Instant::now().checked_add(duration));

    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) => {
                if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
                    let _ = child.kill();
                    let _ = child.wait();
                    break None;
                }
                thread::sleep(Duration::from_millis(5));
            }
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        }
    };

    output.seek(SeekFrom::Start(0))?;
    let mut bytes = Vec::new();
    output.read_to_end(&mut bytes)?;
    Ok(MergedCapturedOutput {
        status,
        output: bytes,
    })
}

static MERGED_OUTPUT_SEQUENCE: AtomicU64 = AtomicU64::new(0);

struct TemporaryOutput(PathBuf);

impl Drop for TemporaryOutput {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn create_merged_output_file() -> io::Result<(File, TemporaryOutput)> {
    for _ in 0..128 {
        let sequence = MERGED_OUTPUT_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "serialwrap-command-{}-{sequence}.tmp",
            std::process::id()
        ));
        let mut options = OpenOptions::new();
        options.read(true).write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        match options.open(&path) {
            Ok(file) => return Ok((file, TemporaryOutput(path))),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "無法建立唯一的合併輸出暫存檔",
    ))
}

fn read_pipe(mut pipe: impl Read) -> io::Result<Vec<u8>> {
    let mut output = Vec::new();
    pipe.read_to_end(&mut output)?;
    Ok(output)
}

fn start_pipe_reader(pipe: impl Read + Send + 'static) -> Receiver<io::Result<Vec<u8>>> {
    let (sender, receiver) = mpsc::channel();
    let _reader = thread::spawn(move || {
        let _ = sender.send(read_pipe(pipe));
    });
    receiver
}

fn receive_pipe(
    receiver: &Receiver<io::Result<Vec<u8>>>,
    deadline: Option<Instant>,
) -> io::Result<Option<Vec<u8>>> {
    let result = match deadline {
        Some(deadline) => {
            let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                return Ok(None);
            };
            match receiver.recv_timeout(remaining) {
                Ok(result) => result?,
                Err(RecvTimeoutError::Timeout) => return Ok(None),
                Err(RecvTimeoutError::Disconnected) => {
                    return Err(io::Error::new(
                        io::ErrorKind::BrokenPipe,
                        "子行程輸出讀取執行緒異常結束",
                    ));
                }
            }
        }
        None => receiver.recv().map_err(|_| {
            io::Error::new(io::ErrorKind::BrokenPipe, "子行程輸出讀取執行緒異常結束")
        })??,
    };
    Ok(Some(result))
}

pub(crate) fn universal_newlines(text: String) -> String {
    text.replace("\r\n", "\n").replace('\r', "\n")
}

#[cfg(unix)]
pub(crate) fn exit_code(status: ExitStatus) -> i32 {
    use std::os::unix::process::ExitStatusExt;
    status
        .code()
        .or_else(|| status.signal().map(|signal| -signal))
        .unwrap_or(-1)
}

#[cfg(windows)]
pub(crate) fn exit_code(status: ExitStatus) -> i32 {
    status.code().unwrap_or(-1)
}
