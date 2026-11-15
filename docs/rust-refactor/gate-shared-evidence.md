# Slice gate 共用證據

依 2026-11-15 OD-93，slice 2.1～2.4 可從同一份候選證據目錄匯入共用段，重複項目只執行一次。各 gate 的 judge 與判定門檻不變；各自專屬的段仍由原 gate 補齊。

## 產生與匯入

在工作樹乾淨且已提交的候選 HEAD 上執行：

```bash
scripts/rust/gate_shared.sh --out target/s2-shared-evidence --run-id <run-id> [--windows-l1 <dir>]
```

四個 gate 以 `--shared-evidence <dir>/<run-id>` 指定同一目錄。`scripts/rust/gate_shared.sh --dry-run` 只列出命令，不執行測試。四個 gate 的 `--dry-run` 也只列計畫，不產生出口證據。

共用 manifest 記錄候選 Git HEAD、產生時與匯入時的乾淨工作樹狀態、debug/release CLI 與 daemon 的 SHA256、每段實際 selector argv、鎖定集合，以及每個證據檔案的 SHA256。匯入時會核對目前 HEAD、乾淨狀態、四個 artifact 雜湊、證據檔雜湊與該 gate 的 `l1_required.lock.json` selector。任一項不符或段落缺失，都記為 `EVIDENCE_MISSING`，不會改跑該共用段；artifact provenance 不符時不會在 gate 內重建替代 binary。來源段的 FAIL 仍傳給原 judge，不會轉成 PASS。

## 共用段與鎖定 selector

- 全套 Python 測試使用 `s21_gate:pytest_full` 與 `p7_gate:python3-m-pytest-q-tests` 的相同 Linux selector：`-q tests/`，並排除 `tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native` 和 `tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`。同一份 L1、JUnit 與 coverage 輸入對應各 gate。
- Slice 2.1／2.2 的 filtered Cargo L1 使用 `--workspace --features test-fakes -- --skip cap_`；P7 的 Cargo L1 使用 `--workspace --features test-fakes`。因鎖定 selector 不同，manifest 分成兩段，不能互相代用。
- Slice 2.4 的一般 Cargo 測試 `cargo test --workspace`、test-fakes Cargo L1、逐案 contract Cargo 結果，以及 Rust contract blackbox A/B 各自記錄 argv 與輸出。
- P7 的 A/B 由 `run_p7_ab.sh --od59-contract` 產生；資料目錄與報告一併匯入。
- Windows L1 由編排者另行提供，不由共用 driver 建置或啟動 Windows runner。輸入目錄每個集合使用 `<dir>/<required-set>/l1-results.json`，例如 `<dir>/s21_core/l1-results.json`。匯入會要求 `l1-results.v1`、Windows 平台、Windows Cargo build target、候選 HEAD 起訖一致且乾淨，並逐字核對該集合的 Windows selector；再用唯一的 `tests/acceptance/l1_judge.py` 核對凍結 nodeid 集合、執行結果與 skip。必要列缺失或被 skip 時是 `EVIDENCE_MISSING`，測試失敗保留 `FAIL`。`s21_core` 可供 slice 2.1 與 P7 non-COM 共用；能力集合及 COM 集合各自依鎖檔保留獨立 selector。交叉編譯收集清單不能取代這些輸入。
- 目前 `s22_gate:win_test` 尚未登記於 `l1_required.lock.json`，沒有可核對的 Windows selector；該必要段維持 `EVIDENCE_MISSING`，`--windows-dir` 不能補成共用證據。待原生 collection 的 selector 依鎖檔程序登記後，才能納入共用 manifest。

Windows COM 測試仍依既有 owner/hardware 規則處理；Slice 2.4 的 `--windows-run` artifact、長跑及真機證據仍走原有匯入路徑。這些證據缺失時維持原判定，不由共用證據推定通過。

## 各 gate 的使用範圍

`slice21_gate.sh` 匯入 filtered Cargo L1、全套 pytest 與其 Windows L1；`s22_gate.sh` 匯入 filtered Cargo L1 及已登記的 Windows capability L1；`p7_gate.sh` 匯入 P7 Cargo L1、全套 pytest、P7 A/B 與 Windows L1；`slice24_exit.sh` 匯入全套 pytest、一般 Cargo、test-fakes Cargo、逐案 Cargo 結果與 S24 A/B。各 gate 的 parity、cfg、policy、L1.5、owner/hardware 與其他專屬段仍由原流程判定。
