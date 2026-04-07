# Design: File Transfer Primitive (file.push / file.pull)

**Issue**: #21  
**Status**: Design draft  

## Problem

No built-in file transfer mechanism. Agent workarounds (gzip+base64 inline,
local HTTP server, sequential echo) are unreliable and slow.

## Proposed API

### CLI

```bash
serialwrap file push --selector COM0 --local /path/to/file --remote /tmp/file
serialwrap file pull --selector COM0 --remote /etc/config --local ./config
```

### MCP

```json
{"tool": "serialwrap_file_push", "params": {"selector": "COM0", "local_path": "/path/to/file", "remote_path": "/tmp/file"}}
{"tool": "serialwrap_file_pull", "params": {"selector": "COM0", "remote_path": "/etc/config"}}
```

### RPC

- `file.push` → `{selector, local_path, remote_path, chunk_size?, checksum?}`
- `file.pull` → `{selector, remote_path, local_path?, chunk_size?}`

## Implementation Strategy

### Push (host → target)

1. Read local file, split into chunks (default 2KB)
2. For each chunk: base64-encode, send as `echo '<b64>' | base64 -d >> /tmp/.sw_upload_<id>`
3. After all chunks: verify checksum (`md5sum` or `sha256sum`)
4. Rename temp file to final path
5. Report progress via status callback

### Pull (target → host)

1. On target: `base64 < /path/to/file`
2. Capture output in chunks via UART
3. Decode and write locally
4. Verify checksum

### Key Considerations

- **Chunk size**: Must stay under UART buffer limit (~2KB per line safe)
- **Progress**: Return chunk count / total in status
- **Resumable**: If interrupted, check existing temp file size
- **Binary safety**: base64 encoding handles binary
- **Cleanup**: Remove temp files on success or explicit cancel

## Alternatives Considered

- **ZMODEM/XMODEM**: Too complex for serial console, requires compatible receiver
- **scp/rsync over network**: Requires network connectivity, not always available
- **tar pipe**: Single-shot, not resumable

## Dependencies

- Session must be in READY state
- Target must have `base64`, `md5sum` or `sha256sum`
