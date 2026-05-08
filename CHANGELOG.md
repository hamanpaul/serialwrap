# Changelog

本檔案依照 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-TW/1.1.0/) 格式維護，版本依照 [Semantic Versioning](https://semver.org/lang/zh-TW/) 編號。

## [Unreleased]

### Added

- 導入 [paulsha-conventions](https://github.com/hamanpaul/paulsha-conventions) v1.0.0 治理基線（`.paul-project.yml`、`policy_version: 1.0.0`）
- 新增 `VERSION` 檔案（值 `0.0.1`，對齊既有 git tag `v0.0.1` / policy R-07）
- 新增 `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`（AI agent policy checklist）
- 新增 `.github/pull_request_template.md`（含 R-11 policy checklist）
- 新增 `.github/workflows/policy-check.yml`（PR 自動 policy 驗證）
- README.md 補充 `## Install`、`## Usage`、`## Version` 段落與 CLI help marker
- `sw_core/config.py`：`ProfileTemplate` 新增 `bootloader_prompts: list[str] = []` 欄位；`SessionProfile` 新增 `bootloader_prompts: tuple[str, ...] = ()`；loader 自動從 YAML 解析並傳播至 session profile
- `sw_core/constants.py`：新增 `MAX_RECOVERY_LEASE_S = 120.0` 與 `BOOTLOADER_RX_TAIL_BYTES = 512`（為 Issue #44 bootloader recovery 所需）
- `profiles/default.yaml`：`brcm-template` 加入 `bootloader_prompts`（CFE、U-Boot、BCM 系列 prompt pattern）

### Changed

- `.github/copilot-instructions.md` 前置 paulsha-conventions marker 與 policy_version

### Notes

- Phase A 為治理/文件/CI scaffolding，不含 Issue #44 recovery 功能
- policy_check engine pinned to `ff1a031172ec24fc155699f9f3ce5bdea24d9e24`
