//! Python 格式化與 repr 語意層（P3-T4 stub，行為尚未實作）。
#![allow(unused_variables)]

use crate::pyjson::PyValue;

/// Python float repr；單向轉呼叫 `pyjson::repr_float`。
pub fn py_repr_float(x: f64) -> String {
    todo!("P3-T4: py_repr_float")
}

/// `repr(str)`：引號選擇、跳脫字元、`isprintable` 規則。
pub fn py_repr_str(s: &str) -> String {
    todo!("P3-T4: py_repr_str")
}

/// `str(v)`：None→"None"、True→"True"、float→repr、str 原樣、list/dict→repr。
pub fn py_str(v: &PyValue) -> String {
    todo!("P3-T4: py_str")
}

/// `repr(v)`：dict 為 `{'k': v}`、list 為 `[a, b]`。
pub fn py_repr(v: &PyValue) -> String {
    todo!("P3-T4: py_repr")
}
