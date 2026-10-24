//! DeferredBuffer（P6 Task 4；BC-02、07 D-02／D-07／D-15／D-22）。
//!
//! 型別為 `pub` 供 L1 測試直接驗證；產品中只作為 `SessionCore` 的私有欄位存在，
//! 其他 Phase 只能經 `SessionCore::invalidate_human_input` 影響它（07 D-02）。
//! Task 1 骨架：型別與簽章凍結，函式本體為 `todo!("P6 Task 4")`。

use indexmap::IndexMap;

/// 單一 client 的 deferred 位元組（以 `client_id` 為 `IndexMap` key，保持首次插入序）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeferredEntry {
    pub client_lifetime: u64,
    pub input_epoch: u64,
    pub bytes: Vec<u8>,
}

/// `append` 的結果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeferOutcome {
    /// 附加成功；`len` 為該 entry 附加後的長度。
    Appended { len: usize },
    /// 擷取時的 `input_epoch` 不等於現值：不進 buffer。
    StaleEpoch,
    /// 附加後超過上限：刪除整個 entry（連同本次 bytes）；`dropped` 為刪除的 bytes 數（07 D-07）。
    Overflowed { dropped: usize },
}

/// BC-02 失效點（07 D-02）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InvalidateReason {
    BootBanner,
    QuietRearm,
    RebootCommand,
    Recovery,
    Release,
    Flash,
}

impl InvalidateReason {
    /// WARNING 模板用的 reason：`boot_banner`｜`quiet_rearm`｜`reboot_command`｜`recovery`｜`release`｜`flash`。
    pub fn as_str(self) -> &'static str {
        todo!("P6 Task 4")
    }
}

/// 一筆待回放的 client 輸入（`source` 為 `"human:<client_id>"`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Replay {
    pub client_id: String,
    pub client_lifetime: u64,
    pub source: String,
    pub bytes: Vec<u8>,
}

/// 每 client 有上限的 deferred 輸入帳本。
#[derive(Debug)]
pub struct DeferredBuffer {
    #[allow(dead_code)]
    entries: IndexMap<String, DeferredEntry>,
    #[allow(dead_code)]
    cap: usize,
}

impl DeferredBuffer {
    pub fn new(_cap: usize) -> Self {
        todo!("P6 Task 4")
    }

    /// `input_epoch` 為擷取當下的值，`current_epoch` 為現值；不等即 `StaleEpoch`。
    pub fn append(
        &mut self,
        _client_id: &str,
        _client_lifetime: u64,
        _input_epoch: u64,
        _current_epoch: u64,
        _bytes: &[u8],
    ) -> DeferOutcome {
        todo!("P6 Task 4")
    }

    /// 清空全部 entry；回傳丟棄的 bytes 總數。
    pub fn invalidate_all(&mut self) -> usize {
        todo!("P6 Task 4")
    }

    /// detach／drop／reap／lease_dropped：刪除該 client 的 entry；回傳丟棄 bytes 數。
    pub fn remove_client(&mut self, _client_id: &str) -> usize {
        todo!("P6 Task 4")
    }

    /// 依首次插入序取出全部可回放項目並清空所有 entry（只回 `input_epoch == current_epoch`、client 存活且非空者）。
    pub fn take_replay(&mut self, _current_epoch: u64, _is_live: &dyn Fn(&str, u64) -> bool) -> Vec<Replay> {
        todo!("P6 Task 4")
    }

    /// 07 D-15(4)：只取該 client 的回放項目；其餘 entry 順序不變。
    pub fn take_replay_for(
        &mut self,
        _client_id: &str,
        _current_epoch: u64,
        _is_live: &dyn Fn(&str, u64) -> bool,
    ) -> Option<Replay> {
        todo!("P6 Task 4")
    }

    /// 07 D-15(3)／D-22(4)：被撤銷或 `NotSubmitted` 的回放放回前端。`items` 依原送出順序、各帶擷取時的 `input_epoch`；
    /// epoch 不等於 `current_epoch` 者丟棄；其餘依序排在所有既有 entry 之前，同 client 已有 entry 時 bytes 接在該 entry
    /// 前面並把 entry 移到該位置；合併後長度 > cap 依 D-07 刪除整個 entry。回傳丟棄的 bytes 總數。
    pub fn restore_front(&mut self, _items: Vec<(Replay, u64)>, _current_epoch: u64) -> usize {
        todo!("P6 Task 4")
    }

    pub fn total_bytes(&self) -> usize {
        todo!("P6 Task 4")
    }

    pub fn len_for(&self, _client_id: &str) -> usize {
        todo!("P6 Task 4")
    }
}
