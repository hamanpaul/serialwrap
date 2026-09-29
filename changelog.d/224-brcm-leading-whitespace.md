---
type: fix
scope: brcm-template
issue: 224
---

修正 `brcm-template` 漏配帶前導 space／tab 的 BDK `>`／`#` 提示符，避免正確帳密下仍發生自動登入與 READY 確認逾時；保留行首錨定及 banner 裝飾線排除，並新增直接載入出貨 YAML 的 regex、登入流程與自動偵測回歸測試。
