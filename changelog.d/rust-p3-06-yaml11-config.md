---
type: feat
scope: rust-cli
---
實作 `sw-proto::yaml11` 與 `config_types`：以 `yaml-rust2` 事件流支援 PyYAML 1.1 SafeLoader 子集解析（含 YAML 1.1 型別判定、tag 建構與 SafeLoader 子集相容），並依 Python 基準實作 profile 目錄載入、template 解析與 `RuntimeConfig` 存取。
