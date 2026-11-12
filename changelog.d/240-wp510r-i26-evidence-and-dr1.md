---
type: fix
scope: rust-acceptance
---

Windows 能力證據匯入與重判均核對受測工作樹起訖的候選 commit 與乾淨狀態；版本不符、缺 provenance 或工作樹不乾淨時記為 EVIDENCE_MISSING（LOCAL-WINDOWS-TODO），附上拒收原因。
DR-1(d) 回歸改為確實填滿 release 的 safety lane，並同步確認排空後再送下一個 release，保留成功、關閉一次、帳本移除與不得重開的原斷言。
