#[cfg(unix)]
fn main() {
    use std::{
        io::{self, Write},
        process::{Command, Stdio},
        thread,
        time::{Duration, Instant},
    };

    if std::env::var_os("S21_SIGNAL_CHILD").is_some() {
        // install() 是此子行程的第一個執行緒相關操作。
        let signals = sw_pal::native().signals;
        signals.install().expect("安裝停止訊號等待器");
        signals.install().expect("重複安裝應冪等");

        let (release_tx, release_rx) = crossbeam_channel::unbounded();
        let workers: Vec<_> = (0..4)
            .map(|_| {
                let release_rx = release_rx.clone();
                thread::spawn(move || release_rx.recv().unwrap())
            })
            .collect();
        println!("READY");
        io::stdout().flush().unwrap();
        let (_extra_tx, extra_rx) = crossbeam_channel::unbounded();
        signals.wait_stop(extra_rx);
        for _ in &workers {
            release_tx.send(()).unwrap();
        }
        for worker in workers {
            worker.join().unwrap();
        }
        return;
    }

    let executable = std::env::current_exe().expect("取得測試 helper 路徑");
    let mut child = Command::new(executable)
        .env("S21_SIGNAL_CHILD", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("啟動訊號子行程");
    let stdout = child.stdout.take().unwrap();
    let (ready_tx, ready_rx) = crossbeam_channel::bounded(1);
    thread::spawn(move || {
        use std::io::BufRead;
        let mut reader = io::BufReader::new(stdout);
        let mut line = String::new();
        let ready = loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) => break false,
                Ok(_) if line.contains("READY") => break true,
                Ok(_) => {}
                Err(_) => break false,
            }
        };
        let _ = ready_tx.send(ready);
    });

    let ready = ready_rx
        .recv_timeout(Duration::from_secs(5))
        .unwrap_or(false);
    if !ready {
        let _ = child.kill();
        let _ = child.wait();
        panic!("子行程未於 5 秒內啟動等待器並保留四個 worker");
    }
    // SAFETY: PID 來自本 helper 剛建立且仍持有的子行程。
    if unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) } != 0 {
        let _ = child.kill();
        let _ = child.wait();
        panic!("送出 SIGTERM 失敗");
    }

    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = child.try_wait().expect("檢查訊號子行程") {
            assert!(status.success(), "子行程應正常處理 SIGTERM：{status}");
            return;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("子行程未於 5 秒內完成停止訊號處理，已終止並回收");
        }
        thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(windows)]
fn main() {
    let signals = sw_pal::native().signals;
    signals.install().expect("安裝 Windows 停止訊號等待器");
    signals.install().expect("重複安裝應冪等");
}

#[cfg(not(any(unix, windows)))]
fn main() {}
