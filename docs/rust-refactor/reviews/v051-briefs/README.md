# 0.5.1 工作包 brief（給 gb10 承接用）

這個目錄放的是編排者（serialwrap-18）派給 gb10 的 0.5.1 工作包：brief、設計稿與 issue 全文。原件在編排主機的 `~/b-log/rust-refactor/exec/v051/<包>/`。

brief 中有兩類內容要依 gb10 的環境改用：
- 本機路徑：`/home/paul_chen/b-log/...` 的設計稿與 issue 檔，改讀本目錄的同名檔。
- 執行環境（共同規則第 8 條）：E 槽 cargo target、heavy-slot 與 thala 共用 slot 的規則，是編排主機專用。gb10 依自身資源設定 CARGO_TARGET_DIR 與並行度，一次只跑一個重工作。

裁決一律以 `origin/docs/parity-amendments:docs/rust-refactor/reviews/rulings/v051.md` 為準。
