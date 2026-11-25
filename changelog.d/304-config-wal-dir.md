---
type: feat
scope: rust
---
受監管與前景啟動的 Rust daemon 現在可從 config.yaml 頂層 `wal_dir` 解析 WAL 目錄；優先序為 `SERIALWRAP_WAL_DIR`、config 設定、預設 `<state_dir>/wal`，相對路徑與 `~` 皆依 config 所在位置與平台展開。doctor、daemon status、setup、監管切換、更新與 rollback 會保留並使用同一結果。
