---
type: fix
scope: rust-gates
---
修正候選版 Windows 原生證據工具的兩個缺陷：`scripts/rust/win-func-test.sh` 寫 ev-01 meta 的命令行尾誤寫成雙反斜線而被截斷（ev-01 的 meta 永遠寫不出），並新增全 repo 掃描測試防止同類錯誤；`scripts/stage0/win-pytest.sh` 的 staging 複製改由 `lib-stage-copy.sh` 處理，symlink 寫成「內容＝連結目標」的一般檔並明確設定 `core.symlinks=false`，Windows 原生 pytest 證據（F-S19）的 provenance 才會是 clean。

另修 `gate_shared_evidence.py` 的 `init_manifest`：候選版實跑時 `gate_shared.sh` 先寫 `RUN_DIR/logs/gate-prereqs.log` 才初始化 manifest，舊檢查把整個目錄拒收；現在只額外接受這一個前置 log（`logs/` 內別無他物、一般檔），其他內容仍拒絕。
