---
type: feat
scope: sw-pal
issue: null
---
實作 systemd、Windows 工作排程器與 Windows 服務的 PAL 監管控制，並加入交接期間持久停用自動拉起及釋放還原流程。修正 held 重新註冊、具名 hold 釋放、systemd 家目錄解析與安裝失敗回報，並補齊對應回歸案例。
