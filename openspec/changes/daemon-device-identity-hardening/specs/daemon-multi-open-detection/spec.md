## ADDED Requirements

### Requirement: daemon 被動偵測同機多開

系統 SHALL 提供 on-demand 偵測，掃描 `/proc` 找出同機其他 `serialwrapd` 程序（不限同 socket / 同監管模式），並 best-effort 判定目標 tty 是否被另一個 `serialwrapd` 持有。偵測 MUST NOT 自動 refuse / kill / 退讓任何 daemon（純偵測+回報）。偵測 MUST NOT 引入週期性背景掃描（僅在被查詢時計算）。

#### Scenario: 同機兩個 serialwrapd 被偵測
- **WHEN** 同機同時有兩個 `serialwrapd`（不同 socket）在跑
- **THEN** 偵測回報「偵測到多開」，且不終止任何 daemon

#### Scenario: 單一 daemon 時回報正常
- **WHEN** 同機僅一個 `serialwrapd`
- **THEN** 偵測回報無多開（`ok` 為真）

### Requirement: 多開狀態暴露於 doctor 與 daemon status

多開偵測結果 SHALL 同時暴露於 `serialwrap doctor`（daemon-less 檢查，回 `{check, ok, detail, fix}` 同形）與 `serialwrap daemon status`（回應含多開 / 外部持有者欄位）。daemon status 的偵測掃描 MUST 不阻塞 daemon 的 RPC event loop（透過 executor offload 或快取）。

#### Scenario: doctor 報出多開
- **WHEN** 偵測到多個 serialwrapd 時執行 `serialwrap doctor`
- **THEN** doctor 輸出含一筆多開檢查項，`ok` 為偽並附說明與修復指引

#### Scenario: daemon status 含多開欄位
- **WHEN** 執行 `serialwrap daemon status`
- **THEN** 回應含多開 / 外部持有者欄位，且 RPC 不因掃描而凍結

### Requirement: 跨 uid 無權限時明確降級

當執行偵測的 user 無權限讀取其他程序的 `/proc/<pid>/fd`（如 systemd-system daemon 以不同 uid 執行），偵測 SHALL 明確以 `permission` / `unknown` 狀態回報，至少確認「另有 serialwrapd 存在」，MUST NOT 靜默略過或誤報為「無多開」。

#### Scenario: 無 fd 權限時降級回報
- **WHEN** 偵測程序無法讀取另一 daemon 的 `/proc/<pid>/fd`
- **THEN** 回報降級狀態（無法判定持有哪條 tty），但仍標示偵測到其他 serialwrapd 存在
