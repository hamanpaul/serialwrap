> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD：`c89cd78c`（detached，工作樹乾淨）。已逐一檢查 `e183755b`、`c89cd78c`，無 merge。前輪修正提交均為 HEAD 祖先；本輪未改產品碼，相關既有測試斷言保留。未找到 `p8-fix-responses.md`。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 原始 selector 傳遞保留。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | 佔位退休與 release 取消互斥保留。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | timeout／settled token 配對保留。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | 未確認關閉的阻擋與持久化重試保留。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 未取得 handle 的開埠失敗排空路徑保留。 |
| P8-fdc1413-1 | High | 已修 8196bca6 | 0 | `finish_attach_op` 鎖內重讀 `close_failed` 保留。 |
| P8-503c341-1 | High | 已修 6334a896 | 0 | bind 重試認領並關閉 parked 作業保留。 |
| P8-e25b69c-1 | High | 已修 8463b22c | 0 | 雙 release 共用關閉作業與首次期限保留。 |
| P8-e25b69c-2 | High | 已修 b1a2e83a | 0 | fallback 遲到完成 RELEASED 與持久化路徑保留。 |
| P8-8463b22-1 | High | 已修 14fe5922 | 0 | 撤銷標記保留至開埠終態。 |
| P8-14fe592-1 | High | 已修 c4913aed | 0 | release 等待退休 bridge 關閉回條保留。 |
| P8-c4913ae-1 | High | 依裁決不處理 | 3 | 歸 wp510r，不增加輪數。 |
| P8-1533cb5-1 | High | 已修 2a660d96 | 0 | atomic 鏡像與鎖內 TX fence 保留。 |
| P8-a190a57-1 | Medium | 已修 a81ff33b | 0 | artifact 選取與環境還原邏輯保留。 |
| P8-a190a57-2 | Medium | 已修 479f8d2b | 0 | TX 串接與交錯案例斷言保留。 |
| P8-a81ff33-1 | Medium | 已修 479f8d2b | 0 | 假 repo／artifact 與建置目錄隔離保留。 |
| P8-591d2ff-1 | Low | 未修 | 10 | `tests/test_contracts_catalog.py:1451` 仍固定要求 DF-27 為 `APPROVED`。 |
| P8-a94151a-1 | Medium | 已修 3c357879 | 0 | 明確 release／Drop 才放行、放行後清除 paused 的邏輯保留。 |
| P8-32219f2-1 | Medium | 已修 08969338 | 0 | 可追溯報告及失敗／待驗收聲明保留；報告與前輪 Git blob 相同。 |
| P8-0896933-1 | Low | 未修 | 2 | 驗收報告第 18～22、32、40 行的統計與時間誤植未改。 |

## B 新發現

**P8-c89cd78-1 [Low]** [tests/test_contracts_catalog.py:1439](tests/test_contracts_catalog.py:1439) — 新增 DF-26 檢查再次將狀態固定為 `APPROVED`。依裁決，slice 出口取得證據後可推進狀態；屆時僅將 DF-26 改為 `IMPLEMENTED`，此測試便會失敗。在記憶體中替換該狀態已重現，`VERIFIED` 亦同。**修法：允許 `APPROVED`／`IMPLEMENTED`／`VERIFIED`，其餘屬性斷言保留；狀態推進所需證據仍由出口 gate 驗證。**與前輪 DF-27 同類，列 Low backlog，不擋本輪放行。

其餘核對結果：

- `e183755b`：DF-26 登記與案例對應符合 OD-80；案例仍要求 `PASS`，未改為 XFAIL／SKIP。四個 amendment 僅追加，既有項目、分母及必要集合保留。
- `c89cd78c`：測試經正式 `Service::rpc` 與容量為 131072 字元的 RX 視窗，驗證 150 KiB pull 的完整內容、長度及 MD5；L1 僅新增該節點。

本輪驗證：

- 指定 `CARGO_TARGET_DIR` 執行新增 Rust 測試：**1 passed**；針對時序疑慮再連跑 **12/12 passed**。
- 直接呼叫兩個相關 Python 測試函式：**2 passed**；未載入 pytest `conftest.py`，不計為 pytest suite。
- `tools.contracts check`：**ok=true，無 findings**。
- `regen_lock.py --check --base 1dd4a3df`：通過。
- `policy_check --repo .`：**25 passed、0 failed、2 warnings**（R-19／R-22）。
- `git diff --check`：exit 2，僅報 `file_transfer_rpc.rs:335` 新增檔尾空白行。

未修改來源或文件；未跑完整 pytest、Docker／bundle，未接觸 production daemon、指定設定／狀態目錄或實體 UART。

## 既有碼待辦（不計入結論）

- 沿用前輪：正式 `human_active` 接線、一般 RELEASED session 開埠失敗再遇落盤失敗的終態差異。
- P8-c4913ae-1 歸 wp510r。
- 本輪完成 OD-80 的契約登記與跨視窗 L1 驗證；未取得實機 1 MB pull 證據，不替代 L2 驗收。
- 前輪無 features sw-pal 編譯失敗、Clippy exit 101，以及 host 完整 pytest 待驗收狀態，本輪未重驗；放行僅涵蓋指定增量。

## 對裁決的異議（不計入結論）

無。

結論：可放行