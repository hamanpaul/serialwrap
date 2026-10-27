//! 命令仲裁：`CommandRegistry`（admission、記錄、history）與 `SessionQueue`（優先序＋FIFO 出列）。

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, HashSet};
use std::sync::{Arc, Mutex};

use indexmap::IndexMap;
use sw_proto::constants::{CMD_HISTORY_MAX, CMD_PENDING_MAX, CMD_REJECT_BYTES, CMD_WARN_BYTES};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pyre::valid_completion_regex;

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

#[derive(Default)]
struct RegistryInner {
    records: IndexMap<String, PyDict>,
    counter: u64,
    in_flight: HashMap<String, String>,
}

/// 命令登錄表：保留 admission 順序，並追蹤每個 session 的在途命令。
pub struct CommandRegistry {
    inner: Mutex<RegistryInner>,
}

impl CommandRegistry {
    /// 建立空登錄表。
    pub fn new() -> Arc<CommandRegistry> {
        Arc::new(CommandRegistry {
            inner: Mutex::new(RegistryInner::default()),
        })
    }

    /// 依 Python 基線的檢查順序執行 admission，建立命令紀錄與佇列項目。
    pub fn admit(
        &self,
        req: SubmitReq,
        queue_registered: bool,
        cmd_id: String,
        now_iso: &str,
    ) -> Result<Admitted, PyDict> {
        let completion_value = req
            .completion_regex
            .as_ref()
            .map(|pattern| PyValue::from(pattern.clone()));
        if !valid_completion_regex(completion_value.as_ref()) {
            return Err(pydict! {
                "ok" => false,
                "error_code" => "INVALID_COMPLETION_REGEX",
            });
        }

        let cmd_len = req.command.len();
        if cmd_len > CMD_REJECT_BYTES as usize {
            return Err(pydict! {
                "ok" => false,
                "error_code" => "CMD_TOO_LONG",
                "cmd_length" => PyValue::from(cmd_len as i64),
                "limit" => PyValue::from(CMD_REJECT_BYTES),
                "hint" => "Command exceeds 16 KB hard limit. Use file-based injection or split into smaller commands.",
            });
        }

        let warning = (cmd_len > CMD_WARN_BYTES as usize).then(|| {
            pydict! {
                "code" => "CMD_LENGTH_WARNING",
                "cmd_length" => PyValue::from(cmd_len as i64),
                "soft_limit" => PyValue::from(CMD_WARN_BYTES),
                "hint" => "Command exceeds 4 KB soft limit. Long commands may cause UART buffer overflow or prompt timeout.",
            }
        });

        if req.command.contains('\n') {
            return Err(pydict! {
                "ok" => false,
                "error_code" => "CMD_CONTAINS_NEWLINE",
                "hint" => "Command must not contain embedded newline characters. Split into multiple independent submissions.",
            });
        }

        let mut inner = self.inner.lock().expect("命令登錄表鎖 poisoned");
        if !queue_registered {
            return Err(pydict! {
                "ok" => false,
                "error_code" => "SESSION_NOT_READY",
                "session_id" => req.session_id.clone(),
            });
        }

        let pending = pending_locked(&inner, &req.session_id);
        if pending >= CMD_PENDING_MAX {
            return Err(pydict! {
                "ok" => false,
                "error_code" => "SESSION_QUEUE_FULL",
                "session_id" => req.session_id.clone(),
                "pending" => PyValue::from(pending as i64),
                "limit" => PyValue::from(CMD_PENDING_MAX as i64),
                "hint" => "Per-session pending command queue is full (backpressure). Wait for in-flight commands to drain before submitting more.",
            });
        }

        inner.counter += 1;
        let counter = inner.counter;
        let mode = req.mode.as_str();
        let execution_mode = match mode {
            "fg" => "line",
            "bg" => "background",
            other => other,
        };
        let expected_duration = req
            .expected_duration_s
            .map(PyValue::from)
            .unwrap_or(PyValue::Null);
        let completion_regex = req
            .completion_regex
            .as_ref()
            .map(|pattern| PyValue::from(pattern.clone()))
            .unwrap_or(PyValue::Null);

        let mut record = PyDict::new();
        record.insert("cmd_id", PyValue::from(cmd_id.clone()));
        record.insert("session_id", PyValue::from(req.session_id.clone()));
        record.insert("command", PyValue::from(req.command.clone()));
        record.insert("source", PyValue::from(req.source.clone()));
        record.insert("mode", PyValue::from(req.mode.clone()));
        record.insert("execution_mode", PyValue::from(execution_mode));
        record.insert("timeout_s", PyValue::from(req.timeout_s));
        record.insert("expected_duration_s", expected_duration);
        record.insert("completion_regex", completion_regex);
        record.insert("priority", PyValue::from(PyInt::Small(req.priority)));
        record.insert("status", PyValue::from("accepted"));
        record.insert("created_at", PyValue::from(now_iso));
        record.insert("accepted_at", PyValue::from(now_iso));
        record.insert("started_at", PyValue::Null);
        record.insert("done_at", PyValue::Null);
        record.insert("error_code", PyValue::Null);
        record.insert("stdout", PyValue::from(""));
        record.insert("partial", PyValue::from(false));
        record.insert("background_capture_id", PyValue::Null);
        record.insert("interactive_session_id", PyValue::Null);
        record.insert("recovery_action", PyValue::Null);
        inner.records.insert(cmd_id.clone(), record);
        evict_history_locked(&mut inner);
        drop(inner);

        let mut response = pydict! {
            "ok" => true,
            "cmd_id" => cmd_id.clone(),
            "status" => "accepted",
            "session_id" => req.session_id.clone(),
        };
        if let Some(warning) = warning {
            response.insert("warning", PyValue::Dict(warning));
        }

        Ok(Admitted {
            response,
            queued: QueuedCommand {
                sort_key: (req.priority, counter),
                cmd_id,
                req,
            },
        })
    }

