"""P7 Task 1：`login_fsm_scenarios` golden 的 DSL 案例（`p7_fake_bridge.ScriptedBridge` 與 Rust `LoginRig` 共用）。

每案：`entry`（ensure_ready｜probe_ready｜detect_template）、`template`（default.yaml 模板名；None＝SessionProfile 預設值）、
`profile_over`（覆寫欄位）、`auth`（SessionAuth 欄位或 None）、`env`（os.environ）、其餘為 DSL 欄位。
"""
from __future__ import annotations

from typing import Any

PRPL = "\r\nroot@prplOS:~# "
SHELL = "\r\nroot@op3:~# "
S4R = "\r\nroot@Speedport Smart 4 Typ A:~# "
NONCE_OK_PRPL = {"on": "prefix", "match": "echo __READY__", "rx": "\r\n__READY__{nonce}" + PRPL}
AUTH = {"username": "root", "password": "pw"}


def _c(entry: str, template: str | None, note: str, **kw: Any) -> dict[str, Any]:
    case: dict[str, Any] = {"entry": entry, "template": template, "note": note, "initial_rx": "", "reactions": [],
                            "nonces": ["deadbeef"], "rx_bytes_10s": 0, "idle_s": 0.0, "env": {}, "auth": None, "profile_over": {}}
    case.update(kw)
    return case


