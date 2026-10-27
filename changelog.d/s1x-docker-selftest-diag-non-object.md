---
type: fix
scope: tests
---

docker smoke 的 selftest 診斷遇到頂層非物件的報告 JSON 時改列「頂層非物件」，不再於 assert 訊息求值時拋 `AttributeError` 吞掉原失敗訊息。
