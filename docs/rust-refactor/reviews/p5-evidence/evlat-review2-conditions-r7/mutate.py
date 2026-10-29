"""event_latency 審閱 2 C3／C4 變異證明：在工作樹原地套用單一變異（跑完由呼叫端還原）。"""
import pathlib, sys
ROOT = pathlib.Path("crates/sw-core/src")  # 於 repo 根執行
mb = ROOT / "mailbox.rs"; ac = ROOT / "actor.rs"
def rep(p, old, new):
    t = p.read_text(encoding="utf-8"); assert t.count(old) == 1, (p, old); p.write_text(t.replace(old, new), encoding="utf-8")
LOOP_TOP = """    ) -> Option<MailboxItem> {
        loop {
            if let Some(index) = self
                .stash"""
STATIC = "static MUT_N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);\n"
name = sys.argv[1]
if name == "M1":   # data 可越過 control 至多 3 筆
    rep(mb, LOOP_TOP, LOOP_TOP.replace("        loop {\n", """        loop {
            if !self.control_rx.is_empty() || !self.safety_rx.is_empty() {
                if MUT_N.load(std::sync::atomic::Ordering::Relaxed) < 3 {
                    if let Ok(d) = self.data_rx.try_recv() {
                        MUT_N.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                        return Some(MailboxItem::Data(d));
                    }
                }
            } else {
                MUT_N.store(0, std::sync::atomic::Ordering::Relaxed);
            }
"""))
    t = mb.read_text(encoding="utf-8"); mb.write_text(t + STATIC, encoding="utf-8")
elif name == "M2":  # 拿掉 lane 見證數累加
    rep(mb, """                bump_first(&mut self.witness.control, self.control_rx.len());
                bump_first(&mut self.witness.safety, self.safety_rx.len());
""", "")
elif name == "M3":  # stopped 非空即 panic
    rep(mb, LOOP_TOP, LOOP_TOP.replace("        loop {\n", "        loop {\n            assert!(self.stopped.is_empty(), \"M3\");\n"))
elif name == "M5":  # timer 每 4 輪才檢查，且 deadline 過後仍回最多 3 筆 data
    rep(ac, """            loop {
                let now = clock.mono_ns();
                if let Some(index) = timers.iter().position(|(at, _)| *at <= now) {""", """            let mut mut_iter = 0u64;
            loop {
                let now = clock.mono_ns();
                mut_iter += 1;
                if let Some(index) = timers.iter().position(|(at, _)| *at <= now && mut_iter % 4 == 0) {""")
    rep(mb, """            let now = clock.mono_ns();
            if deadline.is_some_and(|at| now >= at) {
                return None;
            }""", """            let now = clock.mono_ns();
            if deadline.is_some_and(|at| now >= at) {
                if MUT_N.load(std::sync::atomic::Ordering::Relaxed) < 3 {
                    if let Ok(d) = self.data_rx.try_recv() {
                        MUT_N.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                        return Some(MailboxItem::Data(d));
                    }
                }
                MUT_N.store(0, std::sync::atomic::Ordering::Relaxed);
                return None;
            }""")
    t = mb.read_text(encoding="utf-8"); mb.write_text(t + STATIC, encoding="utf-8")
elif name == "M6":  # stopped 路徑不先排空 data（receipt 越過已讀 RX，與 Python 排空順序不符）
    rep(mb, """            if let Some(receipt) = self.stopped.front() {
                if let Ok(data) = self.data_rx.try_recv() {
                    return Some(MailboxItem::Data(data));
                }""", """            if let Some(receipt) = self.stopped.front() {""")
elif name == "M7":  # N_pre 不分 epoch（舊 epoch data 被算進依賴等待前已讀入筆數）
    rep(mb, "if pre.epoch == *epoch {", "if pre.epoch == *epoch || true {")
else:
    raise SystemExit(f"unknown {name}")
print("applied", name)
