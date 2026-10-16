//! CPython 3.12.3 argparse 相容層（P3-T10 紅燈 stub）。
//!
//! 方言固定為 CPython 3.12.3（§0.7、arch §7.2）。子模組 `parser`／`help`／`usage`／`error` 於 P3-T10 承接。
#![allow(unused_variables, dead_code)]

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
        todo!("P3-T10: Namespace::get")
    }
    pub fn str(&self, dest: &str) -> Option<&str> {
        todo!("P3-T10: Namespace::str")
    }
    /// `["session","list"]`：`cmd` → `<group>_cmd` 的值。
    pub fn path(&self) -> Vec<&str> {
        todo!("P3-T10: Namespace::path")
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
    todo!("P3-T10: parse_args")
}

pub fn format_help(spec: &ParserSpec, width: usize) -> String {
    todo!("P3-T10: format_help")
}

pub fn format_usage(spec: &ParserSpec, width: usize) -> String {
    todo!("P3-T10: format_usage")
}

/// `shutil.get_terminal_size`：`int(COLUMNS) > 0` 才採，否則 terminal，否則 80；再減 2（HelpFormatter）。
pub fn help_width(columns_env: Option<&str>, terminal: Option<u16>) -> usize {
    todo!("P3-T10: help_width")
}
