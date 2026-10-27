//! S1-W WAL 寫入微基準的 Rust 量測器（P5-T22，規格 §1.1.2、§1.1.3、§1.2）。
//!
//! 以 P1 T19 `scripts/stage0/wal_microbench.py` 凍結的口徑驅動 Task 7 `WalWriter`＋Task 6 原生 `WalFs`：
//! - 預設四流 × 10,000 筆 × 1,024 B、rotate 門檻 `R = 64 MiB`（`constants.py` `DEFAULT_WAL_ROTATE_BYTES`）、
//!   每流預填合法紀錄至 `60/64 × R`、`--rate 0`（不節流；正式量測由 manifest 指定）。
//! - 資料混合（ANSI 20%／文字 60%／prompt 20%）與 seed 規則逐位元組對齊 Python：每筆以
//!   `random.Random(seed)`（MT19937、`init_by_array`）產生，正式流 seed＝`stream*1000000+idx`、
//!   預填 seed＝`stream*500000+fill_idx`、預填片段 `min(payload, 2048)` bytes。
//! - 每筆 append 以單調時鐘計時；當筆 append 前後歸檔數（`raw.wal.ndjson.*`）增加即計入 rotate 停頓、
//!   不計入一般寫入分位數（§1.2「不含 rotate 當筆」）；分位數取排序後 `samples[int(n*q)]`（與 Python 同）。
//!
//! 只量測、不改 WAL 行為。門檻逐字取規格 §1.2，超標即 `verdict: FAIL` 並保留全部 rotate 樣本，
//! 不得改列觀察值。Windows 原生與 EDR 開啟下的正式量測屬 BO-S21-14（擁有者證據）。
//!
//! 用法：`cargo run --release -p sw-core --example wal_s1w -- --out <path.json> [--streams N]
//! [--records N] [--payload N] [--rotate-bytes N] [--prefill-ratio F] [--rate N] [--wal-root DIR]`
//! 結束碼：0 量測完成（不論 PASS／FAIL，判定見輸出 `verdict`）；2 參數或環境錯誤。

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Barrier};
use std::time::Instant;

use sw_core::wal::{WalAppendReq, WalConfig, WalWriter};
use sw_pal::BackendKind;
use sw_proto::pyjson::{DumpOpts, PyDict, PyInt, PyValue, Sep, dumps_dict};

const MIX_ANSI_RATIO: f64 = 0.20;
const MIX_TEXT_RATIO: f64 = 0.60;
const MIX_PROMPT_RATIO: f64 = 0.20;

const SAMPLE_ANSI: &[u8] =
    b"\x1b[32m[  OK  ]\x1b[0m Started Serialwrap UART multiplexer service.\r\n\
\x1b[1;33m[ WARN ]\x1b[0m Buffer occupancy reached 85% watermark.\r\n\
\x1b[31;1m[ FAIL ]\x1b[0m Hardware handshake CTS asserted unexpectedly.\r\n";
const SAMPLE_TEXT: &[u8] = b"[ 123.456789] serialwrap: session COM0 baud 115200 8N1 rx_fifo=4096\n\
[ 123.460012] kernel: eth0: link up, 1000Mbps, full duplex, lpa 0xCDE1\n\
[ 123.471203] systemd[1]: Reached target Multi-User System.\n\
[ 123.489110] daemon: wal writer sync completed in 1240 ns\n";
const SAMPLE_PROMPT: &[u8] = b"root@DUT-Router:/# CFE> uboot# login: Password: ";

/// 規格 §1.2 門檻（毫秒）：(p50, p95, p99, rotate 最大停頓)。
const LINUX_LIMITS_MS: (f64, f64, f64, f64) = (0.005, 0.01, 0.02, 3.0);
const WINDOWS_LIMITS_MS: (f64, f64, f64, f64) = (0.03, 0.08, 0.20, 15.0);

/// CPython `random.Random`（MT19937）的最小移植，只承接 `seed(int)` 與 `random()`。
struct PyRandom {
    mt: [u32; 624],
    index: usize,
}

