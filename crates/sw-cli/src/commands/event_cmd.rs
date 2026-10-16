//! `event` 群組（`cli.py:965-1078`）：P3-T15 紅燈 stub。
#![allow(unused_variables)]

use crate::argparse::Namespace;
use crate::ctx::CliCtx;
use sw_proto::pyjson::PyValue;

pub const EVENT_CMD_METHOD: [(&str, &str); 10] = [
    ("add", "event.rule_set"),
    ("rm", "event.rule_delete"),
    ("list", "event.rule_list"),
    ("show", "event.rule_get"),
    ("enable", "event.com_enable"),
    ("disable", "event.com_disable"),
    ("status", "event.com_status"),
    ("reset", "event.reset"),
    ("reload", "event.reload"),
    ("tail", "event.tail"),
];

/// `Err` 為 INTERNAL_ERROR message。
pub fn event_params(ctx: &CliCtx, ns: &Namespace) -> Result<PyValue, String> {
    todo!("P3-T15: event_params")
}

pub fn dispatch_event(ctx: &mut CliCtx, ns: &Namespace) -> i32 {
    todo!("P3-T15: dispatch_event")
}