    /// worker 取件：只有仍為 accepted 且尚未終結的命令可轉為 running。
    pub fn mark_running(&self, cmd_id: &str, now_iso: &str) -> RunDecision {
        let mut inner = self.inner.lock().expect("命令登錄表鎖 poisoned");
        let session_id = {
            let Some(record) = inner.records.get_mut(cmd_id) else {
                return RunDecision::Skip;
            };
            if record.get("status").and_then(PyValue::as_str) != Some("accepted")
                || record.get_truthy("done_at")
            {
                return RunDecision::Skip;
            }
            let Some(session_id) = record
                .get("session_id")
                .and_then(PyValue::as_str)
                .map(str::to_owned)
            else {
                return RunDecision::Skip;
            };
            record.insert("status", PyValue::from("running"));
            record.insert("started_at", PyValue::from(now_iso));
            session_id
        };
        // 與 running 轉移共用鎖，避免 cancel/history 觀察到尚未登記的在途命令；保留該 session 既有指標。
        inner
            .in_flight
            .entry(session_id)
            .or_insert_with(|| cmd_id.to_string());
        RunDecision::Run
    }

    /// 設定或清除某 session 的在途命令指標。
    pub fn set_in_flight(&self, session_id: &str, cmd_id: Option<&str>) {
        let mut inner = self.inner.lock().expect("命令登錄表鎖 poisoned");
        if let Some(cmd_id) = cmd_id {
            inner
                .in_flight
                .insert(session_id.to_string(), cmd_id.to_string());
        } else {
            inner.in_flight.remove(session_id);
        }
    }

    /// 命令終結；取消中的 worker 收尾只清除在途指標，不覆寫 canceled 紀錄。
    pub fn complete(&self, cmd_id: &str, outcome: CmdOutcome, now_iso: &str) {
        let mut inner = self.inner.lock().expect("命令登錄表鎖 poisoned");
        if let Some(record) = inner.records.get_mut(cmd_id) {
            if record.get("status").and_then(PyValue::as_str) != Some("canceled") {
                match outcome {
                    CmdOutcome::Result(result) => {
                        for key in [
                            "stdout",
                            "partial",
                            "background_capture_id",
                            "interactive_session_id",
                            "recovery_action",
                            "execution_mode",
                        ] {
                            if let Some(value) = result.get(key) {
                                record.insert(key, value.clone());
                            }
                        }

                        let failed = result.get("ok").is_some() && !result.get_truthy("ok");
                        if failed {
                            let error_code = result
                                .get("error_code")
                                .filter(|_| result.get_truthy("error_code"))
                                .cloned()
                                .unwrap_or_else(|| PyValue::from("COMMAND_FAILED"));
                            record.insert("status", PyValue::from("error"));
                            record.insert("error_code", error_code);
                        } else if result.get("status").and_then(PyValue::as_str)
                            == Some("interactive")
                        {
                            record.insert("status", PyValue::from("interactive"));
                        } else {
                            record.insert("status", PyValue::from("done"));
                        }
                    }
                    CmdOutcome::SendFailed => {
                        record.insert("status", PyValue::from("error"));
                        record.insert("error_code", PyValue::from("SEND_FAILED"));
                    }
                    CmdOutcome::Aborted(class) => {
                        let error_code = match class {
                            AbortClass::InFlight => "COMMAND_ABORTED_IN_FLIGHT",
                            AbortClass::NotSubmitted => "FLUSHED_BY_RECOVERY",
                            AbortClass::Unknown => "SEND_FAILED",
                        };
                        record.insert("status", PyValue::from("error"));
                        record.insert("error_code", PyValue::from(error_code));
                    }
                }
                record.insert("done_at", PyValue::from(now_iso));
            }
        }

        inner
            .in_flight
            .retain(|_, active_cmd_id| active_cmd_id != cmd_id);
        evict_history_locked(&mut inner);
    }

