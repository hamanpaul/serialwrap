---
type: fix
scope: tests
---
原生 Windows 執行 `-m core_portable` 時，四個頂層 `import pty` 的 posix_only 測試模組（agent defer TX、flash pump、human／agent 共存、multi-agent E2E）改以既有的模組層 POSIX 守衛略過，不再因缺 termios 中斷整次收集；契約測試的 `SessionPublic` 鍵順序依平台判定 `console_endpoint` 條件欄，真實 repo 契約檢查測試明示以 UTF-8 解碼 CLI 輸出。
