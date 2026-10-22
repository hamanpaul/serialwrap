---
type: feat
scope: stage3
---
新增 `python3 -m tools.stage3` 統一命令列入口，串接 A/B、Canary、回滾演練、證據索引與退役子命令，並以結束碼 0／1／2／3 區分 PASS、未通過、參數與 harness 錯誤及基礎設施失敗；同時新增 Stage 3 擁有者執行程序文件（總流程、Sequential A/B、Canary、回滾演練、Python daemon 退役、證據索引）。文件中的實機與時間步驟以 BLOCKED-OWNER 標示，不產生 PASS。`evidence add` 僅接受 manual-procedure 項目的人工紀錄；工具產出的 PASS／XFAIL 一律走 `evidence ingest`。`ab run` 被中斷時改依 `ab-report.json` 判定結束碼（已記錄的 FAIL 維持 exit 1，報告為 PASS 者回退為 ABORTED）。Stage 0～2.4 的 target 改用各階段自身的 revision 變數，並補上 L1 junit 與手動程序 `daemon_live` 的收集命令。
