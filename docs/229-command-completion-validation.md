# 每命令完成條件驗證（#229）

追蹤 [#229](https://github.com/hamanpaul/serialwrap/issues/229)：完整 producer 回應已抵達，卻因沒有 profile prompt 而等待 timeout／partial。修正語意見 [每命令完成條件](command-completion.md)。本紀錄是 2026-10-14 的驗證快照，不代表所有 console 雜訊或 target 回覆缺損已解決。

## 原始觀察與修正

單一 systemd broker、單一 UART 的唯讀查詢已輸出唯一 BEGIN、value、RC=0、END；整個 180 秒命令視窗沒有符合 profile 的 prompt，呼叫端最後收到 partial。缺少 prompt 已確認，但沒有證據確定 prompt 被 log 打散，也沒有證據把本次問題認定為 #162 回歸。

程式修正 commit 為 `6aaea92`。呼叫端可指定 `--completion-regex`，broker 只比對本次提交後的新 RX；結果保存該 regex，讓 operator 能確認真正運行的 daemon 有使用新介面。省略參數仍等待 profile prompt。呼叫端繼續驗 framing、唯一 value、實際 RC 與 readback；reboot／SysRq 維持原生 recovery。

## 已執行的驗證

- 全套 `python3 -m pytest -q tests/`：1966 passed、16 skipped、92 subtests passed，140.68 秒。
- PTY 覆蓋完整 numeric RC／END、無 prompt、echo-only、歷史標記、無效 regex 零 TX、原 prompt fallback、CLI／RPC／queue 傳遞與 stdout 前置 prompt 不截斷。
- 安裝 wheel 後，53 個已追蹤 Python source files 與上述 source commit 的內容相符。單一 systemd broker 持有實體 UART，沒有第二個 reader。
- 三個真機唯讀命令共 1.949 秒，均為 done／partial=false，command record 保留本次指定的 completion regex。
- 真機 TestPilot 串接流程已完成 normal reboot、factory reset、還原後 normal reboot，共三次開機，最後原始 bytes 與 hashes 校對相符。產品 finding 由呼叫端的獨立報告判定。
- 該輪共 254 個命令，其中 251 個使用 producer completion，3 個為原生 reboot。兩個 producer 回覆缺少完整要求的 frame，被保留為 partial；RAW 視窗中沒有「符合指定 completion 卻回傳 partial」的例子。此結論只涵蓋本輪觀察。
- 三次開機的第一個自動空行 probe 都在 RX 靜默滿五秒後送出：5.019885、5.005466、5.019914 秒。credential TX 不記入 WAL，此量測只驗證空行 probe 的 gate；secret 的靜默 gate 另由已安裝 source 與 PTY regression 覆蓋。

完整 framing 無 prompt 的完成語意可由 pytest／PTY 回歸覆蓋。真板登入、console lock 與開機時序另以此次真機串接驗證；原始 RAW 保留在 operator 的受限 evidence storage，本文件只保存必要結論。

## 限制與交付狀態

不完整的 producer frame 仍可能 timeout／partial，不會被這個介面當成成功。broker 完成也不等於 target 操作或產品安全通過。

修正與本紀錄保存在 feature branch；issue 保持 open，尚未推送或建立 PR。後續 PR 應引用 `Closes #229`，並另評估真板 console lock／開機時序的 serialwrap regression case 歸屬。