impl PyRandom {
    fn new(seed: u64) -> PyRandom {
        // `random.seed(int)`：key 為 |seed| 的 32-bit little-endian 字組；seed 為 0 時 key=[0]。
        let mut key: Vec<u32> = Vec::new();
        let mut n = seed;
        while n > 0 {
            key.push((n & 0xffff_ffff) as u32);
            n >>= 32;
        }
        if key.is_empty() {
            key.push(0);
        }
        let mut r = PyRandom {
            mt: [0; 624],
            index: 624,
        };
        r.init_genrand(19_650_218);
        let (mut i, mut j) = (1usize, 0usize);
        let mut k = 624usize.max(key.len());
        while k > 0 {
            let prev = r.mt[i - 1];
            r.mt[i] = (r.mt[i] ^ (prev ^ (prev >> 30)).wrapping_mul(1_664_525))
                .wrapping_add(key[j])
                .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= 624 {
                r.mt[0] = r.mt[623];
                i = 1;
            }
            if j >= key.len() {
                j = 0;
            }
            k -= 1;
        }
        k = 623;
        while k > 0 {
            let prev = r.mt[i - 1];
            r.mt[i] = (r.mt[i] ^ (prev ^ (prev >> 30)).wrapping_mul(1_566_083_941))
                .wrapping_sub(i as u32);
            i += 1;
            if i >= 624 {
                r.mt[0] = r.mt[623];
                i = 1;
            }
            k -= 1;
        }
        r.mt[0] = 0x8000_0000;
        r
    }

    fn init_genrand(&mut self, s: u32) {
        self.mt[0] = s;
        for i in 1..624 {
            let prev = self.mt[i - 1];
            self.mt[i] = 1_812_433_253u32
                .wrapping_mul(prev ^ (prev >> 30))
                .wrapping_add(i as u32);
        }
        self.index = 624;
    }

    fn genrand_u32(&mut self) -> u32 {
        if self.index >= 624 {
            for kk in 0..624 {
                let y = (self.mt[kk] & 0x8000_0000) | (self.mt[(kk + 1) % 624] & 0x7fff_ffff);
                let mut v = self.mt[(kk + 397) % 624] ^ (y >> 1);
                if y & 1 != 0 {
                    v ^= 0x9908_b0df;
                }
                self.mt[kk] = v;
            }
            self.index = 0;
        }
        let mut y = self.mt[self.index];
        self.index += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c_5680;
        y ^= (y << 15) & 0xefc6_0000;
        y ^= y >> 18;
        y
    }

    fn random(&mut self) -> f64 {
        let a = (self.genrand_u32() >> 5) as f64;
        let b = (self.genrand_u32() >> 6) as f64;
        (a * 67_108_864.0 + b) * (1.0 / 9_007_199_254_740_992.0)
    }
}

/// 與 `wal_microbench.generate_payload` 逐位元組相同。
fn generate_payload(size: usize, seed: u64) -> Vec<u8> {
    let mut rng = PyRandom::new(seed);
    let mut out: Vec<u8> = Vec::with_capacity(size + 256);
    while out.len() < size {
        let choice = rng.random();
        let chunk = if choice < MIX_ANSI_RATIO {
            SAMPLE_ANSI
        } else if choice < MIX_ANSI_RATIO + MIX_TEXT_RATIO {
            SAMPLE_TEXT
        } else {
            SAMPLE_PROMPT
        };
        out.extend_from_slice(chunk);
    }
    out.truncate(size);
    out
}

struct Args {
    streams: usize,
    records: usize,
    payload: usize,
    rotate_bytes: u64,
    prefill_ratio: f64,
    rate: u64,
    out: PathBuf,
    wal_root: Option<PathBuf>,
    dump_payload_seed: Option<u64>,
}