    /// 取消命令；取消只更新紀錄，不中止正在執行的 worker。
    pub fn cancel(&self, cmd_id: &str, now_iso: &str) -> PyDict {
        let mut inner = self.inner.lock().expect("命令登錄表鎖 poisoned");
        let Some(record) = inner.records.get_mut(cmd_id) else {
            return pydict! {
                "ok" => false,
                "error_code" => "CMD_NOT_FOUND",
                "cmd_id" => cmd_id,
            };
        };

        let status = record.get("status").and_then(PyValue::as_str);
        if matches!(status, Some("done" | "error")) {
            return pydict! {
                "ok" => false,
                "error_code" => "CMD_NOT_CANCELABLE",
                "cmd_id" => cmd_id,
            };
        }

        record.insert("status", PyValue::from("canceled"));
        record.insert("done_at", PyValue::from(now_iso));
        evict_history_locked(&mut inner);
        pydict! {
            "ok" => true,
            "cmd_id" => cmd_id,
            "status" => "canceled",
        }
    }

    /// 將該 session 尚未啟動的 accepted 命令終結，不在 flush 時淘汰 history。
    pub fn flush_session(&self, session_id: &str, code: &str, now_iso: &str) -> Vec<String> {
        let mut inner = self.inner.lock().expect("命令登錄表鎖 poisoned");
        let mut flushed = Vec::new();
        for (cmd_id, record) in inner.records.iter_mut() {
            let belongs_to_session =
                record.get("session_id").and_then(PyValue::as_str) == Some(session_id);
            let has_done_at = record
                .get("done_at")
                .is_some_and(|value| !matches!(value, PyValue::Null));
            if belongs_to_session
                && !has_done_at
                && record.get("status").and_then(PyValue::as_str) == Some("accepted")
            {
                record.insert("status", PyValue::from("error"));
                record.insert("error_code", PyValue::from(code));
                record.insert("done_at", PyValue::from(now_iso));
                flushed.push(cmd_id.clone());
            }
        }
        flushed
    }

    /// 取得命令紀錄副本。
    pub fn get(&self, cmd_id: &str) -> Option<PyDict> {
        self.inner
            .lock()
            .expect("命令登錄表鎖 poisoned")
            .records
            .get(cmd_id)
            .cloned()
    }

    /// 全部命令的 `(cmd_id, error_code)`（依 admission 順序；無 error_code 為 `None`）。
    /// daemon 停機診斷用（P5-T19：停機前後比對 `FLUSHED_BY_SHUTDOWN` 的命令）。
    pub fn error_codes(&self) -> Vec<(String, Option<String>)> {
        self.inner
            .lock()
            .expect("命令登錄表鎖 poisoned")
            .records
            .iter()
            .map(|(cmd_id, record)| {
                (
                    cmd_id.clone(),
                    record
                        .get("error_code")
                        .and_then(PyValue::as_str)
                        .map(str::to_string),
                )
            })
            .collect()
    }

    /// 目前紀錄總數，包含在途命令。
    pub fn len(&self) -> usize {
        self.inner
            .lock()
            .expect("命令登錄表鎖 poisoned")
            .records
            .len()
    }

