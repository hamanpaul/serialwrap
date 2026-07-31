---
type: fix
scope: doctor
---
補齊 `serialwrap doctor` 的 human console 就緒檢查組，並修正 README 自相矛盾的
`minicom` 範例（#149）：`doctor` 過去只驗 `serialwrap`／`serialwrapd` 是否在 PATH，
完全沒驗證 `serialwrap-minicom`（human console broker wrapper 本體）、`jq`、`minicom`
（`sw_core/assets/tools/minicom_router.sh` 的執行期依賴）是否就緒——doctor 全綠不代表
human console 真能動。同時 `README.md` 繁中「多 minicom 使用」段落自 2026-06-22 改名
commit 起就帶著 stale code block（`minicom` / `minicom -D /dev/ttyUSB0` 等未改名的
裸指令），與同段落下方「不要直接打 `minicom`」的警語自相矛盾，讀者照抄該 code block
正好重現本 issue 的錯誤重現步驟。

修法：(1) `sw_core/doctor_cmd.py` 的 `_check_on_path()` 新增 keyword-only
`check_name` 參數（既有呼叫端 100% 相容），供 `serialwrap-minicom` 產生底線命名
`serialwrap_minicom_on_path`；Linux 檢查清單於 `dialout` 之後、`systemd` 之前插入
`serialwrap_minicom_on_path`／`jq_on_path`／`minicom_on_path` 三項（純 `fx.which()`
查表、無 I/O 副作用）。(2) `sw_core/cli.py` 的 `_DOCTOR_ADVISORY_CHECKS` 納入新三項
（與 `devices`／`systemd` 同哲學：headless agent-only 部署不需要 human console，缺席
不拉低整體 `ok`）；`_run_setup()` payload 新增 `console_hint` 主動提示
`serialwrap-minicom COMx`；`doctor` 子命令 help／description 同步補一句（R-16）。
(3) `README.md`：英文 Prerequisites／Quick Start 補走 wrapper 的提示與範例；繁中
`## 依賴`／`## 快速開始`（含結尾同名的 `## 安裝` 附錄區塊）把 `serialwrap-minicom`
提示提升為第 2 條並加粗；`## 多 minicom 使用` 段落的 stale code block 全面改用
`serialwrap-minicom`，並在最後一行加註解澄清「僅示意 wrapper 內部 fallback 語意」，
消除與下方警語的自相矛盾；`serialwrap --help` 的 `cli-help` marker 區塊（R-16 pinned）
同步以實際指令輸出重新產生。

真機驗證：本變更 100% 由 pytest 覆蓋（3 個新 check 為純 `fx.which()` 查表，
`FakeEffects` 可完全模擬每種 PATH 組合；不涉及 daemon/PTY/UART，不符合
`docs/regression-plugin.md` 的 realhw case 定位），故未新增 TestPilot
`serialwrap_regression` case；`docs/func-test/realhw-stability-checklist.md` 的
`p0-doctor` row 追加 `serialwrap-minicom --help` 手動等效命令與新三項 check 的驗收
判定，讓既有 bench smoke 近似「免費」獲得覆蓋。

已知風險（見 PR 描述）：`realhw/preflight.py::_doctor_ok()` 與
`regression/serialwrap_regression/preflight.py` 皆用 `all()`（不分 advisory）判定
suite-refuse，新 `jq_on_path` 一旦落地會讓「bench 是否裝了 jq」首次成為真機測試套件
的 preflight 硬門檻——本批次僅以 pytest 驗證（任務範圍禁止對 live daemon 做
attach/console 等寫操作），**合併前仍建議在真機 bench 手動跑一次 `serialwrap doctor`
確認新三項皆 `ok:true`**，避免 realhw／regression suite 意外全面拒跑。
