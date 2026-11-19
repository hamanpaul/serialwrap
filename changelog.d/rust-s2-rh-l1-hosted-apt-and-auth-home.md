---
type: fix
scope: hosted-ci
---

Hosted Docker 驗證映像改用 HTTPS 連線至 Ubuntu archive；P7 auth golden fixture 同時設定 `HOME` 與 `USERPROFILE`，讓兩平台以相同合成家目錄解析 `~`，維持 Linux golden 不變。