    /// 該 session 的 pending 數，包含已取消但 worker 尚未收尾的命令。
    pub fn pending(&self, session_id: &str) -> usize {
        let inner = self.inner.lock().expect("命令登錄表鎖 poisoned");
        pending_locked(&inner, session_id)
    }
}

fn pending_locked(inner: &RegistryInner, session_id: &str) -> usize {
    let mut pending = inner
        .records
        .values()
        .filter(|record| {
            record.get("session_id").and_then(PyValue::as_str) == Some(session_id)
                && !record.get_truthy("done_at")
        })
        .count();

    if let Some(cmd_id) = inner.in_flight.get(session_id) {
        if inner.records.get(cmd_id).is_some_and(|record| {
            record.get("session_id").and_then(PyValue::as_str) == Some(session_id)
                && record.get("status").and_then(PyValue::as_str) == Some("canceled")
        }) {
            pending += 1;
        }
    }
    pending
}

fn evict_history_locked(inner: &mut RegistryInner) {
    if inner.records.len() <= CMD_HISTORY_MAX {
        return;
    }

    let in_flight: HashSet<String> = inner.in_flight.values().cloned().collect();
    let mut completed = Vec::new();
    for (cmd_id, record) in &inner.records {
        if record.get_truthy("done_at") && !in_flight.contains(cmd_id) {
            let done_at = record
                .get("done_at")
                .and_then(PyValue::as_str)
                .unwrap_or_default()
                .to_string();
            completed.push((done_at, cmd_id.clone()));
        }
    }
    completed.sort_by(|left, right| left.0.cmp(&right.0));

    let excess = inner.records.len().saturating_sub(CMD_HISTORY_MAX);
    for (_, cmd_id) in completed.into_iter().take(excess) {
        inner.records.shift_remove(&cmd_id);
    }
}

/// 單一 session 的待執行佇列，以 priority 小者優先、相同 priority 依 admission FIFO 出列。
pub struct SessionQueue {
    heap: BinaryHeap<Reverse<(i128, u64, String)>>,
    queued: HashMap<String, QueuedCommand>,
}

impl SessionQueue {
    /// 建立空佇列。
    pub fn new() -> SessionQueue {
        SessionQueue {
            heap: BinaryHeap::new(),
            queued: HashMap::new(),
        }
    }

    /// 入列；物理長度達 pending 上限時，先移除登錄表中已非 accepted 的項目。
    pub fn push(&mut self, command: QueuedCommand, registry: &CommandRegistry) {
        if self.heap.len() >= CMD_PENDING_MAX {
            self.compact(registry);
        }
        let (priority, counter) = command.sort_key;
        let cmd_id = command.cmd_id.clone();
        self.heap.push(Reverse((priority, counter, cmd_id.clone())));
        self.queued.insert(cmd_id, command);
    }

    /// 出列排序最前的命令，並略過已移除的 heap 項目。
    pub fn pop(&mut self) -> Option<QueuedCommand> {
        while let Some(Reverse((_, _, cmd_id))) = self.heap.pop() {
            if let Some(command) = self.queued.remove(&cmd_id) {
                return Some(command);
            }
        }
        None
    }

    /// 移除指定命令，並同步從 heap 移除其實體項目。
    pub fn remove(&mut self, cmd_id: &str) -> bool {
        if self.queued.remove(cmd_id).is_none() {
            return false;
        }
        self.heap
            .retain(|Reverse((_, _, queued_id))| queued_id != cmd_id);
        true
    }

    /// 尚未被移除的佇列項目數。
    pub fn len(&self) -> usize {
        self.queued.len()
    }

    /// heap 的實體項目數，包含尚未 compact 的惰性項目。
    pub fn physical_len(&self) -> usize {
        self.heap.len()
    }

    /// 依排序順序取出全部尚未移除的命令。
    pub fn drain_all(&mut self) -> Vec<QueuedCommand> {
        std::iter::from_fn(|| self.pop()).collect()
    }

    fn compact(&mut self, registry: &CommandRegistry) {
        self.queued.retain(|cmd_id, _| {
            registry.get(cmd_id).is_some_and(|record| {
                record.get("status").and_then(PyValue::as_str) == Some("accepted")
            })
        });
        self.heap = self
            .queued
            .values()
            .map(|command| {
                Reverse((
                    command.sort_key.0,
                    command.sort_key.1,
                    command.cmd_id.clone(),
                ))
            })
            .collect();
    }
}
