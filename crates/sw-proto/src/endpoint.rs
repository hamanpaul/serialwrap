//! RPC endpoint 型別與解析（P3-T7 stub）。
#![allow(unused_variables, dead_code)]

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Endpoint {
    Unix(String),
    Tcp { host: String, port: u16 },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EndpointError(pub String);

pub fn parse_endpoint(s: &str) -> Result<Endpoint, EndpointError> {
    todo!("P3-T7: parse_endpoint")
}

pub fn is_loopback_tcp(s: &str) -> bool {
    todo!("P3-T7: is_loopback_tcp")
}
