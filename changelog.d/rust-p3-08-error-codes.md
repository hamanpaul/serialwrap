---
type: feat
scope: rust-cli
---
新增 `scripts/rust/gen_error_codes.py`，由 `contracts/error_codes.yaml`（依 YAML 順序）與 `contracts/session_last_errors.yaml` 生成 sw-proto 的 `ErrorCode`（含 `META`）與 `SessionLastError` 列舉，`--check` 可作為生成物同步閘門；並於 sw-cli 實作唯一的錯誤回應入口 `output::error_resp(ErrorCode, Option<&str>)`。生成器不產生任何 passthrough 變體（SC-34）。
