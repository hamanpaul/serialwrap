"""per-session 帳密解析。

每個 session 可以透過 ``env_file`` 指定自己的帳密來源，
不再依賴 daemon 全域 ``os.environ``。
"""

from __future__ import annotations

import dataclasses
import logging
import os

from .config import SessionProfile

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class SessionAuth:
    """attach / login 時使用的帳密，已解析為明文值。"""

    username: str | None = None
    password: str | None = None


@dataclasses.dataclass(frozen=True)
class AuthResolution:
    """帳密解析的觀測性結果（#140）。

    僅描述「為什麼」帳密解析成 目前的 ``SessionAuth``，供上層決定是否
    示警／中止登入。**不含帳密值本身**（示警訊息絕不印出帳密）。

    ``reason`` 取值：

    - ``ok``：帳密成功解析（``username`` 與 ``password`` 皆非空）。
    - ``env_file_missing``：profile 宣告 ``env_file``、但解析絕對路徑不存在，
      且帳密無法由其他來源補齊。
    - ``env_file_unreadable``：``env_file`` 存在但讀取／解析失敗，且帳密無法補齊。
    - ``key_absent``：宣告了帳密來源、但缺 ``user_env``/``pass_env`` 指定的 key
      或其值為空（含 env_file 可讀但缺 key、或無 env_file 而 os.environ 缺 key）。
    - ``not_configured``：profile 未宣告任何帳密來源（三者皆無）。

    ``env_file_path``：profile 宣告 ``env_file`` 時帶 ``os.path.expanduser`` 後的
    絕對路徑（供示警訊息指出實際解析位置）；未宣告時為 ``None``。
    """

    reason: str = "ok"
    env_file_path: str | None = None


def parse_env_file(path: str) -> dict[str, str]:
    """純 Python 解析 KEY=VALUE 格式的 env 檔。

    支援：
    - ``export`` 前綴（可有可無）
    - 單引號 / 雙引號包圍的值
    - ``#`` 開頭的註解行
    - 空行
    """
    env: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fp:
        for raw_line in fp:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            env[key] = value
    return env


def resolve_session_auth(sp: SessionProfile) -> tuple[SessionAuth, AuthResolution]:
    """從 env_file 與 os.environ 解析 session 帳密，並回報解析狀態（#140）。

    優先序：
    1. ``env_file`` 內的 key（若檔案存在）
    2. ``os.environ`` fallback（向後相容）
    3. ``sp.username`` 欄位（最低優先）

    回傳 ``(SessionAuth, AuthResolution)``。``AuthResolution.reason`` 反映
    「帳密是否真的解析成功」：只要 ``username``/``password`` 皆非空即為 ``ok``
    （即使 env_file 缺失、只要 os.environ 補齊亦然，避免誤擋可用帳密）；解析為空
    才依來源狀況細分成 ``env_file_missing`` / ``env_file_unreadable`` /
    ``key_absent`` / ``not_configured``，供上層 gate 與示警使用。
    """
    expanded: str | None = None
    env_file_state: str | None = None  # None=無 env_file / "missing" / "unreadable" / "read"
    local_env: dict[str, str] = {}
    if sp.env_file:
        expanded = os.path.expanduser(sp.env_file)
        if os.path.isfile(expanded):
            try:
                local_env = parse_env_file(expanded)
                env_file_state = "read"
            except Exception:
                env_file_state = "unreadable"
                log.warning("無法解析 env_file: %s（session %s）", expanded, sp.com)
        else:
            env_file_state = "missing"
            log.warning("env_file 不存在: %s（session %s）", expanded, sp.com)

    username: str | None = None
    if sp.user_env:
        username = local_env.get(sp.user_env) or os.environ.get(sp.user_env)
    if not username and sp.username:
        username = sp.username

    password: str | None = None
    if sp.pass_env:
        password = local_env.get(sp.pass_env) or os.environ.get(sp.pass_env)

    auth = SessionAuth(username=username, password=password)

    # --- reason 判定（#140）---
    declared = bool(sp.user_env or sp.pass_env or sp.env_file)
    if not declared:
        reason = "not_configured"
    elif username and password:
        # 帳密確實解析成功（不論來自 env_file 或 os.environ fallback）→ ok，不阻擋。
        reason = "ok"
    elif env_file_state == "missing":
        reason = "env_file_missing"
    elif env_file_state == "unreadable":
        reason = "env_file_unreadable"
    else:
        # env_file 可讀但缺 key，或無 env_file 而 os.environ 亦缺 → key 缺失。
        reason = "key_absent"

    return auth, AuthResolution(reason=reason, env_file_path=expanded)
