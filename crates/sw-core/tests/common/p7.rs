//! P7（08-slice23-login-events.md）測試共用輔助：讀 `contracts/golden/p7/*.ndjson`、規則 JSON 建構與規則檔 dump 選項。
#![allow(dead_code)]

use sw_proto::pyjson::{self, DumpOpts, PyDict, PyValue, Sep};

/// 一列 golden（`id` 欄位為 `<file>:<nnn>`）。
pub struct Row(pub PyDict);

impl Row {
    pub fn id(&self) -> &str {
        self.str("id")
    }
    pub fn get(&self, k: &str) -> &PyValue {
        self.0
            .get(k)
            .unwrap_or_else(|| panic!("golden 欄位 {k} 不存在"))
    }
    pub fn str(&self, k: &str) -> &str {
        self.get(k)
            .as_str()
            .unwrap_or_else(|| panic!("golden 欄位 {k} 不是字串"))
    }
    pub fn opt_str(&self, k: &str) -> Option<&str> {
        self.0.get(k).and_then(PyValue::as_str)
    }
    pub fn bool(&self, k: &str) -> bool {
        self.get(k)
            .as_bool()
            .unwrap_or_else(|| panic!("golden 欄位 {k} 不是 bool"))
    }
}

impl std::ops::Index<&str> for Row {
    type Output = PyValue;
    fn index(&self, k: &str) -> &PyValue {
        self.get(k)
    }
}

pub fn golden_rows(name: &str) -> Vec<Row> {
    let path = super::repo_path(&format!("contracts/golden/p7/{name}.ndjson"));
    let text =
        std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("讀取 {}：{e}", path.display()));
    text.lines()
        .map(|l| match pyjson::loads(l).expect("golden 行須為 JSON") {
            PyValue::Dict(d) => Row(d),
            other => panic!("golden 行須為 object：{other:?}"),
        })
        .collect()
}

/// `json.dump(raw, ensure_ascii=False, indent=2, sort_keys=True)`（registry 規則檔格式）。
pub const RULE_FILE: DumpOpts = DumpOpts {
    sep: Sep::Indent(2),
    ensure_ascii: false,
    sort_keys: true,
    allow_nan: true,
};

/// 以合法預設規則為底，覆寫 `fragment`（JSON object）中的頂層鍵。
pub fn rule_json(fragment: &str) -> PyValue {
    let mut base = match pyjson::loads(
        r#"{"schema_version":1,"owner":"o","name":"n","kind":"tool","selectors":["COM0"],
            "pattern":{"kind":"contains","value":"x"},"handler":{"exec":["/bin/true"]}}"#,
    )
    .unwrap()
    {
        PyValue::Dict(d) => d,
        _ => unreachable!(),
    };
    if let PyValue::Dict(over) = pyjson::loads(fragment).expect("fragment 須為 JSON") {
        base.update(&over);
    }
    PyValue::Dict(base)
}

impl Row {
    pub fn opt_bool(&self, k: &str) -> Option<bool> {
        self.0.get(k).and_then(PyValue::as_bool)
    }
    pub fn str_vec(&self, k: &str) -> Vec<String> {
        self.get(k)
            .as_array()
            .unwrap_or_else(|| panic!("golden 欄位 {k} 不是 list"))
            .iter()
            .map(|v| v.as_str().expect("list 元素須為字串").to_string())
            .collect()
    }
    /// `[[k, v], ...]` → `Vec<(String, String)>`。
    pub fn pairs(&self, k: &str) -> Vec<(String, String)> {
        self.get(k)
            .as_array()
            .unwrap_or_else(|| panic!("golden 欄位 {k} 不是 list"))
            .iter()
            .map(|pair| {
                let pair = pair.as_array().expect("pair 須為 list");
                (
                    pair[0].as_str().expect("key").to_string(),
                    pair[1].as_str().expect("value").to_string(),
                )
            })
            .collect()
    }
}

/// golden 的 base64 欄位解碼（`binascii.a2b_base64`）。
pub fn b64(s: &str) -> Vec<u8> {
    sw_proto::pycompat::a2b_base64_lenient(s.as_bytes()).expect("golden base64")
}

fn opt_string(v: Option<&PyValue>) -> Option<String> {
    v.and_then(PyValue::as_str).map(str::to_string)
}

fn float(v: &PyValue) -> f64 {
    match v {
        PyValue::Float(f) => *f,
        PyValue::Int(_) => v.as_i64().expect("int") as f64,
        other => panic!("不是數值：{other:?}"),
    }
}

fn pyint(v: &PyValue) -> sw_proto::pyjson::PyInt {
    match v {
        PyValue::Int(i) => i.clone(),
        other => panic!("不是 int：{other:?}"),
    }
}

