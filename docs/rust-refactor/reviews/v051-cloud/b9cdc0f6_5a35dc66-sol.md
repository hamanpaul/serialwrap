> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

無。

## B 新發現

1. **V051-5a35dc6-1 [High]** [daemon_main.rs:1174](crates/sw-daemon/src/daemon_main.rs:1174) — 系統設定檔的相對 `wal_dir` 在 daemon 重啟後會改指其他目錄。

   具體情境：使用者設定檔不存在，`/etc/serialwrap/config.yaml` 設定 `wal_dir: archive`。首次啟動使用 `/etc/serialwrap/archive`；但啟動後 [write_endpoint](crates/sw-daemon/src/daemon_main.rs:1588) 會將系統設定連同原字串複製到使用者設定檔。下一次啟動優先讀取該副本，改用 `~/.config/serialwrap/archive`。WAL 因重啟而分流；首次啟動後 doctor 也可能因讀到副本而誤報不一致。

   **修法：**處理系統 fallback 設定複製時的路徑來源，保留 `wal_dir` 原鍵值及其原始解析基準，確保啟動、寫回、重啟與 doctor 都指向同一目錄；補上系統 fallback 加相對路徑、連續兩次啟動及 doctor 比對的回歸測試。

2. **V051-5a35dc6-2 [High]** [runtime_paths.rs:203](crates/sw-proto/src/runtime_paths.rs:203) — 寫入前以字串 `normpath` 消去 `..`，遇到符號連結時會改變實際目錄。

   具體情境：設定檔路徑為 `/cfg/link/config.yaml`，其中 `/cfg/link` 指向 `/vault/project`，且 `wal_dir: ../wal`。依檔案系統路徑解析，目標是 `/vault/wal`；目前先正規化成 `/cfg/wal`，可能寫錯磁碟位置或因權限而無法啟動。絕對路徑中含符號連結與 `..` 時也有相同問題。

   **修法：**保留交給檔案系統解析的路徑語意，避免在尚未解析符號連結時折疊 `..`；doctor 比對也應避免把不同實際目錄判成相同。補上含符號連結的相對與絕對路徑測試。

定向驗證：`sw-proto` 的兩個 WAL 路徑測試通過；`sw-daemon` 的系統設定 fallback／寫回既有測試通過。上述兩個反例目前是依程式路徑推導；符號連結的路徑差異另以唯讀路徑解析確認，尚未以 daemon 端到端重現。未執行完整 pytest、Docker 或 bundle 測試；工作樹保持乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正