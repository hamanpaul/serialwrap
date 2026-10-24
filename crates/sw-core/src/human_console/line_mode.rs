//! human console line 命令的模式判定（P6 Task 3；`service.py:89-142` `_human_console_mode`）。
//!
//! Task 1 骨架：型別與簽章凍結，函式本體為 `todo!("P6 Task 3")`。

/// `line`：一般命令；`interactive`：全螢幕互動程式（開 human lease）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HumanLineMode {
    Line,
    Interactive,
}

impl HumanLineMode {
    /// `"line"` ｜ `"interactive"`。
    pub fn as_str(self) -> &'static str {
        todo!("P6 Task 3")
    }
}

/// `_human_console_mode`：shlex 分詞（失敗退回空白分割）後略過 env 賦值、`--`、`sudo`／`env`／`command`／`builtin`／`exec`
/// 前綴，第一個實際命令的 basename 在 `HUMAN_INTERACTIVE_COMMANDS` 內即 `Interactive`。
pub fn human_console_mode(_command: &str) -> HumanLineMode {
    todo!("P6 Task 3")
}
