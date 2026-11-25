---
type: feat
scope: rust
---
受監管與前景啟動的 Rust daemon 現在可從 config.yaml 頂層 `wal_dir` 解析 WAL 目錄；優先序為 `SERIALWRAP_WAL_DIR`、config 設定、預設 `<state_dir>/wal`，相對路徑與 `~` 皆依 config 所在位置與平台展開。system config 複製後保留 `wal_dir` 原值與來源基準；使用者改值後來源基準即失效，不會在改回舊字串時復活，也不會覆寫既有同名設定。daemon status 回報實際 WAL 路徑，doctor 不以自己的工作目錄猜測 daemon 相對路徑。setup、監管切換、更新與 rollback 會保留設定。