def login_cases() -> list[dict[str, Any]]:
    on_empty = lambda rx: {"on": "eq", "match": "", "rx": rx}  # noqa: E731
    cases: list[dict[str, Any]] = [
        # ── prpl：probe_ready ──
        _c("probe_ready", "prpl-template", "prpl 成功", reactions=[on_empty(PRPL), NONCE_OK_PRPL]),
        _c("probe_ready", "prpl-template", "無 prompt → PROMPT_UNAVAILABLE"),
        _c("probe_ready", "prpl-template", "看到 login → LOGIN_REQUIRED", reactions=[on_empty("\r\nlogin: ")]),
        _c("probe_ready", "prpl-template", "ANSI 色彩 login → LOGIN_REQUIRED（clean_text 後比對）",
           reactions=[on_empty("\r\n\x1b[1m(none) login:\x1b[0m")], profile_over={"login_regex": "(?mi)^.*login:\\s*$"}),
        _c("probe_ready", "prpl-template", "flood 19999 → 不反分類", initial_rx="garbage", rx_bytes_10s=19999),
        _c("probe_ready", "prpl-template", "flood 20000 → RX_FLOOD", initial_rx="garbage", rx_bytes_10s=20000),
        _c("probe_ready", "prpl-template", "LOGIN_REQUIRED 不被 flood 遮蔽", reactions=[on_empty("\r\nlogin: ")], rx_bytes_10s=50000),
        _c("probe_ready", "prpl-template", "nonce 逾時 → READY_NONCE_TIMEOUT", reactions=[on_empty(PRPL)]),
        _c("probe_ready", "prpl-template", "nonce 逾時＋flood → RX_FLOOD", reactions=[on_empty(PRPL)], rx_bytes_10s=20000),
        _c("probe_ready", "prpl-template", "nonce 逾時後 tail 為 login → LOGIN_REQUIRED",
           reactions=[on_empty(PRPL), {"on": "prefix", "match": "echo __READY__", "rx": "\r\nlogin: "}]),
        _c("probe_ready", "prpl-template", "nonce 後 prompt 不在行尾 → READY_PROMPT_TIMEOUT",
           reactions=[on_empty(PRPL), {"on": "prefix", "match": "echo __READY__", "rx": "\r\n__READY__{nonce}\r\n"}],
           profile_over={"prompt_regex": "(?m)^root@prplOS:.*# \\Z"}),
        _c("probe_ready", "prpl-template", "無效 prompt_regex → raised error", initial_rx="root@prplOS:~# ",
           profile_over={"prompt_regex": "("}),
        _c("probe_ready", "prpl-template", "prompt 於 9.5 s 後才出現（timeout 10 內）→ 成功",
           reactions=[{"on": "eq", "match": "", "rx": PRPL, "after_s": 9.5}, NONCE_OK_PRPL]),
        _c("probe_ready", "prpl-template", "prompt 於 10.5 s 後才出現（逾 timeout）→ PROMPT_UNAVAILABLE",
           reactions=[{"on": "eq", "match": "", "rx": PRPL, "after_s": 10.5}]),
        _c("probe_ready", "prpl-template", "nonce 不足 8 碼時以 uuid hex 前 8 碼", reactions=[on_empty(PRPL), NONCE_OK_PRPL],
           nonces=["0a1b2c3d4e5f"]),
        # ── prpl：ensure_ready ──
        _c("ensure_ready", "prpl-template", "已在 prompt → 不登入", reactions=[on_empty(PRPL), NONCE_OK_PRPL], auth=AUTH),
        _c("ensure_ready", "prpl-template", "完整登入", auth=AUTH, reactions=[
            on_empty("\r\nlogin: "), {"on": "eq", "match": "root", "rx": "\r\nPassword: "}, {"on": "secret", "rx": PRPL}, NONCE_OK_PRPL]),
        _c("ensure_ready", "prpl-template", "PASSWORD 大寫亦匹配", auth=AUTH, reactions=[
            on_empty("\r\nlogin: "), {"on": "eq", "match": "root", "rx": "\r\nPASSWORD: "}, {"on": "secret", "rx": PRPL}, NONCE_OK_PRPL]),
        _c("ensure_ready", "prpl-template", "無 login prompt → LOGIN_PROMPT_TIMEOUT", auth=AUTH),
        _c("ensure_ready", "prpl-template", "LOGIN_PROMPT_TIMEOUT＋flood → RX_FLOOD", auth=AUTH, rx_bytes_10s=25000),
        _c("ensure_ready", "prpl-template", "LOGIN_PROMPT_TIMEOUT＋flood 19999", auth=AUTH, rx_bytes_10s=19999),
        _c("ensure_ready", "prpl-template", "pass_env 宣告但無 user → LOGIN_USER_REQUIRED", reactions=[on_empty("\r\nlogin: ")],
           profile_over={"pass_env": "SW_P"}),
        _c("ensure_ready", "prpl-template", "username 欄位、無 pass_env → PASS_ENV_REQUIRED",
           reactions=[on_empty("\r\nlogin: "), {"on": "eq", "match": "admin", "rx": "\r\nPassword: "}], profile_over={"username": "admin"}),
        _c("ensure_ready", "prpl-template", "pass_env 宣告但 env 缺 → PASS_ENV_MISSING",
           reactions=[on_empty("\r\nlogin: "), {"on": "eq", "match": "admin", "rx": "\r\nPassword: "}],
           profile_over={"username": "admin", "pass_env": "SW_P"}),
        _c("ensure_ready", "prpl-template", "env 補齊密碼（auth 無密碼）", env={"SW_P": "envpw"},
           reactions=[on_empty("\r\nlogin: "), {"on": "eq", "match": "admin", "rx": "\r\nPassword: "}, {"on": "secret", "rx": PRPL}, NONCE_OK_PRPL],
           profile_over={"username": "admin", "pass_env": "SW_P"}),
        _c("ensure_ready", "prpl-template", "secret 後無 prompt → PRPL_PROMPT_TIMEOUT", auth=AUTH,
           reactions=[on_empty("\r\nlogin: "), {"on": "eq", "match": "root", "rx": "\r\nPassword: "}]),
        _c("ensure_ready", "prpl-template", "無 password prompt、直接出 prompt → 成功", auth=AUTH,
           reactions=[on_empty("\r\nlogin: "), {"on": "eq", "match": "root", "rx": PRPL}, NONCE_OK_PRPL]),
        _c("ensure_ready", "prpl-template", "無 password prompt、也無 prompt → PRPL_PROMPT_TIMEOUT", auth=AUTH,
           reactions=[on_empty("\r\nlogin: ")]),
        _c("ensure_ready", "prpl-template", "user_env 缺＋pass_env 宣告、有 login prompt → LOGIN_USER_REQUIRED（X3）",
           reactions=[on_empty("\r\nlogin: ")], profile_over={"user_env": "SW_U", "pass_env": "SW_P", "username": "root"}),
        _c("ensure_ready", "prpl-template", "user_env 缺＋pass_env 宣告、無 login prompt → LOGIN_PROMPT_TIMEOUT（X3）",
           profile_over={"user_env": "SW_U", "pass_env": "SW_P"}),
        _c("ensure_ready", "prpl-template", "user_env 由 env 補（auth 無）", env={"SW_U": "envuser", "SW_P": "envpw"},
           reactions=[on_empty("\r\nlogin: "), {"on": "eq", "match": "envuser", "rx": "\r\nPassword: "}, {"on": "secret", "rx": PRPL}, NONCE_OK_PRPL],
           profile_over={"user_env": "SW_U", "pass_env": "SW_P"}),
        _c("ensure_ready", "prpl-template", "auth.username 優先於 env", env={"SW_U": "envuser", "SW_P": "envpw"}, auth={"username": "authuser"},
           reactions=[on_empty("\r\nlogin: "), {"on": "eq", "match": "authuser", "rx": "\r\nPassword: "}, {"on": "secret", "rx": PRPL}, NONCE_OK_PRPL],
           profile_over={"user_env": "SW_U", "pass_env": "SW_P"}),
        _c("ensure_ready", "prpl-template", "不需登入且無 prompt → PRPL_PROMPT_TIMEOUT"),
        _c("ensure_ready", None, "SessionProfile 預設 login_regex 雙反斜線永不匹配（EVT-D-C1）→ LOGIN_PROMPT_TIMEOUT", auth=AUTH,
           reactions=[on_empty("\r\nlogin: ")]),
        _c("ensure_ready", "prpl-template", "無效 login_regex → raised error", auth=AUTH, profile_over={"login_regex": "("}),
        _c("ensure_ready", "prpl-template", "unicode 密碼", auth={"username": "root", "password": "密碼"},
           reactions=[on_empty("\r\nlogin: "), {"on": "eq", "match": "root", "rx": "\r\nPassword: "}, {"on": "secret", "rx": PRPL}, NONCE_OK_PRPL]),
        # ── shell（op3-template）──
        _c("ensure_ready", "op3-template", "shell 完整登入", auth=AUTH, reactions=[
            on_empty("\r\nop3 login: "), {"on": "eq", "match": "root", "rx": "\r\nPassword: "}, {"on": "secret", "rx": SHELL},
            {"on": "prefix", "match": "echo __READY__", "rx": "\r\n__READY__{nonce}" + SHELL}]),
        _c("ensure_ready", "op3-template", "shell secret 後無 prompt → SHELL_PROMPT_TIMEOUT", auth=AUTH,
           reactions=[on_empty("\r\nop3 login: "), {"on": "eq", "match": "root", "rx": "\r\nPassword: "}]),
        _c("probe_ready", "op3-template", "shell probe 成功", reactions=[on_empty(SHELL),
           {"on": "prefix", "match": "echo __READY__", "rx": "\r\n__READY__{nonce}" + SHELL}]),
        _c("probe_ready", "op3-template", "shell 無 prompt → PROMPT_UNAVAILABLE"),
        # ── bcm（brcm-template）──
        _c("ensure_ready", "brcm-template", "bcm 兩段登入＋post_login sh＋nonce", auth={"username": "admin", "password": "pw"}, reactions=[
            on_empty("\r\nLogin: "), {"on": "eq", "match": "admin", "rx": "\r\nPassword: "}, {"on": "secret", "rx": "\r\nCM> "},
            {"on": "eq", "match": "sh", "rx": "\r\n# "}, {"on": "prefix", "match": "echo __READY__", "rx": "\r\n__READY__{nonce}\r\n# "}]),
        _c("ensure_ready", "brcm-template", "bcm secret 後無 prompt → BCM_PROMPT_TIMEOUT", auth={"username": "admin", "password": "pw"},
           reactions=[on_empty("\r\nLogin: "), {"on": "eq", "match": "admin", "rx": "\r\nPassword: "}]),
        _c("ensure_ready", "brcm-template", "bcm post_login sh 無回應 → POST_LOGIN_CMD_TIMEOUT", auth={"username": "admin", "password": "pw"},
           reactions=[on_empty("\r\nLogin: "), {"on": "eq", "match": "admin", "rx": "\r\nPassword: "}, {"on": "secret", "rx": "\r\nCM> "}]),
        _c("ensure_ready", "brcm-template", "POST_LOGIN_CMD_TIMEOUT＋flood → RX_FLOOD", auth={"username": "admin", "password": "pw"},
           reactions=[on_empty("\r\nLogin: "), {"on": "eq", "match": "admin", "rx": "\r\nPassword: "}, {"on": "secret", "rx": "\r\nCM> "}],
           rx_bytes_10s=30000),
        _c("ensure_ready", "brcm-template", "post_login 逾時後 tail 回到 Login → LOGIN_REQUIRED", auth={"username": "admin", "password": "pw"},
           reactions=[on_empty("\r\nLogin: "), {"on": "eq", "match": "admin", "rx": "\r\nPassword: "}, {"on": "secret", "rx": "\r\nCM> "},
                      {"on": "eq", "match": "sh", "rx": "\r\nLogin: "}]),
        _c("probe_ready", "brcm-template", "prompt 誤配但 tail 有 Login → post_login guard LOGIN_REQUIRED（不送 sh）",
           reactions=[on_empty("\r\nBDK>\r\nLogin: ")]),
        _c("ensure_ready", "brcm-template", "bcm nonce 逾時 → READY_NONCE_TIMEOUT", auth={"username": "admin", "password": "pw"},
           reactions=[on_empty("\r\nLogin: "), {"on": "eq", "match": "admin", "rx": "\r\nPassword: "}, {"on": "secret", "rx": "\r\nCM> "},
                      {"on": "eq", "match": "sh", "rx": "\r\n# "}]),
        _c("probe_ready", "brcm-template", "bcm 前導空白 prompt（#224）成功", reactions=[on_empty("\r\n  # "), {"on": "eq", "match": "sh", "rx": "\r\n # "},
           {"on": "prefix", "match": "echo __READY__", "rx": "\r\n__READY__{nonce}\r\n\t# "}]),
        _c("ensure_ready", "brcm-template", "bcm 無 Login prompt → LOGIN_PROMPT_TIMEOUT", auth={"username": "admin", "password": "pw"}),
        # ── passthrough ──
        _c("probe_ready", "uboot-template", "passthrough uboot：prompt 命中後 nonce 無回應 → READY_NONCE_TIMEOUT", reactions=[on_empty("\r\n=> ")]),
        _c("ensure_ready", "uboot-template", "passthrough uboot：無 prompt、不需登入 → PRPL_PROMPT_TIMEOUT"),
        _c("ensure_ready", "others-template", "passthrough others：prompt '.*' 恆中、ready_probe 空 → READY_NONCE_TIMEOUT"),
        _c("probe_ready", "others-template", "passthrough others：probe_ready 同上"),
        # ── console unlock（smart4R-profile）──
        _c("ensure_ready", "smart4R-profile", "未解析 unlock → CREDENTIALS_UNRESOLVED"),
        _c("ensure_ready", "smart4R-profile", "已在 prompt：靜默後直接 nonce", auth={"console_unlock": "k"}, initial_rx=S4R.lstrip("\r\n"),
           idle_s=10.0, reactions=[{"on": "prefix", "match": "echo __READY__", "rx": "__READY__{nonce}" + S4R}]),
        _c("ensure_ready", "smart4R-profile", "lock：空行無回應後送 secret", auth={"console_unlock": "k"}, idle_s=10.0,
           reactions=[{"on": "secret", "rx": S4R}, {"on": "prefix", "match": "echo __READY__", "rx": "__READY__{nonce}" + S4R}]),
        _c("ensure_ready", "smart4R-profile", "timeout < idle → CONSOLE_UNLOCK_IDLE_TIMEOUT", auth={"console_unlock": "k"},
           profile_over={"timeout_s": 2.0}),
        _c("ensure_ready", "smart4R-profile", "尾行 CFE> → CONSOLE_UNLOCK_BOOTLOADER", auth={"console_unlock": "k"}, initial_rx="CFE> ", idle_s=10.0),
        _c("ensure_ready", "smart4R-profile", "CFE banner 歷史不阻擋（尾行已是 Linux）", auth={"console_unlock": "k"},
           initial_rx="CFE version 1.0\r\n...\r\nLinux booting\r\n", reactions=[{"on": "secret", "rx": S4R},
           {"on": "prefix", "match": "echo __READY__", "rx": "__READY__{nonce}" + S4R}], nonces=["0a1b2c3d"]),
        _c("ensure_ready", "smart4R-profile", "secret 後無 prompt → CONSOLE_UNLOCK_PROMPT_TIMEOUT", auth={"console_unlock": "k"}, idle_s=10.0),
        _c("ensure_ready", "smart4R-profile", "只有 echo、無 __READY__ 行 → READY_NONCE_TIMEOUT", auth={"console_unlock": "k"}, idle_s=10.0,
           reactions=[{"on": "secret", "rx": S4R}, {"on": "prefix", "match": "echo __READY__", "rx": "echo __READY__{nonce}\r\n"}]),
        _c("ensure_ready", "smart4R-profile", "無效 bootloader regex → raised error", auth={"console_unlock": "k"}, idle_s=10.0,
           profile_over={"bootloader_prompts": ["("]}),
        _c("ensure_ready", "smart4R-profile", "空行後 prompt 晚到（等待期間）→ 不送 secret", auth={"console_unlock": "k"}, idle_s=10.0,
           reactions=[{"on": "eq", "match": "", "rx": S4R, "after_s": 3.0}, {"on": "prefix", "match": "echo __READY__", "rx": "__READY__{nonce}" + S4R}]),
        _c("ensure_ready", "smart4R-profile", "secret 後 prompt 延遲 3 s", auth={"console_unlock": "k"}, idle_s=10.0,
           reactions=[{"on": "secret", "rx": S4R, "after_s": 3.0}, {"on": "prefix", "match": "echo __READY__", "rx": "__READY__{nonce}" + S4R}]),
        _c("ensure_ready", "smart4R-profile", "初始 RX 剛到（idle 0）需等滿 5 s", auth={"console_unlock": "k"}, initial_rx="boot log\r\n",
           reactions=[{"on": "secret", "rx": S4R}, {"on": "prefix", "match": "echo __READY__", "rx": "__READY__{nonce}" + S4R}]),
        # ── detect_template ──
        _c("detect_template", "prpl-template", "prompt 命中 prpl", templates=["uboot-template", "smart4R-profile", "prpl-template"],
           reactions=[on_empty(PRPL)]),
        _c("detect_template", "op3-template", "只命中 login → 第一個 login 候選", templates=["uboot-template", "smart4R-profile", "op3-template", "prpl-template"],
           reactions=[on_empty("\r\nlogin: ")]),
        _c("detect_template", "prpl-template", "皆不命中 → None", templates=["uboot-template", "prpl-template"]),
        _c("detect_template", "prpl-template", "無效 regex 略過", templates=["prpl-template"], reactions=[on_empty(PRPL)],
           probe_timeout_s=1.0),
    ]
    return cases
