# F91／F111 受控重現（第三輪）：模擬 F94 洩漏 daemon，對啟動後新出現的 /dev/pts/N 開啟並持續讀走資料。
# 用法：python3 f91-hijack-pts.py 50 & ；同時執行
#   cargo test -p sw-daemon --features test-fakes --test restart_after_commit -- --exact kill_before_commit_restores_previous_state
# 期望（產品正確拒開）：panic 訊息含 last_error：ATTACH_FAILED:DeviceHeldError 與 /dev/pts/N 的持有者（本腳本 pid）。
# 模擬 F94 洩漏 daemon：對啟動後新出現的 /dev/pts/N 開啟並持續讀走資料（不寫）
import os, select, time, sys
seen = set(os.listdir("/dev/pts")); fds = {}
end = time.time() + float(sys.argv[1])
while time.time() < end:
    for n in os.listdir("/dev/pts"):
        if n not in seen and n != "ptmx":
            seen.add(n)
            try: fds[os.open(f"/dev/pts/{n}", os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)] = n
            except OSError: pass
    r, _, _ = select.select(list(fds), [], [], 0.01) if fds else ([], [], time.sleep(0.01))
    for fd in r:
        try: os.read(fd, 4096)
        except OSError: pass
