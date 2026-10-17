---
type: fix
scope: session
---

Agent interactive lease 到期改由背景 tick 主動收回並保留 tombstone；flash 認線排除 released、recovering、drain 中與前景忙碌的 session；human console 單行超過 16385 bytes 或被拒時記錄 WARNING。
