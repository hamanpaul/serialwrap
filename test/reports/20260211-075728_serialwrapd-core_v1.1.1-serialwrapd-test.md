# serialwrapd-core 測試報告

- 時間(UTC): 2026-02-11 07:57:28
- 版本: v1.1.1-serialwrapd-test
- 範圍: 核心重構（daemon/rpc/wal/hotplug 結構）

## 單元測試
```bash
PYTHONPATH=/home/paul_chen/arc_prj/ser-dep /usr/bin/python3 -m unittest discover -s /home/paul_chen/arc_prj/ser-dep/tests -p "test_*.py" -v
```
結果: `Ran 10 tests ... OK`

## 語法檢查
```bash
/usr/bin/python3 -m compileall /home/paul_chen/arc_prj/ser-dep/sw_core /home/paul_chen/arc_prj/ser-dep/serialwrapd.py /home/paul_chen/arc_prj/ser-dep/sw_mcp/server.py
```
結果: 完成，無 SyntaxError。

## Smoke
- `serialwrap daemon start` 可啟動服務。
- `serialwrap daemon status` 可回傳 health。
- `serialwrap session list` 可回傳 session 狀態。
- `serialwrap daemon stop` 可正常停止。
