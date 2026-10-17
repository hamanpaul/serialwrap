//! CPython 3.12.3 argparse 相容層。

pub mod error;
pub mod help;
pub mod parser;
pub mod usage;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Nargs {
    One,
    ZeroOrMore,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ActionKind {
    Store,
    StoreTrue,
    Count,
    Append,
    Version,
    Help,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Conv {
    Str,
    Int,
    Float,
}

#[derive(Clone, Debug, PartialEq)]
pub enum ArgValue {
    None,
    Str(String),
    Int(sw_proto::pyjson::PyInt),
    Float(f64),
    Bool(bool),
    Count(i64),
    List(Vec<String>),
}

#[derive(Clone, Debug, PartialEq)]
pub struct ActionSpec {
    pub option_strings: Vec<&'static str>,
    pub dest: &'static str,
    pub kind: ActionKind,
    pub nargs: Nargs,
    pub conv: Conv,
    pub default: ArgValue,
    pub choices: Option<Vec<&'static str>>,
    pub required: bool,
    /// `%(default)s` 已展開。
    pub help: Option<String>,
    pub metavar: Option<&'static str>,
    pub version: Option<String>,
}

pub struct MutexSpec {
    pub required: bool,
    pub dests: Vec<&'static str>,
}

pub struct SubparserEntry {
    pub name: &'static str,
    pub help: Option<&'static str>,
    pub target: SubTarget,
}

pub enum SubTarget {
    Native(Box<ParserSpec>),
    Delegated,
}

pub struct SubparsersSpec {
    pub dest: &'static str,
    pub required: bool,
    pub title: Option<&'static str>,
    pub metavar: Option<&'static str>,
    pub entries: Vec<SubparserEntry>,
}

pub struct ParserSpec {
    pub prog: String,
    pub description: Option<&'static str>,
    pub epilog: Option<&'static str>,
    pub raw_text: bool,
    /// 只讓 description 與 epilog 保持原樣（Python `RawDescriptionHelpFormatter`）；參數 help 仍依 `raw_text` 折行。
    pub raw_description: bool,
    pub add_help: bool,
    pub allow_abbrev: bool,
    pub actions: Vec<ActionSpec>,
    pub mutex: Vec<MutexSpec>,
    pub sub: Option<SubparsersSpec>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Namespace {
    pub vals: indexmap::IndexMap<String, ArgValue>,
}

impl Namespace {
    pub fn get(&self, dest: &str) -> &ArgValue {
        static NONE: ArgValue = ArgValue::None;
        self.vals.get(dest).unwrap_or(&NONE)
    }
    pub fn str(&self, dest: &str) -> Option<&str> {
        match self.get(dest) {
            ArgValue::Str(value) => Some(value),
            _ => None,
        }
    }
    /// `["session","list"]`：`cmd` → `<group>_cmd` 的值。
    pub fn path(&self) -> Vec<&str> {
        let Some(group) = self.str("cmd") else {
            return Vec::new();
        };
        let mut path = vec![group];
        let dest = format!("{}_cmd", group.replace('-', "_"));
        if let Some(command) = self.str(&dest) {
            path.push(command);
        }
        path
    }
}

pub struct ExitRequest {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

pub enum ParseResult {
    Ok(Namespace),
    Exit(ExitRequest),
    Delegated,
}

pub fn parse_args(spec: &ParserSpec, argv: &[String], width: usize) -> ParseResult {
    parser::parse_args(spec, argv, width)
}

pub fn format_help(spec: &ParserSpec, width: usize) -> String {
    help::format_help(spec, width)
}

pub fn format_usage(spec: &ParserSpec, width: usize) -> String {
    usage::format_usage(spec, width)
}

/// `shutil.get_terminal_size`：`int(COLUMNS) > 0` 才採，否則 terminal，否則 80；再減 2（HelpFormatter）。
pub fn help_width(columns_env: Option<&str>, terminal: Option<u16>) -> usize {
    let columns = columns_env
        .and_then(sw_proto::pycompat::py_int_parse)
        .and_then(|value| match value {
            sw_proto::pyjson::PyInt::Small(value) if value > 0 => {
                Some(usize::try_from(value).unwrap_or(usize::MAX))
            }
            sw_proto::pyjson::PyInt::Big(value) if !value.starts_with('-') && value != "0" => {
                Some(value.parse::<usize>().unwrap_or(usize::MAX))
            }
            _ => None,
        })
        .or_else(|| terminal.map(usize::from))
        .unwrap_or(80);
    columns.saturating_sub(2).max(1)
}

pub(super) fn display_actions(spec: &ParserSpec) -> Vec<ActionSpec> {
    let mut actions = Vec::with_capacity(spec.actions.len() + usize::from(spec.add_help));
    if spec.add_help {
        let help = spec
            .actions
            .iter()
            .find(|action| action.kind == ActionKind::Help)
            .cloned()
            .unwrap_or(ActionSpec {
                option_strings: vec!["-h", "--help"],
                dest: "help",
                kind: ActionKind::Help,
                nargs: Nargs::One,
                conv: Conv::Str,
                default: ArgValue::None,
                choices: None,
                required: false,
                help: Some("show this help message and exit".to_string()),
                metavar: None,
                version: None,
            });
        actions.push(help);
    }
    actions.extend(
        spec.actions
            .iter()
            .filter(|action| action.kind != ActionKind::Help)
            .cloned(),
    );
    actions
}

pub(super) fn action_metavar(action: &ActionSpec) -> String {
    if let Some(metavar) = action.metavar {
        return metavar.to_string();
    }
    if let Some(choices) = &action.choices {
        return format!("{{{}}}", choices.join(","));
    }
    if action.option_strings.is_empty() {
        action.dest.to_string()
    } else {
        action.dest.to_ascii_uppercase()
    }
}

pub(super) fn py_repr(value: &str) -> String {
    sw_proto::pyfmt::py_repr_argv(value)
}
