---
type: fix
scope: contracts
---

- OD-94：長跑時數門檻由 48 h 調為每平台 24 h；Slice 2.4 不再匯入或消費長跑證據，`lr-mixed` 固定 `BLOCKED-OWNER`，RC 後另以 `realhw/acceptance/gate.py` 產生並封存綁定 RC bundle 身分的 `release-gate.json`。更新出口報告，列出共用 Windows L1 實際狀態、OD-91 MCU 模擬 A/B 與 OD-96 COM6 smoke 基準及差異、六個 coverage-map-only 案例和八個 dev-hook 測試的 CLI／daemon 證據層級；正式 Slice 2.4 不帶 `--windows-run`，BO #5 保持阻擋。
