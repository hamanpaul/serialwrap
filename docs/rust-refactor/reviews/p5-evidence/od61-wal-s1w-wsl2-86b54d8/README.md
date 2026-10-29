# OD-61：§1.2 WAL 寫入效能 WSL2 重測（P5 @ 86b54d8，2026-10-29 21:22）

- 主機同前次（AMD Ryzen 9 9900X、WSL2 6.18.54.1、ext4）；與 thala M5 協調安靜時段 21:22～21:24，loadavg 1 分鐘值 3.11～3.95（逐輪見 `host.txt`）。
- 受測：`feature/rust-refactor-p5` @ `86b54d8`（含 `ba8bf0f` 每 1 MiB `sync_file_range` 非同步回寫），乾淨工作樹（`source_tree_dirty: false`），`cargo build --release -p sw-core --example wal_s1w`，預設口徑連跑三輪。對照：同時段 Python 0.3.2 `scripts/stage0/wal_microbench.py` 一輪。
- gate 匯入核對：`python3 scripts/rust/slice21_gate_judge.py wal-s1w-verdict --import wal_s1w-N.json --host-label owner-wsl2-ext4-vhdx` 三份皆可匯入（判定依 §1.2 門檻為 FAIL）。

| 項目 | §1.2 門檻 | Rust 輪 1 | Rust 輪 2 | Rust 輪 3 | Python 基線（同時段） |
|---|---:|---:|---:|---:|---:|
| append p50 (ms) | 0.005 | 0.0216 | 0.0216 | 0.0211 | 1.010 |
| append p95 (ms) | 0.010 | 0.0302 | 0.0331 | 0.0291 | 2.251 |
| append p99 (ms) | 0.020 | 0.0523 | 0.0591 | 0.0505 | 3.207 |
| rotate 最大停頓 (ms) | 3 | 48.3 | 41.3 | 55.2 | 902.3 |

- 前次（`110bd58`，修正前）Rust rotate 約 855 ms；修正後降到 36～55 ms。前次同主機 Python 基線 rotate 108～132 ms、本次 884～902 ms：Python 的 rotate 同樣取決於 fsync 當下未回寫的資料量。
- 結論：Rust 在同主機同時段每項都優於 Python 基線（append 約 47 倍、rotate 約 16～22 倍），但兩者在本 WSL2 主機皆遠高於 §1.2 絕對門檻；門檻是否適用 WSL2 待擁有者裁決（OD-61 編排者裁決 2）。
