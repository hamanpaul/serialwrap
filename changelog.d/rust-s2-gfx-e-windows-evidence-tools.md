---
type: fix
scope: rust-gates
---
修正候選版 Windows 原生證據工具的兩個缺陷：`scripts/rust/win-func-test.sh` 寫 ev-01 meta 的命令行尾誤寫成雙反斜線而被截斷（ev-01 的 meta 永遠寫不出），並新增全 repo 掃描測試防止同類錯誤；`scripts/stage0/win-pytest.sh` 的 staging 複製改由 `lib-stage-copy.sh` 處理，symlink 寫成「內容＝連結目標」的一般檔並明確設定 `core.symlinks=false`，Windows 原生 pytest 證據（F-S19）的 provenance 才會是 clean。
