---
type: fix
scope: realhw
---
長跑 observer 在 Windows 獨占探測提早喚醒時，不再多取一筆樣本並跳過下一筆排定取樣；修正每 300 秒出現約 19.5 秒觀測缺口、僅比 gate 門檻少 0.5 秒的問題。
