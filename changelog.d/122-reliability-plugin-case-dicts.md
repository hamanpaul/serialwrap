---
type: feat
issue: 122
scope: reliability
---
擴充 `serialwrap_reliability.core` 的 case dict 映射能力：新增 longrun checkpoint steps 合成、`realhw` case → plugin case dict 轉換、依 registry 順序建構 case list，以及執行前預設排除 destructive／顯式點名才納入的選擇期過濾；同步加入 `tests/test_reliability_core.py` 覆蓋 schema 形狀、longrun steps 與 destructive filtering。
