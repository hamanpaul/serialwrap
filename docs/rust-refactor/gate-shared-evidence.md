# Slice gate 共用證據

依 2026-11-15 OD-93，slice 2.1～2.4 可從同一份候選證據目錄匯入共用段，重複項目只執行一次。各 gate 的 judge 與判定門檻不變；各自專屬的段仍由原 gate 補齊。

## 產生與匯入

在工作樹乾淨且已提交的候選 HEAD 上執行：

```bash
scripts/rust/gate_shared.sh --out target/s2-shared-evidence --run-id <run-id> [--windows-l1 <dir>]
```

共用證據目錄產生後不可搬動或改名。`p7_ab` 匯入會比對完整 argv，其中包含共用目錄的絕對路徑與目錄名；搬動或改名會使該段成為 `EVIDENCE_MISSING`，P7 的 A/B 與 func-test 證據列都不會被採用。因此應在產生前決定最終 `--out` 路徑，直接於該位置完成產生與匯入。

`gate_shared.sh`、`p7_gate.sh` 與 `slice24_exit.sh` 的正式執行都會先檢查 `git status --porcelain --untracked-files=normal`；髒工作樹時列出髒檔並以結束碼 3 停止，不啟動前置或測試。三個 driver 的 `gate_prereqs.sh --gate` 失敗時，保留檢查／建置輸出的具體原因於各自 run 目錄的 `logs/gate-prereqs.log`，並以結束碼 4 回報（參數錯誤仍為 2）。P7 未指定 `--out-md` 時，報告寫到 `target/p7-gate/<run_id>/p7-gate.md`，不改動 `docs/`。

四個 gate 以 `--shared-evidence <dir>/<run-id>` 指定同一目錄。`scripts/rust/gate_shared.sh --dry-run` 只列出命令，不執行測試。四個 gate 的 `--dry-run` 也只列計畫，不產生出口證據。

共用 manifest 記錄候選 Git HEAD、產生時與匯入時的乾淨工作樹狀態、debug/release CLI 與 daemon 的 SHA256、每段實際 selector argv、鎖定集合，以及每個證據檔案的 SHA256。四個 artifact 的 manifest 識別名稱維持 `debug_cli`、`debug_daemon`、`release_cli`、`release_daemon`；實體路徑固定為 `artifacts/debug/serialwrap`、`artifacts/debug/serialwrapd`、`artifacts/release/serialwrap`、`artifacts/release/serialwrapd`，匯入 gate 的副本亦沿用各自的 `serialwrap`／`serialwrapd` 檔名。匯入時會核對目前 HEAD、乾淨狀態、artifact 路徑與四個 artifact 雜湊、證據檔雜湊及該 gate 的 `l1_required.lock.json` selector。任一項不符或段落缺失，都記為 `EVIDENCE_MISSING`，不會改跑該共用段；artifact provenance 不符時不會在 gate 內重建替代 binary。來源段的 FAIL 仍傳給原 judge，不會轉成 PASS。

## 共用段與鎖定 selector

- 全套 Python 測試使用 `s21_gate:pytest_full` 與 `p7_gate:python3-m-pytest-q-tests` 的相同 Linux selector：`-q tests/`，並排除 `tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native` 和 `tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`。同一份 L1、JUnit 與 coverage 輸入對應各 gate。
- Slice 2.1／2.2 的 filtered Cargo L1 使用 `--workspace --features test-fakes -- --skip cap_`；P7 的 Cargo L1 使用 `--workspace --features test-fakes`。因鎖定 selector 不同，manifest 分成兩段，不能互相代用。
- Slice 2.4 的一般 Cargo 測試 `cargo test --workspace`、test-fakes Cargo L1、逐案 contract Cargo 結果，以及 Rust contract blackbox A/B 各自記錄 argv 與輸出。逐案 Cargo 結果雖執行 test-fakes 測試 binary，其 `rust-linux` 受測身分蓋章與 S24 黑箱相同，使用同 HEAD 候選 bundle 版本目錄內的 `bin/serialwrap{,d}`，不得使用 debug artifact；兩層 SHA256 不一致時仍判身分錯誤。
- S24 A/B 執行前後各重新核對候選 bundle。任一檢查失敗時 `s24_ab` 明確記為 `EVIDENCE_MISSING`，不把測試結果當有效證據，並繼續處理 Windows L1 匯入；Windows 匯入結果仍依各自的 HEAD、乾淨狀態、selector 與 `l1_judge` 判定。
- Slice 2.3 的黑箱使用 debug daemon；Slice 2.4 候選是 release bundle。跨切片身分以 P7 結果的 `prior_slice_artifacts.serialwrapd_release` 對照 2.4 的 `--expect-daemon-sha256`；若 release SHA 不符仍拒收，不能以 2.3 `artifacts.serialwrapd` 的 debug SHA 代替。
- P7 的 A/B 由 `run_p7_ab.sh --od59-contract` 產生；資料目錄與報告一併匯入。
- Windows L1 由原生 Windows 執行產生，不由共用 driver 建置或啟動 Windows runner。輸入目錄每個集合使用 `<dir>/<required-set>/l1-results.json`，例如 `<dir>/s21_core/l1-results.json`。匯入會要求 `l1-results.v1`、Windows 平台、Windows Cargo build target、候選 HEAD 起訖一致且乾淨，並逐字核對該集合的 Windows selector；再用唯一的 `tests/acceptance/l1_judge.py` 核對凍結 nodeid 集合、執行結果與 skip。必要列缺失或被 skip 時是 `EVIDENCE_MISSING`，測試失敗保留 `FAIL`。`s21_core` 可供 slice 2.1 與 P7 non-COM 共用；P7 COM 與各能力集合依鎖檔保留獨立 selector。交叉編譯收集清單不能取代這些輸入。
- `s22_gate:win_test` 已登記於 `l1_required.lock.json`。Slice 2.2 的共用模式直接從共用 manifest 匯入 `windows_l1:s22_gate:win_test`；缺少該段或其 provenance／selector／必要節點驗證失敗時維持 `EVIDENCE_MISSING`，不啟動 Windows runner，也不以 `--windows-dir` 替代共用 manifest 證據。非共用模式則依原流程使用明確指定的 Windows L1 目錄。
- P7 共用模式下 Windows L1 匯入被拒時不記入 `caps_missing`；缺少該列的 `l1_results` 由 P7 judge 判為 `INCOMPLETE`。`BLOCKED-OWNER` 僅用於實際缺少 Windows 執行能力的主機，不用來掩蓋 provenance、selector、skip 或必要節點缺漏。

Windows COM 測試仍依既有 owner/hardware 規則處理；Slice 2.4 的 `--windows-run` artifact、長跑及真機證據仍走原有匯入路徑。這些證據缺失時維持原判定，不由共用證據推定通過。

## 各 gate 的使用範圍

`slice21_gate.sh` 匯入 filtered Cargo L1、全套 pytest 與其 Windows L1；`s22_gate.sh` 匯入 filtered Cargo L1 及已登記的 Windows capability L1；`p7_gate.sh` 匯入 P7 Cargo L1、全套 pytest、P7 A/B 與 Windows L1；`slice24_exit.sh` 匯入全套 pytest、一般 Cargo、test-fakes Cargo、逐案 Cargo 結果與 S24 A/B。各 gate 的 parity、cfg、policy、L1.5、owner/hardware 與其他專屬段仍由原流程判定。
