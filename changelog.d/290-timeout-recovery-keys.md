---
type: change
scope: profile
---

承接 PR 290（原作者 weisheng-lin-227），並納入 issue 308：Rust profile 支援 `timeout_recovery_keys` 的 template 預設、target 繼承與覆寫；逾時恢復預設改為只送 `[CTRL_C]`，不再自動送可能讓已登入 shell 登出的 Ctrl-D。`[]` 不送救援鍵，一般 recovery 仍使 deferred human 輸入失效。setup 會在管理目錄中為缺欄位的 prpl template 精確補 `[CTRL_C]`，保留既有值與其他位元組，並在降級時保留新增欄位。POSIX 原子遷移同時保留 system profile 的 access ACL；若無法讀取或複製 ACL，會拒絕替換並保留原檔。