fn parse_args() -> Result<Args, String> {
    let mut a = Args {
        streams: 4,
        records: 10_000,
        payload: 1024,
        rotate_bytes: 64 * 1024 * 1024,
        prefill_ratio: 0.9375,
        rate: 0,
        out: PathBuf::new(),
        wal_root: None,
        dump_payload_seed: None,
    };
    let mut have_out = false;
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < argv.len() {
        let flag = argv[i].as_str();
        let val = argv
            .get(i + 1)
            .cloned()
            .ok_or_else(|| format!("{flag} 缺少參數值"))?;
        let bad = |e: &dyn std::fmt::Display| format!("{flag} 參數值無效：{val}（{e}）");
        match flag {
            "--streams" => a.streams = val.parse().map_err(|e| bad(&e))?,
            "--records" => a.records = val.parse().map_err(|e| bad(&e))?,
            "--payload" => a.payload = val.parse().map_err(|e| bad(&e))?,
            "--rotate-bytes" => a.rotate_bytes = val.parse().map_err(|e| bad(&e))?,
            "--prefill-ratio" => a.prefill_ratio = val.parse().map_err(|e| bad(&e))?,
            "--rate" => a.rate = val.parse().map_err(|e| bad(&e))?,
            "--out" => {
                a.out = PathBuf::from(&val);
                have_out = true;
            }
            "--wal-root" => a.wal_root = Some(PathBuf::from(&val)),
            // 口徑自我核對：輸出指定 seed 的 payload（hex）後結束，供與 Python 逐位元組比對。
            "--dump-payload-seed" => a.dump_payload_seed = Some(val.parse().map_err(|e| bad(&e))?),
            _ => return Err(format!("未知參數：{flag}")),
        }
        i += 2;
    }
    if a.dump_payload_seed.is_some() {
        return Ok(a);
    }
    if !have_out {
        return Err("缺少 --out".to_string());
    }
    if a.streams == 0 || a.records == 0 || a.payload == 0 || a.rotate_bytes == 0 {
        return Err("--streams／--records／--payload／--rotate-bytes 必須為正數".to_string());
    }
    if !(a.prefill_ratio.is_finite() && (0.0..1.0).contains(&a.prefill_ratio)) {
        return Err("--prefill-ratio 必須介於 [0, 1)".to_string());
    }
    Ok(a)
}

fn archive_count(dir: &Path) -> usize {
    std::fs::read_dir(dir)
        .map(|it| {
            it.filter_map(Result::ok)
                .filter(|e| {
                    e.file_name()
                        .to_string_lossy()
                        .starts_with("raw.wal.ndjson.")
                })
                .count()
        })
        .unwrap_or(0)
}

fn file_size(path: &Path) -> u64 {
    std::fs::metadata(path).map(|m| m.len()).unwrap_or(0)
}

fn int(v: u64) -> PyValue {
    PyValue::Int(PyInt::Small(v as i128))
}

fn text(s: impl Into<String>) -> PyValue {
    PyValue::Str(s.into())
}

fn first_line_after(path: &str, prefix: &str) -> Option<String> {
    let body = std::fs::read_to_string(path).ok()?;
    body.lines()
        .find(|l| l.starts_with(prefix))
        .and_then(|l| l.split_once(':'))
        .map(|(_, v)| v.trim().to_string())
}

fn run_cmd(prog: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(prog).args(args).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (!s.is_empty()).then_some(s)
}

