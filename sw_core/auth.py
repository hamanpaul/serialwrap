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


def resolve_session_auth(sp: SessionProfile) -> SessionAuth:
    """從 env_file 與 os.environ 解析 session 帳密。

    優先序：
    1. ``env_file`` 內的 key（若檔案存在）
    2. ``os.environ`` fallback（向後相容）
    3. ``sp.username`` 欄位（最低優先）
    """
    local_env: dict[str, str] = {}
    if sp.env_file:
        expanded = os.path.expanduser(sp.env_file)
        if os.path.isfile(expanded):
            try:
                local_env = parse_env_file(expanded)
            except Exception:
                log.warning("無法解析 env_file: %s（session %s）", expanded, sp.com)
        else:
            log.warning("env_file 不存在: %s（session %s）", expanded, sp.com)

    username: str | None = None
    if sp.user_env:
        username = local_env.get(sp.user_env) or os.environ.get(sp.user_env)
    if not username and sp.username:
        username = sp.username

    password: str | None = None
    if sp.pass_env:
        password = local_env.get(sp.pass_env) or os.environ.get(sp.pass_env)

    return SessionAuth(username=username, password=password)