/// `capture_p7_goldens.profile_json(sp)` 的反向：golden `profile` 物件 → `SessionProfile`。
pub fn profile_from_json(v: &PyValue) -> sw_proto::config_types::SessionProfile {
    use sw_proto::config_types::{ProfileTemplate, SessionProfile, UartProfile};
    let d = match v {
        PyValue::Dict(d) => d,
        other => panic!("profile 須為 object：{other:?}"),
    };
    let g = |k: &str| d.get(k).unwrap_or_else(|| panic!("profile 欄位 {k} 不存在"));
    let s = |k: &str| g(k).as_str().unwrap_or_else(|| panic!("profile 欄位 {k} 不是字串")).to_string();
    let uart = g("uart");
    let u = |k: &str| uart.get(k).unwrap_or_else(|| panic!("uart 欄位 {k} 不存在"));
    SessionProfile {
        com: s("com"),
        act_no: pyint(g("act_no")),
        alias: s("alias"),
        device_by_id: s("device_by_id"),
        template: ProfileTemplate {
            profile_name: s("profile_name"),
            platform: s("platform"),
            prompt_regex: s("prompt_regex"),
            login_regex: s("login_regex"),
            password_regex: s("password_regex"),
            post_login_cmd: s("post_login_cmd"),
            ready_probe: s("ready_probe"),
            username: opt_string(d.get("username")),
            user_env: opt_string(d.get("user_env")),
            pass_env: opt_string(d.get("pass_env")),
            env_file: opt_string(d.get("env_file")),
            console_unlock_env: opt_string(d.get("console_unlock_env")),
            console_unlock_idle_s: float(g("console_unlock_idle_s")),
            timeout_s: float(g("timeout_s")),
            quiet_window_s: float(g("quiet_window_s")),
            hard_timeout_s: float(g("hard_timeout_s")),
            log_dir: opt_string(d.get("log_dir")),
            uart: UartProfile {
                baud: pyint(u("baud")),
                data_bits: pyint(u("data_bits")),
                parity: u("parity").as_str().expect("parity").to_string(),
                stop_bits: pyint(u("stop_bits")),
                flow_control: u("flow_control").as_str().expect("flow_control").to_string(),
                xonxoff: u("xonxoff").as_bool().expect("xonxoff"),
            },
            bootloader_prompts: g("bootloader_prompts")
                .as_array()
                .expect("bootloader_prompts")
                .iter()
                .map(|p| p.as_str().expect("prompt").to_string())
                .collect(),
            max_console_line_chars: match g("max_console_line_chars") {
                PyValue::Null => None,
                other => Some(pyint(other)),
            },
        },
    }
}

/// golden `environ` 物件 → `FakeEnv`（`HOME` 等鍵照原樣）。
pub fn fake_env(v: &PyValue) -> sw_pal::fakes::FakeEnv {
    let env = sw_pal::fakes::FakeEnv::default();
    if let PyValue::Dict(d) = v {
        let mut vars = env.vars.lock().unwrap();
        for (k, val) in &d.0 {
            vars.insert(k.clone(), val.as_str().expect("environ 值須為字串").to_string());
        }
    }
    env
}

/// golden `files` 物件（`{path: {"b64": ...}}`）→ `read_env_file` 替身：缺檔 `None`、非 UTF-8 `Some(Err(InvalidData))`。
pub fn fake_read(v: &PyValue) -> impl Fn(&str) -> Option<std::io::Result<String>> {
    let mut files = std::collections::BTreeMap::new();
    if let PyValue::Dict(d) = v {
        for (k, val) in &d.0 {
            let data = b64(val.get("b64").and_then(PyValue::as_str).expect("files[].b64"));
            files.insert(k.clone(), data);
        }
    }
    move |path: &str| {
        files.get(path).map(|data| {
            String::from_utf8(data.clone())
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))
        })
    }
}

const DEFAULT_PROFILE_DIR: &str = "/etc/serialwrap/profiles";

struct DefaultYamlFs(String);

impl sw_proto::config_types::ProfileFs for DefaultYamlFs {
    fn flavor(&self) -> sw_proto::pypath::PathFlavor {
        sw_proto::pypath::PathFlavor::Posix
    }
    fn is_dir(&self, p: &str) -> bool {
        p == DEFAULT_PROFILE_DIR
    }
    fn list_dir(&self, _p: &str) -> Result<Vec<String>, sw_proto::pycompat::PyOsError> {
        Ok(vec!["default.yaml".to_string()])
    }
    fn read_bytes(&self, _p: &str) -> Result<Vec<u8>, sw_proto::pycompat::PyOsError> {
        Ok(self.0.clone().into_bytes())
    }
    fn expanduser(&self, p: &str) -> String {
        p.to_string()
    }
}

/// 出貨 `sw_core/assets/profiles/default.yaml` 的模板（`capture_p7_goldens._templates()`）。
pub fn default_templates() -> Vec<sw_proto::config_types::ProfileTemplate> {
    let path = super::repo_path("sw_core/assets/profiles/default.yaml");
    let text = std::fs::read_to_string(&path).expect("default.yaml");
    sw_proto::config_types::load_profiles(DEFAULT_PROFILE_DIR, &DefaultYamlFs(text))
        .expect("default.yaml 應可載入")
        .templates
}

pub fn default_template(name: &str) -> sw_proto::config_types::ProfileTemplate {
    default_templates()
        .into_iter()
        .find(|t| t.profile_name == name)
        .unwrap_or_else(|| panic!("default.yaml 無模板 {name}"))
}

/// `capture_p7_goldens.session_profile(template)`：COM0／act_no 1／alias lab+1／fake by-id。
pub fn session_profile_for(name: &str) -> sw_proto::config_types::SessionProfile {
    sw_proto::config_types::SessionProfile {
        com: "COM0".into(),
        act_no: sw_proto::pyjson::PyInt::Small(1),
        alias: "lab+1".into(),
        device_by_id: "/dev/serial/by-id/fake".into(),
        template: default_template(name),
    }
}