/// 規格 §1.1.1 量測環境欄位；查不到者一律 `unknown`（不得填 0 或空字串）。
fn collect_env(backend: BackendKind, wal_root: &Path) -> PyDict {
    let unknown = || "unknown".to_string();
    let os = match backend {
        BackendKind::Posix => "posix",
        BackendKind::Win => "windows",
    };
    let kernel = std::fs::read_to_string("/proc/sys/kernel/osrelease")
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|_| unknown());
    let proc_version = std::fs::read_to_string("/proc/version")
        .unwrap_or_default()
        .to_lowercase();
    let virtualization = if matches!(backend, BackendKind::Win) {
        "native".to_string()
    } else if proc_version.contains("microsoft") || proc_version.contains("wsl") {
        "wsl2".to_string()
    } else {
        match run_cmd("systemd-detect-virt", &[]) {
            Some(v) if v == "none" => "native".to_string(),
            Some(v) => format!("vm:{v}"),
            None => unknown(),
        }
    };
    let cpu = first_line_after("/proc/cpuinfo", "model name").unwrap_or_else(unknown);
    let mem = first_line_after("/proc/meminfo", "MemTotal")
        .and_then(|v| v.split_whitespace().next()?.parse::<u64>().ok())
        .map(|kb| int(kb * 1024))
        .unwrap_or_else(|| text("unknown"));
    let (storage, fs_type) = run_cmd("df", &["-T", &wal_root.to_string_lossy()])
        .and_then(|s| {
            let line = s.lines().nth(1)?.to_string();
            let mut parts = line.split_whitespace();
            Some((parts.next()?.to_string(), parts.next()?.to_string()))
        })
        .unwrap_or_else(|| (unknown(), unknown()));
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let repo_s = repo.to_string_lossy().to_string();
    let git_rev = run_cmd("git", &["-C", &repo_s, "rev-parse", "HEAD"]).unwrap_or_else(unknown);
    let contract_tree =
        run_cmd("git", &["-C", &repo_s, "rev-parse", "HEAD:contracts"]).unwrap_or_else(unknown);
    let tool_src = Path::new(env!("CARGO_MANIFEST_DIR")).join("examples/wal_s1w.rs");
    let tool_revision = run_cmd(
        "git",
        &["-C", &repo_s, "hash-object", &tool_src.to_string_lossy()],
    )
    .unwrap_or_else(unknown);
    let cpu_count = std::thread::available_parallelism()
        .map(|n| int(n.get() as u64))
        .unwrap_or_else(|_| text("unknown"));
    let mut mix = PyDict::new();
    mix.insert("ansi", PyValue::Float(MIX_ANSI_RATIO));
    mix.insert("text", PyValue::Float(MIX_TEXT_RATIO));
    mix.insert("prompt", PyValue::Float(MIX_PROMPT_RATIO));
    let mut env = PyDict::new();
    env.insert("os", text(os));
    env.insert("os_build", text(kernel));
    env.insert("virtualization", text(virtualization));
    env.insert("cpu", text(cpu));
    env.insert("cpu_count", cpu_count);
    env.insert("memory_bytes", mem);
    env.insert("storage_device", text(storage));
    env.insert("fs_type", text(fs_type));
    env.insert("power", text("unknown"));
    env.insert("endpoint_av", text("unknown"));
    env.insert("implementation", text("rust"));
    env.insert("wal_writer", text("sw_core::wal::WalWriter"));
    env.insert("wal_fs", text("sw_pal::native().wal_fs"));
    env.insert("git_revision", text(git_rev));
    env.insert("contract_tree", text(contract_tree));
    env.insert("tool_revision", text(tool_revision));
    env.insert("timing_method", text("std::time::Instant"));
    env.insert("serial", text("none"));
    env.insert("mix", PyValue::Dict(mix));
    env
}

struct StreamResult {
    ordinary_ns: Vec<u64>,
    rotate_ns: Vec<u64>,
    rotations: usize,
}

fn percentile(sorted: &[u64], q: f64) -> u64 {
    if sorted.is_empty() {
        return 0;
    }
    let idx = ((sorted.len() as f64) * q) as usize;
    sorted[idx.min(sorted.len() - 1)]
}

fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(msg) => {
            eprintln!("wal_s1w: {msg}");
            std::process::exit(2);
        }
    };
    if let Some(seed) = args.dump_payload_seed {
        let p = generate_payload(args.payload, seed);
        let hex: String = p.iter().map(|b| format!("{b:02x}")).collect();
        println!("{hex}");
        return;
    }
    let tmp_root;
    let wal_root = match &args.wal_root {
        Some(p) => p.clone(),
        None => {
            tmp_root = std::env::temp_dir().join(format!("wal_s1w-{}", std::process::id()));
            tmp_root.clone()
        }
    };
    if let Err(e) = std::fs::create_dir_all(&wal_root) {
        eprintln!("wal_s1w: 無法建立 WAL 根目錄 {}：{e}", wal_root.display());
        std::process::exit(2);
    }
    let platform = sw_pal::native();
    let backend = platform.caps.native_backend;

    // 預填（§1.1.3：以合法 WAL 紀錄預填至 prefill_ratio × R，保存實際起始大小）。
    let target_prefill = (args.prefill_ratio * args.rotate_bytes as f64) as u64;
    let fill_chunk = args.payload.min(2048);
    let mut writers: Vec<(WalWriter, PathBuf)> = Vec::new();
    let mut prefill_bytes: Vec<PyValue> = Vec::new();
    for i in 0..args.streams {
        let dir = wal_root.join(format!("s{i}"));
        let w = WalWriter::open(
            WalConfig {
                dir: dir.clone(),
                rotate_bytes: args.rotate_bytes,
            },
            Arc::clone(&platform.wal_fs),
            Arc::clone(&platform.clock),
        );
        let wal_path = w.wal_path();
        let com = format!("COM{i}");
        let mut fill_idx: u64 = 0;
        while file_size(&wal_path) < target_prefill {
            let data = generate_payload(fill_chunk, i as u64 * 500_000 + fill_idx);
            let r = w.append(WalAppendReq {
                com: &com,
                dir: "RX",
                source: "prefill",
                payload: &data,
                cmd_id: None,
                loss_flag: false,
                meta: None,
            });
            if !r.ok {
                eprintln!("wal_s1w: 預填寫入失敗（stream {i}）");
                std::process::exit(2);
            }
            fill_idx += 1;
        }
        prefill_bytes.push(int(file_size(&wal_path)));
        writers.push((w, dir));
    }

    let barrier = Arc::new(Barrier::new(args.streams));
    let mut handles = Vec::new();
    for (i, (w, dir)) in writers.into_iter().enumerate() {
        let barrier = Arc::clone(&barrier);
        let (records, payload_len, rate) = (args.records, args.payload, args.rate);
        handles.push(std::thread::spawn(move || {
            let com = format!("COM{i}");
            let mut res = StreamResult {
                ordinary_ns: Vec::with_capacity(records),
                rotate_ns: Vec::new(),
                rotations: 0,
            };
            let mut write_failures = 0u64;
            barrier.wait();
            let mut last_archives = archive_count(&dir);
            for idx in 0..records {
                let data = generate_payload(payload_len, i as u64 * 1_000_000 + idx as u64);
                let t0 = Instant::now();
                let r = w.append(WalAppendReq {
                    com: &com,
                    dir: "RX",
                    source: "bench",
                    payload: &data,
                    cmd_id: None,
                    loss_flag: false,
                    meta: None,
                });
                let elapsed = t0.elapsed().as_nanos() as u64;
                if !r.ok {
                    write_failures += 1;
                }
                let now_archives = archive_count(&dir);
                if now_archives > last_archives {
                    res.rotate_ns.push(elapsed);
                    res.rotations += now_archives - last_archives;
                    last_archives = now_archives;
                } else {
                    res.ordinary_ns.push(elapsed);
                }
                if rate > 0 {
                    let target_ns = (payload_len as f64 / rate as f64 * 1e9) as u64;
                    if target_ns > elapsed {
                        std::thread::sleep(std::time::Duration::from_nanos(target_ns - elapsed));
                    }
                }
            }
            let _ = w.shutdown();
            (res, write_failures)
        }));
    }
    let mut results = Vec::new();
    let mut write_failures = 0u64;
    for h in handles {
        match h.join() {
            Ok((r, wf)) => {
                write_failures += wf;
                results.push(r);
            }
            Err(_) => {
                eprintln!("wal_s1w: 寫入執行緒 panic");
                std::process::exit(2);
            }
        }
    }

    let mut ordinary: Vec<u64> = results
        .iter()
        .flat_map(|r| r.ordinary_ns.iter().copied())
        .collect();
    let rotates: Vec<u64> = results
        .iter()
        .flat_map(|r| r.rotate_ns.iter().copied())
        .collect();
    let rotations_per_stream: Vec<usize> = results.iter().map(|r| r.rotations).collect();
    ordinary.sort_unstable();
    let (p50, p95, p99) = (
        percentile(&ordinary, 0.50),
        percentile(&ordinary, 0.95),
        percentile(&ordinary, 0.99),
    );
    let rotate_max = rotates.iter().copied().max().unwrap_or(0);

    let (limits, limits_name) = match backend {
        BackendKind::Posix => (LINUX_LIMITS_MS, "linux"),
        BackendKind::Win => (WINDOWS_LIMITS_MS, "windows"),
    };
    let ms = |ns: u64| ns as f64 / 1_000_000.0;
    let mut failures: Vec<PyValue> = Vec::new();
    let mut check = |name: &str, got_ns: u64, limit_ms: f64| {
        if ms(got_ns) > limit_ms {
            failures.push(text(format!("{name} {:.6} ms > {limit_ms} ms", ms(got_ns))));
        }
    };
    check("append_p50", p50, limits.0);
    check("append_p95", p95, limits.1);
    check("append_p99", p99, limits.2);
    check("rotate_max", rotate_max, limits.3);
    if rotations_per_stream.iter().any(|&n| n == 0) {
        failures.push(text("至少一流未跨越 rotate（§1.1.3 要求每流至少一次）"));
    }
    if write_failures > 0 {
        failures.push(text(format!("write_failures={write_failures}")));
    }
    let verdict = if failures.is_empty() { "PASS" } else { "FAIL" };

    let mut append_ns = PyDict::new();
    append_ns.insert("p50", int(p50));
    append_ns.insert("p95", int(p95));
    append_ns.insert("p99", int(p99));
    append_ns.insert("n", int(ordinary.len() as u64));
    let mut append_ms = PyDict::new();
    append_ms.insert("p50", PyValue::Float(ms(p50)));
    append_ms.insert("p95", PyValue::Float(ms(p95)));
    append_ms.insert("p99", PyValue::Float(ms(p99)));
    let mut thresholds = PyDict::new();
    thresholds.insert("platform", text(limits_name));
    thresholds.insert("source", text("serialwrap-rust-spec-final.md §1.2"));
    thresholds.insert("append_p50_ms", PyValue::Float(limits.0));
    thresholds.insert("append_p95_ms", PyValue::Float(limits.1));
    thresholds.insert("append_p99_ms", PyValue::Float(limits.2));
    thresholds.insert("rotate_max_ms", PyValue::Float(limits.3));
    let mut params = PyDict::new();
    params.insert("streams", int(args.streams as u64));
    params.insert("records", int(args.records as u64));
    params.insert("payload", int(args.payload as u64));
    params.insert("rotate_bytes", int(args.rotate_bytes));
    params.insert("prefill_ratio", PyValue::Float(args.prefill_ratio));
    params.insert("rate", int(args.rate));

    let mut out = PyDict::new();
    out.insert("schema", text("wal_s1w.v1"));
    out.insert("env", PyValue::Dict(collect_env(backend, &wal_root)));
    out.insert("params", PyValue::Dict(params));
    out.insert("rotate_bytes", int(args.rotate_bytes));
    out.insert("prefill_bytes", PyValue::List(prefill_bytes));
    out.insert("total_records", int((args.streams * args.records) as u64));
    out.insert("append_ns", PyValue::Dict(append_ns));
    out.insert("append_ms", PyValue::Dict(append_ms));
    out.insert("ordinary_samples", int(ordinary.len() as u64));
    out.insert("rotate_samples", int(rotates.len() as u64));
    out.insert(
        "rotate_pauses_ns",
        PyValue::List(rotates.iter().map(|&n| int(n)).collect()),
    );
    out.insert("rotate_max_ns", int(rotate_max));
    out.insert("rotate_max_ms", PyValue::Float(ms(rotate_max)));
    out.insert(
        "rotations_per_stream",
        PyValue::List(
            rotations_per_stream
                .iter()
                .map(|&n| int(n as u64))
                .collect(),
        ),
    );
    out.insert("write_failures", int(write_failures));
    out.insert("thresholds", PyValue::Dict(thresholds));
    out.insert("failures", PyValue::List(failures));
    out.insert("verdict", text(verdict));

    let opts = DumpOpts {
        sep: Sep::Indent(2),
        ensure_ascii: false,
        sort_keys: true,
        allow_nan: true,
    };
    let body = dumps_dict(&out, &opts) + "\n";
    if let Some(parent) = args.out.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Err(e) = std::fs::write(&args.out, body) {
        eprintln!("wal_s1w: 無法寫入 {}：{e}", args.out.display());
        std::process::exit(2);
    }
    if args.wal_root.is_none() {
        let _ = std::fs::remove_dir_all(&wal_root);
    }
    println!(
        "wal_s1w: verdict={verdict} p50={:.6}ms p95={:.6}ms p99={:.6}ms rotate_max={:.3}ms rotations={:?}",
        ms(p50),
        ms(p95),
        ms(p99),
        ms(rotate_max),
        rotations_per_stream
    );
}
