---
type: feat
scope: rust
---
受監管與前景啟動的 Rust daemon 現在可從 config.yaml 頂層 `wal_dir` 解析 WAL 目錄；優先序為 `SERIALWRAP_WAL_DIR`、config 設定、預設 `<state_dir>/wal`。複製 system fallback 到新建的 user config 時，相對 `wal_dir` 會依 system config 所在目錄解析並寫成絕對路徑，含 `~` 的值會在複製時展開，原本的絕對路徑維持原字串；使用者之後編輯的相對值一律依目前 config 所在目錄解析。config 不寫入來源中繼資料，既有使用者設定鍵保持原樣。daemon status 回報實際 WAL 路徑，doctor 不以自己的工作目錄猜測 daemon 相對路徑。setup、監管切換、更新與 rollback 會保留設定。
