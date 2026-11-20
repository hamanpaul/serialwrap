# WAL rotate 停頓根因與修正（P5 第 6 輪；rulings/p5.md OD-61 WSL2 實測 High）

- 主機：cloud Linux 容器（4 核、ext4／virtio，`env.virtualization=vm:docker`）——只作診斷（OD-61）；正式數字須由編排者於擁有者 WSL2 主機重測。
- 工具：`cargo run --release -p sw-core --example wal_s1w`，預設口徑（4 流 × 10,000 筆 × 1,024 B、R=64 MiB、預填 60/64、不節流）；非 root。
- `base*`：`289f7573` 乾淨樹（修正前）。`sync*`：同上，但臨時在預填後、量測前呼叫一次 `sync(2)`（根因實驗，程式未提交）。
  `fix*`：`289f7573`＋本 commit 的 `sw-pal` 修正（量測時工作樹含該未提交變更，故 `env.git_revision` 顯示 `289f7573`）。
- Python 基線：`scripts/stage0/wal_microbench.py` 同口徑一輪（同主機，診斷）：rotate 停頓 221.3, 289.8, 330.2, 360.4 ms。

| 輪 | append p50 ms | p95 ms | p99 ms | rotate max ms |
|---|---:|---:|---:|---:|
| base1 | 0.0238 | 0.0408 | 0.0621 | 440.0 |
| base2 | 0.0236 | 0.0400 | 0.0593 | 367.6 |
| sync1 | 0.0300 | 0.0421 | 0.0641 | 24.2 |
| sync2 | 0.0309 | 0.0419 | 0.0639 | 21.6 |
| fix1 | 0.0258 | 0.0432 | 0.0760 | 64.9 |
| fix2 | 0.0246 | 0.0444 | 0.0832 | 65.2 |
| fix3 | 0.0236 | 0.0407 | 0.0638 | 66.4 |

根因：rotate 的 `close(sync)` 須 fsync 整個檔案仍在 page cache 的髒頁。預填 60 MiB 後立即量測，Rust 的 append 比 Python 快約 47 倍，
輪替時幾乎整個 64 MiB 都未回寫，四流同時 fsync（WSL2 約 855 ms、cloud 368～440 ms）；Python 寫得慢，背景回寫早已完成大半。
臨時於預填後 `sync(2)` 即降到 22～24 ms，證實停頓來自髒頁量而非跨流鎖。

修正：`sw_pal::wal_fs::posix` 寫入路徑每 1 MiB 以 `sync_file_range(SYNC_FILE_RANGE_WRITE)` 啟動非同步回寫（Linux；不等待、
不改持久性語意——append 仍不 fsync、rotate 前仍 fsync）。cloud 上 rotate max 降至 65～66 ms，低於同主機 Python 基線；append p50 不變。
