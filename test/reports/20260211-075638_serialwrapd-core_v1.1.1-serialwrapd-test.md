# serialwrapd-core 測試報告

- 時間(UTC): 2026-02-11 07:56:38
- 版本: v1.1.1-serialwrapd-test
- 範圍: daemon/cli/rpc/wal/profile/alias/singleton

## 1) 單元測試
命令:
```bash
PYTHONPATH=/home/paul_chen/arc_prj/ser-dep /usr/bin/python3 -m unittest discover -s /home/paul_chen/arc_prj/ser-dep/tests -p "test_*.py" -v
```
結果:
- Ran 10 tests
- OK

## 2) 編譯檢查
命令:
```bash
/usr/bin/python3 -m compileall /home/paul_chen/arc_prj/ser-dep/sw_core /home/paul_chen/arc_prj/ser-dep/serialwrapd.py /home/paul_chen/arc_prj/ser-dep/sw_mcp/server.py
```
結果:
- 完成，無 SyntaxError

## 3) 端到端 Smoke
命令序列:
```bash
/home/paul_chen/arc_prj/ser-dep/serialwrap daemon start --profile-dir /home/paul_chen/arc_prj/ser-dep/profiles
/home/paul_chen/arc_prj/ser-dep/serialwrap daemon status
/home/paul_chen/arc_prj/ser-dep/serialwrap session list
/home/paul_chen/arc_prj/ser-dep/serialwrap daemon stop
```
觀察:
- daemon 可啟動並回傳 pid/socket
- `daemon status` 可回傳 health/wal 路徑
- `session list` 可讀取 session 狀態
- `daemon stop` 可停止服務

## 4) 風險與後續
- `login_fsm` 與真實 BCM/prpl 裝置仍需實機驗證。
- `UARTBridge` 在高頻流量下的 long-run 穩定性需壓測。
- MCP 目前為 RPC adapter，完整 MCP protocol capability 可再補強。
