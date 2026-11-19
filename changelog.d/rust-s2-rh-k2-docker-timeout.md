---
type: fix
scope: docker-ci
---

L1.5 Docker smoke 以逐行 BuildKit 輸出保留建置診斷；逾時時附上輸出尾端，並延長 hosted runner 的 smoke 等待上限。
