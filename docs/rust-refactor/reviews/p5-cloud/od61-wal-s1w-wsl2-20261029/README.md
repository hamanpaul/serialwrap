# OD-61：§1.2 WAL 寫入效能於擁有者 WSL2 主機實測（2026-10-29）

- 依據：擁有者裁決 OD-61（rulings/p5.md）。主機 AMD Ryzen 9 9900X（24 執行緒）、WSL2 kernel 6.18.54.1、ext4（/dev/sdd）。
- 安靜時段：與同機 thala M5 協調停派工（15:59～16:02），量測期間 loadavg 1 分鐘值 1.85～2.76（常駐 cortex manager_daemon 約 0.85 核、hippo dream 等背景程序未停）；逐輪 loadavg 見 `host.txt`。
- 受測：`feature/rust-refactor-p5` @ `110bd58` 的 `cargo build --release -p sw-core --example wal_s1w`，預設口徑（4 流 × 10,000 筆 × 1,024 B、R=64 MiB、預填 60/64、不節流），連跑三輪；三輪全 PASS 才算 PASS，結果全數保留。
- 對照：同主機、同口徑的 Python 基線 `scripts/stage0/wal_microbench.py`（0.3.2）一輪（診斷用）。

| 項目 | §1.2 Linux 門檻 | Rust 輪 1 | Rust 輪 2 | Rust 輪 3 | Python 基線 |
|---|---:|---:|---:|---:|---:|
| append p50 | 0.005 ms | 0.0217 | 0.0220 | 0.0216 | 1.031 |
| append p95 | 0.010 ms | 0.0280 | 0.0322 | 0.0290 | 2.286 |
| append p99 | 0.020 ms | 0.0459 | 0.0501 | 0.0483 | 3.210 |
| rotate 最大停頓 | 3 ms | 856.7 | 859.0 | 849.2 | 132.2 |
| 判定 | | FAIL | FAIL | FAIL | （未判定） |

- 每輪 4 條流各 rotate 1 次，Rust 四流停頓皆約 855 ms 且同時發生（Python 108～132 ms），疑似 rotate 期間持有跨流共用鎖或同步大量 I/O。
- 原始輸出：`wal_s1w-{1,2,3}.json`、`python-baseline-wal_microbench.json`、`host.txt`（家目錄路徑已改寫為 `~`）。

## 編排者裁決

1. **rotate 停頓（High，交 P5）**：Rust rotate 最大停頓約為 Python 基線的 6.5 倍，屬效能回歸；P5 須查明根因（先以可重現的微基準或 L1 計時測試紅燈），修正後至少不劣於同主機 Python 基線，並以同一工具重測。
2. **append 門檻**：Rust 已比 Python 基線快約 47 倍，但仍約為 §1.2 門檻 4 倍；門檻是否適用於 WSL2 屬擁有者裁決，待 rotate 修正後再約一次安靜時段重測，連同新數字提交擁有者。
3. gate 的 `wal_s1w_perf` 依本次結果照實記 FAIL，不得改列觀察值。
