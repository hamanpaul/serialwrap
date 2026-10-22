//! 命令仲裁：`CommandRegistry`（admission、記錄、history）與 `SessionQueue`（優先序＋FIFO 出列）。
//!
//! Slice 2.1 Task 8 骨架：本檔只含型別與函式簽章，行為由 Task 8 實作者依 plan 06 Task 8 補上。
//! 函式本體皆為 `s21_todo!`（即 `unimplemented!`，訊息 `slice21 T8 <symbol>`），使紅燈測試可編譯並於執行期失敗（非編譯錯誤）。

use std::sync::Arc;

use sw_proto::pyjson::PyDict;

/// 命令提交請求（對應 Python `CommandArbiter.submit` 的參數）。
#[derive(Clone, Debug)]
pub struct SubmitReq {
    pub session_id: String,
    pub command: String,
    pub source: String,
    pub mode: String,
    pub timeout_s: f64,
    pub priority: i128,
    pub expected_duration_s: Option<f64>,
    pub completion_regex: Option<String>,
}

/// 已進入佇列的命令；排序鍵為 `(priority, counter)`，數值小者先出列。
#[derive(Clone, Debug)]
pub struct QueuedCommand {
    pub sort_key: (i128, u64),
    pub cmd_id: String,
    pub req: SubmitReq,
}

/// admission 成功的結果：RPC 回應字典與待入列項目。
pub struct Admitted {
    pub response: PyDict,
    pub queued: QueuedCommand,
}

/// worker 取件時的決策：仍可執行或已被取消／終結而跳過。
pub enum RunDecision {
    Run,
    Skip,
}

/// 在途命令的中止分類（BC-03 三態映射）。
#[derive(Clone, Debug, PartialEq)]
pub enum AbortClass {
    InFlight,
    NotSubmitted,
    Unknown,
}

/// 命令完成結果。
pub enum CmdOutcome {
    Result(PyDict),
    SendFailed,
    Aborted(AbortClass),
}

/// 命令登錄表：記錄（保留插入順序）、計數器與各 session 的在途命令指標。
pub struct CommandRegistry {
    _priv: (),
}

impl CommandRegistry {
    /// 建立空登錄表（骨架：不含狀態）。
    pub fn new() -> Arc<CommandRegistry> {
        Arc::new(CommandRegistry { _priv: () })
    }

    /// admission：依 completion regex、長度、換行、session 註冊與 pending 上限順序檢查後建立紀錄。
    pub fn admit(
        &self,
        _req: SubmitReq,
        _queue_registered: bool,
        _cmd_id: String,
        _now_iso: &str,
    ) -> Result<Admitted, PyDict> {
        s21_todo!("T8", "CommandRegistry::admit")
    }

    /// worker 取件：仍為 accepted 則轉 running 並回 `Run`；已取消或已終結回 `Skip`。
    pub fn mark_running(&self, _cmd_id: &str, _now_iso: &str) -> RunDecision {
        s21_todo!("T8", "CommandRegistry::mark_running")
    }

    /// 設定或清除某 session 的在途命令指標（影響 pending 計數與 history 淘汰）。
    pub fn set_in_flight(&self, _session_id: &str, _cmd_id: Option<&str>) {
        s21_todo!("T8", "CommandRegistry::set_in_flight")
    }

    /// 命令終結：依 outcome 寫入 status／error_code／stdout 等欄位。
    pub fn complete(&self, _cmd_id: &str, _outcome: CmdOutcome, _now_iso: &str) {
        s21_todo!("T8", "CommandRegistry::complete")
    }

    /// 取消：accepted／running／interactive／canceled 可取消，回 `{"ok":true,...}`；其餘回錯誤字典。
    pub fn cancel(&self, _cmd_id: &str, _now_iso: &str) -> PyDict {
        s21_todo!("T8", "CommandRegistry::cancel")
    }

    /// 將該 session 尚未啟動（accepted）的命令以 `code` 終結，回傳被 flush 的 cmd_id（依 admit 順序）。
    pub fn flush_session(&self, _session_id: &str, _code: &str, _now_iso: &str) -> Vec<String> {
        s21_todo!("T8", "CommandRegistry::flush_session")
    }

    /// 取得紀錄副本。
    pub fn get(&self, _cmd_id: &str) -> Option<PyDict> {
        s21_todo!("T8", "CommandRegistry::get")
    }

    /// 目前紀錄總數（含在途，history 淘汰後的值）。
    pub fn len(&self) -> usize {
        s21_todo!("T8", "CommandRegistry::len")
    }

    /// 該 session 的 pending 數：無 `done_at` 的紀錄，加上「在途且已 canceled」者（SC-06）。
    pub fn pending(&self, _session_id: &str) -> usize {
        s21_todo!("T8", "CommandRegistry::pending")
    }
}

/// 單一 session 的待執行佇列（`BinaryHeap` 實作，依排序鍵出列）。
pub struct SessionQueue {
    _priv: (),
}

impl SessionQueue {
    /// 建立空佇列（骨架：不含狀態）。
    pub fn new() -> SessionQueue {
        SessionQueue { _priv: () }
    }

    /// 入列；物理長度達 `CMD_PENDING_MAX` 時先依登錄表 compact。
    pub fn push(&mut self, _q: QueuedCommand, _reg: &CommandRegistry) {
        s21_todo!("T8", "SessionQueue::push")
    }

    /// 出列最小排序鍵的項目（略過已 remove 的項目）。
    pub fn pop(&mut self) -> Option<QueuedCommand> {
        s21_todo!("T8", "SessionQueue::pop")
    }

    /// 依 cmd_id 移除待執行項目；存在則回 `true`。
    pub fn remove(&mut self, _cmd_id: &str) -> bool {
        s21_todo!("T8", "SessionQueue::remove")
    }

    /// 邏輯長度（未被 remove 的待執行項目數）。
    pub fn len(&self) -> usize {
        s21_todo!("T8", "SessionQueue::len")
    }

    /// 物理長度（含尚未 compact 的惰性項目）。
    pub fn physical_len(&self) -> usize {
        s21_todo!("T8", "SessionQueue::physical_len")
    }

    /// 全部取出（供 flush／關機使用）。
    pub fn drain_all(&mut self) -> Vec<QueuedCommand> {
        s21_todo!("T8", "SessionQueue::drain_all")
    }
}
