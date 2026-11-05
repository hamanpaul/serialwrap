### Fixed

- 修正 Windows 原生 Cargo 測試的 passwd 對照夾具位置：由 runner 提供本 worktree 的隔離暫存根，保留真實家目錄防線，讓 HOME 優先序、hold 與 unit 快照還原回歸能進入原有行為斷言。
