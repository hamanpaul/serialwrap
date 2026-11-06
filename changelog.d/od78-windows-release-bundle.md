---
type: feat
scope: release
---

- GitHub Release 新增 `serialwrap-<版本>-windows-x86_64.zip`（OD-78）：`release.yml` 的 `publish-windows-bundle` job 在 hosted `windows-latest` 建置，內含 Rust CLI／launcher 與自帶 CPython 3.12.3、PyYAML、pyserial 的 Python daemon runtime，解開後 smoke 通過才合併 `SHA256SUMS` 上傳；不含 com0com、未簽署，PyInstaller exe 續發
- release workflow 支援 `workflow_dispatch`（輸入 tag）對既有 tag 補傳 Windows zip，不重發 wheel、exe 與 Linux bundle
- 新增 `scripts/rust/mkbundle-windows.ps1`、`scripts/rust/mkbundle_windows.py`、`scripts/rust/smoke-windows-bundle.ps1` 與雜湊釘版的 `scripts/rust/win-bundle-requirements.txt`；`tests/test_release_workflow.py` 由禁止 Windows bundle 改為驗證該 job
