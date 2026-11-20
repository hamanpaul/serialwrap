//! JSON 值的 Python `==`／`<` 語意（P7 Task 13：registry `diff_against` 的 `raw !=`、event_log `tail` 的過濾）。
//! bool 為 int、int 與 float 精確比較（不經 f64 轉換）、dict 比較不看鍵序；`<` 不支援的型別組合回 TypeError 訊息。

use std::cmp::Ordering;

use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::{PyInt, PyValue};

use super::schema::{float_to_int, type_name};

enum Num {
    Int(PyInt),
    Float(f64),
}

fn num(v: &PyValue) -> Option<Num> {
    match v {
        PyValue::Bool(b) => Some(Num::Int(PyInt::Small(i128::from(*b)))),
        PyValue::Int(i) => Some(Num::Int(i.clone())),
        PyValue::Float(f) => Some(Num::Float(*f)),
        _ => None,
    }
}

/// `(負號, 去號十進位)`；`Big` 已為正規化字串。
fn sign_digits(i: &PyInt) -> (bool, String) {
    let s = match i {
        PyInt::Small(n) => n.to_string(),
        PyInt::Big(s) => s.clone(),
    };
    match s.strip_prefix('-') {
        Some(d) => (true, d.to_string()),
        None => (false, s),
    }
}

fn cmp_int(a: &PyInt, b: &PyInt) -> Ordering {
    if let (PyInt::Small(x), PyInt::Small(y)) = (a, b) {
        return x.cmp(y);
    }
    let ((na, da), (nb, db)) = (sign_digits(a), sign_digits(b));
    let mag = da.len().cmp(&db.len()).then_with(|| da.cmp(&db));
    match (na, nb) {
        (false, false) => mag,
        (true, true) => mag.reverse(),
        (true, false) => Ordering::Less,
        (false, true) => Ordering::Greater,
    }
}

/// int 與有限 float 的精確比較：先比 `trunc(f)` 的整數值，相等時看小數部分。
fn cmp_int_float(i: &PyInt, f: f64) -> Option<Ordering> {
    if f.is_nan() {
        return None;
    }
    if f.is_infinite() {
        return Some(if f > 0.0 {
            Ordering::Less
        } else {
            Ordering::Greater
        });
    }
    let t = f.trunc();
    let ti = float_to_int(t).ok()?;
    Some(cmp_int(i, &ti).then(if f > t {
        Ordering::Less
    } else if f < t {
        Ordering::Greater
    } else {
        Ordering::Equal
    }))
}

fn cmp_num(a: &Num, b: &Num) -> Option<Ordering> {
    match (a, b) {
        (Num::Int(x), Num::Int(y)) => Some(cmp_int(x, y)),
        (Num::Float(x), Num::Float(y)) => x.partial_cmp(y),
        (Num::Int(x), Num::Float(y)) => cmp_int_float(x, *y),
        (Num::Float(x), Num::Int(y)) => cmp_int_float(y, *x).map(Ordering::reverse),
    }
}

/// 容器元素比較（`PyObject_RichCompareBool`）：同一物件直接相等。JSON 解出的 NaN 為 `json.decoder` 模組層單例，
/// 故容器內 NaN 與 NaN 相等；頂層純量 `NaN != NaN` 仍不相等（審查 76de0cf_96b28dd c58e8d4-1）。
fn py_eq_elem(a: &PyValue, b: &PyValue) -> bool {
    match (a, b) {
        (PyValue::Float(x), PyValue::Float(y)) if x.is_nan() && y.is_nan() => true,
        _ => py_eq(a, b),
    }
}

/// Python `a == b`。
pub(crate) fn py_eq(a: &PyValue, b: &PyValue) -> bool {
    if let (Some(x), Some(y)) = (num(a), num(b)) {
        return cmp_num(&x, &y) == Some(Ordering::Equal);
    }
    match (a, b) {
        (PyValue::Null, PyValue::Null) => true,
        (PyValue::Str(x), PyValue::Str(y)) => x == y,
        (PyValue::List(x), PyValue::List(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| py_eq_elem(p, q))
        }
        (PyValue::Dict(x), PyValue::Dict(y)) => {
            x.0.len() == y.0.len()
                && x.0
                    .iter()
                    .all(|(k, v)| y.0.get(k).is_some_and(|w| py_eq_elem(v, w)))
        }
        _ => false,
    }
}

fn lt_unsupported(a: &PyValue, b: &PyValue) -> PyErr {
    PyErr {
        message: format!(
            "'<' not supported between instances of '{}' and '{}'",
            type_name(a),
            type_name(b)
        ),
    }
}

/// Python `a < b`（JSON 值）；list 依字典序（先找第一個不相等元素）。
pub(crate) fn py_lt(a: &PyValue, b: &PyValue) -> Result<bool, PyErr> {
    if let (Some(x), Some(y)) = (num(a), num(b)) {
        return Ok(cmp_num(&x, &y) == Some(Ordering::Less));
    }
    match (a, b) {
        (PyValue::Str(x), PyValue::Str(y)) => Ok(x < y),
        (PyValue::List(x), PyValue::List(y)) => {
            for (p, q) in x.iter().zip(y) {
                if !py_eq_elem(p, q) {
                    return py_lt(p, q);
                }
            }
            Ok(x.len() < y.len())
        }
        _ => Err(lt_unsupported(a, b)),
    }
}

/// Python `time.time()`：wall clock 奈秒轉 double 秒（`_PyTime_AsSecondsDouble`）。
pub(crate) fn py_time_s(clock: &dyn sw_pal::traits::Clock) -> f64 {
    clock.wall_unix_us().saturating_mul(1000) as f64 / 1e9
}

/// Python `int(time.time() * 1000)`（向零截斷）。
pub(crate) fn py_time_ms(clock: &dyn sw_pal::traits::Clock) -> i64 {
    (py_time_s(clock) * 1000.0) as i64
}
