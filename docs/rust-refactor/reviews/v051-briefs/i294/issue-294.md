# issue 294：agent 命令的完成判定可被 human console 命令的 prompt 滿足，回報 done 但輸出為空（Rust 因逐筆 RX 檢查而較易發生）

## 現象

human console（raw interactive owner）送出完整一行並按下 Enter 之後、該行的 prompt 回來之前，若 agent 的 line／background 命令剛好被送出，agent 命令的完成判定可能被「human 那一行的 prompt」滿足：命令回報 `status=done`，但 stdout 為空或只含 human 命令的殘餘輸出。DUT 實際上有正常執行 agent 的命令。

實機長跑（OD-102，rc.1 候選）觀察：

- Windows（OPI，TCP console）：13.5 小時 1 筆。WAL：`echo HUMAN_TICK_178\r`（human，單筆 TX）→ DUT 回 echo 與輸出 → agent TX `echo LR_rhw3_9302_3464\n` → 1.2 ms 後 human 命令的 prompt 單獨成一筆 RX → DUT 執行 agent 命令並輸出 marker；agent 該筆回報 done 但 stdout 不含 marker。同一工作負載的 Python 0.4.1 基線 24 小時 0 筆。
- Linux（兩塊 BRCM 板，PTY console）：候選 13.6 小時、227 行 human 輸入中，agent TX 落在「human 整行送出之後、prompt 之前」3 次，皆未造成錯誤；Python 基線 24 小時、415 行中 0 次。

## 原因（讀碼比對 Python 0.4.1 與 Rust rc.1）

- 兩個版本都**沒有**「human 行在途時延後 agent 命令」的閘門：raw owner 按 Enter 直接寫 UART、不佔前景槽；agent 命令開始前只檢查前景忙、READY、lease 等，不看 human 狀態。Python 0.4.1 的 Windows 基線 WAL 中，同樣的 TX 順序出現過 9 次（410 行 human 輸入），9 筆都正常完成。
- 差別在完成判定的檢查點：Python `wait_for_regex_from` 送出後查一次、之後每 50 ms 輪詢一次；Rust 每收到一筆 RX 就檢查（condvar，非輪詢）。human 的 prompt 若單獨成一筆 RX 並落在 agent TX 之後，Rust 會立即命中，Python 多半要到下一個 50 ms 檢查點，那時 agent 自己的 echo／輸出／prompt 已經到了。
- 所以這是兩版共有的完成判定弱點（agent 命令可被不屬於自己的 prompt 滿足），Rust 因檢查點較密而曝險較大。

## 影響與範圍

- 以行尾錨定的 `prompt_regex`（例如 `.*[$#] $`）時，窗口只有數毫秒，長跑實測約 13 小時 1 筆。
- 不帶行尾錨點的 prefix 型 prompt 樣式（內建的 prpl、Speedport 範本）尚未實測；讀碼推論在 Rust 上窗口會擴大為「human 按 Enter 到 prompt 回來」的整段，需補測。
- 契約與凍結案例沒有條文規定這個時序；既有測試的 fake DUT 都是原子回覆，沒有涵蓋。

## 使用端的避免方式

- 命令帶 `--completion-regex` 加唯一記號，或由呼叫端檢查輸出後重送冪等命令。

## 修法方向（待裁決）

- 修正完成判定：以命令自身的 echo 為錨，或對齊 Python 的檢查點；不改 schema、WAL、TX 順序，但在途情境的 stdout 會與 0.4.1 不同，需登記行為變更並補案例。
- 「開始前等 human 的 prompt」是 Python 沒有的新行為，需另行裁決且必須有上限。
- 補測 prefix 型 prompt 樣式。

完整分析與 WAL 掃描方法：長跑紀錄 `human-agent-gate-analysis.md`（擁有者工作目錄）。


## 留言
### hamanpaul 2026-11-22T01:21:42Z
## 2026-11-22 擁有者裁決

不阻擋 0.5.0；列為 0.5.0 已知問題，於 0.5.1 修正。0.5.0 release notes 註明：human 與 agent 同時操作同一 session 時，agent 命令的輸出偶爾可能為空，建議搭配 `--completion-regex` 與唯一記號；prefix 型 prompt 樣式（prpl、Speedport 範本）尚未驗證，風險可能較高。0.5.1 的工作項：修正完成判定（以命令自身的 echo 為錨或等效做法，登記行為變更）、補 prefix 型樣式的測試與案例。
