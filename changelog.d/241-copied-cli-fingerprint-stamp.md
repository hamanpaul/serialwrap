---
type: fix
scope: rust-refactor
issue: 241
---

bundle CLI 被複製離開 `release-manifest.json` 時，仍以編譯期印入的 Python daemon 程式碼指紋回報 `--version` 與 setup `daemon_code`，與 Python 基線一致。開發建置維持占位、不謊報指紋；manifest 缺欄位或損壞時不改用編譯期值。
