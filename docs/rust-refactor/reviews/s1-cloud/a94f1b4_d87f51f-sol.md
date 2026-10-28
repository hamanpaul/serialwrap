### 3a46b25 fix(command): 支援多次 readline 折行回顯

1. **[Medium] [sw_core/session_manager.py:5762](sw_core/session_manager.py:5762) — 前置空行使原本可剝除的回顯污染 stdout。**

   設 `C = "echo " + "x"*90`，輸入：
   ```python
   "\r\n" + C[:79] + " \r" + C[79:] + "\r\nOUT\r\nroot@prplOS:~# "
   ```
   搭配既有 prompt regex，`a94f1b4` 實測回傳 `"OUT"`；新版回傳包含命令回顯的文字及 `OUT`。原因是 `text.find("\n")` 指向前置空行，未移除真正回顯中的折行標記，與後續 `lstrip("\n")` 接受前置空行的行為不一致。

   **確切修法：**先略過前置空行，定位第一個非空回顯區段，再僅於該區段移除 `" \r"`；保留後方 stdout。新增前置 `\n`／`\r\n` 搭配單次及多次折行的精確輸出回歸。

### 4e3ba13 fix(stage1): 逐列核對 Windows L1.5 延後證據

2. **[Medium] [scripts/rust/l15_exit_driver.py:89](scripts/rust/l15_exit_driver.py:89) — 把合法重複執行誤判為無效證據，擋住已授權延後。**

   helper 要求所有 `run_index == 0`、`case_id` 不重複，且證據目錄固定結尾 `/bridge/0`。然而 [rc-01-interactive-mode-switch.yaml:8](func-test/cases/rc-01-interactive-mode-switch.yaml:8) 宣告 `repeat: 5`；runner 會產生同案例的索引 0～4。

   已以記憶體內報告重現：只有 OD-31 的 `ev-01` 非 PASS，其餘案例通過時，單次假資料可豁免 producer exit 1；加入符合現有 YAML 的五次 `rc-01` PASS 後，helper 反而保留失敗。因此完整原生報告無法走通這條合法豁免路徑。

   **確切修法：**以 `(case_id, mode, run_index)` 判重，依各列索引驗證目錄；`independent_cases` 對照唯一案例數，並依案例宣告確認重複次數完整。新增「五次 PASS＋合法延後可接受」及「缺次、重複索引、任一次未授權失敗必拒絕」測試。

### b2b0bd0 test(session): 同步等待 release drain 收尾

NO FINDINGS

### d87f51f docs(rust-refactor): 記錄 s1v13 第二輪修正與驗證

NO FINDINGS

本輪逐一檢視全部 4 個 commit，無 merge commit；未修改檔案，HEAD 與工作樹維持原狀。回顯測試 13 項及 logger 契約檢查 2 項通過；兩項新增回顯測試、兩項 gate 拒絕測試均以記憶體內載入基準實作確認修正前會紅。未執行完整 pytest、Rust 建置或 Windows 原生驗證，也未存取 production daemon、指定設定／狀態目錄或實體 UART。

### High／Medium 摘要表

| 編號 | 等級 | Commit | 問題 |
|---|---|---|---|
| 1 | Medium | `3a46b25` | 前置空行造成 readline 回顯剝除回歸 |
| 2 | Medium | `4e3ba13` | 合法五次執行報告被拒，阻擋授權延後 |

High：0；Medium：2。

