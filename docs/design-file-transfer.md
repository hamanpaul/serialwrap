# Design: File Transfer Primitive (file.push / file.pull)

**Issue**: #21  
**Status**: 已實作（Phase 2，`feat/open-issues-phase2`）  

## 背景問題

過去沒有內建檔案傳輸機制。Agent 的替代方案（gzip+base64 inline、本地 HTTP server、sequential echo）既不可靠又慢。

## API

### CLI

```bash
serialwrap file push --selector COM0 --local /path/to/file --remote /tmp/file
serialwrap file pull --selector COM0 --remote /etc/config --local ./config
```

### MCP

```json
{"tool":"serialwrap_file_push","params":{"selector":"COM0","local_path":"/path/to/file","remote_path":"/tmp/file"}}
{"tool":"serialwrap_file_pull","params":{"selector":"COM0","remote_path":"/etc/config"}}
```

### RPC

- `file.push` → `{selector, local_path, remote_path, chunk_size?, checksum?}`
- `file.pull` → `{selector, remote_path, local_path?, chunk_size?}`

## 實作策略

### Push（host → target）

1. 讀取本地檔案，分割為 chunk（預設 2KB）
2. 每個 chunk：base64 編碼後送出 `echo '<b64>' | base64 -d >> /tmp/.sw_upload_<id>`
3. 所有 chunk 送完後：校驗 checksum（`md5sum` 或 `sha256sum`）
4. 將暫存檔 rename 到最終路徑
5. 透過 status callback 回報進度

### Pull（target → host）

1. 在 target 執行 `base64 < /path/to/file`
2. 透過 UART 分段擷取輸出
3. 解碼並寫入本地
4. 校驗 checksum

### 設計要點

- **Chunk size**：須控制在 UART buffer 上限內（每行 ~2KB 安全）
- **Progress**：在 status 回傳 chunk count / total
- **Resumable**：中斷時可檢查既有暫存檔大小
- **Binary safety**：base64 編碼處理二進位檔案
- **Cleanup**：成功或明確取消後清除暫存檔

## 曾考慮的替代方案

- **ZMODEM/XMODEM**：serial console 太複雜，需要相容接收器
- **scp/rsync**：需要網路連線，不一定可用
- **tar pipe**：一次性，不可恢復

## 前置條件

- Session 必須處於 `READY` 狀態
- Target 必須有 `base64`、`md5sum` 或 `sha256sum`
