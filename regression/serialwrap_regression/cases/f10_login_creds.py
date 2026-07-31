"""F10 登入與帳密解析（#140）——throwaway daemon 隔離，prod 組態零接觸。

兩個 case 共同重現 #140 情境（COM1／bcm）：

- ``f10-unresolved-creds-terminal``：profile 宣告帳密來源但解析為空 → 必須落終態
  ``CREDENTIALS_UNRESOLVED``，不得靜默對 ``Login:``/``Password:`` 送空字串迴圈。
- ``f10-creds-fixed-then-ready``：補上有效帳密後，bcm 登入路徑須恢復、session 能到 ``READY``
  （證明終態並非不可逆，補帳密＋re-attach 即可復原）。

全程在 ``guards.ThrowawayDaemon`` sandbox 內操作（獨立 XDG/socket/by-id），對 prod 組態／state
零接觸；prod 側只做 ``device release``（交出裝置）／``device attach``（收回）兩個交接動作。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import yaml
from realhw.harness import CaseResult

from ..harness import Case, register
from .. import guards


def _case(id, title, issues, hints=(), requires=(), destructive=True):
    def deco(fn):
        register(Case(id=id, family="F10", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints)))
        return fn
    return deco


# 已知 profile-dir 候選（README「關鍵排查點」）：systemd-system 安裝在 /etc/serialwrap/profiles/，
# pipx/XDG 安裝在 ~/.config/serialwrap/profiles/。唯讀掃描，找 platform=bcm 的 template。
_PROD_PROFILE_CANDIDATES: tuple[str, ...] = (
    "/etc/serialwrap/profiles/default.yaml",
    "~/.config/serialwrap/profiles/default.yaml",
)
# 20s 觀察窗內若累計 >= 此門檻的空 TX payload，視為疑似連續空帳密敲擊（寬鬆判定，防單筆誤判）。
_EMPTY_LOGIN_SPAM_THRESHOLD = 3


def _com1_board(ctx: Any) -> dict[str, Any] | None:
    """testbed 第二板（COM1，#140 原始情境是 bcm）；未設定則回 None（case 轉 SKIP）。"""
    boards = ctx.cfg.get("boards") or []
    if len(boards) < 2:
        return None
    return boards[1]


def _find_prod_bcm_template() -> tuple[Path, dict[str, Any]] | None:
    """唯讀掃描 prod profile-dir 候選，取 ``platform: bcm`` 的 template dict。

    只讀「組態結構」（platform/prompt_regex/user_env/pass_env 等鍵名），不讀帳密值本身
    ——env_file 內容留給 :func:`_resolve_prod_bcm_credentials` 在需要時才讀取，且絕不落 evidence。
    找不到符合條件的 profile 檔或 bcm template → None（case 轉 SKIP）。
    """
    for cand in _PROD_PROFILE_CANDIDATES:
        path = Path(cand).expanduser()
        if not path.is_file():
            continue
        try:
            obj = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        profiles = obj.get("profiles") if isinstance(obj, dict) else None
        if not isinstance(profiles, dict):
            continue
        for tpl in profiles.values():
            if isinstance(tpl, dict) and str(tpl.get("platform", "")).strip().lower() == "bcm":
                return path, tpl
    return None


def _sandbox_symlink_for_board(board: dict[str, Any], by_id_dir: Path) -> str:
    """在 sandbox ``by_id_dir`` 內只放這一塊板的 by-id symlink（其餘裝置對 throwaway 不可見）。

    未採用「原樣複製 ``os.readlink()`` 相對 target」的寫法：``sw_core/device_source.py`` 的
    ``PosixDeviceSource`` 用 ``os.path.realpath(entry)`` 解析 by-id symlink——relative target是
    相對「symlink 自身所在目錄」解析。真實 ``/dev/serial/by-id/<link>`` 的 target（如
    ``../../ttyUSB1``）是相對 ``/dev/serial/by-id`` 這個固定深度算的；sandbox 目錄
    （``ctx.case_dir/byid``）深度不同，原樣複製同一段相對字串會解析到錯誤位置、device_source
    掃不到裝置。改為直接對 ``os.path.realpath()`` 解出的絕對路徑建 symlink——語意等價（同一個
    real device），且不受 sandbox 目錄深度影響。

    回傳 symlink 檔名；找不到對應 serial 的裝置 → ``LookupError``（呼叫端轉 SKIP）。
    """
    serial = str(board.get("serial") or "").strip()
    if not serial:
        raise LookupError(f"testbed board 缺 serial 欄位：{board!r}")
    src_dir = Path("/dev/serial/by-id")
    match = next((p for p in sorted(src_dir.glob("*")) if serial in p.name), None)
    if match is None:
        raise LookupError(f"/dev/serial/by-id 找不到含 serial={serial} 的裝置（board={board!r}）")
    by_id_dir.mkdir(parents=True, exist_ok=True)
    dest = by_id_dir / match.name
    if not dest.exists():
        os.symlink(os.path.realpath(match), dest)
    return match.name


def _render_profile_yaml(bcm_template: dict[str, Any], *, profile_name: str, env_file: str) -> str:
    """複製 prod bcm template 全欄位、只覆寫 ``env_file``（結構對齊 ``sw_core/config.py`` schema：
    platform/prompt_regex/login_regex/password_regex/post_login_cmd/ready_probe/user_env/
    pass_env/env_file/timeout_s/bootloader_prompts/uart）。``targets: []`` 交給動態偵測配 COM。
    """
    tpl = dict(bcm_template)
    tpl["env_file"] = env_file
    doc: dict[str, Any] = {"defaults": {}, "profiles": {profile_name: tpl}, "targets": []}
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def _parse_env_file(path: Path) -> dict[str, str]:
    """獨立實作的極簡 KEY=VALUE 解析（禁 import ``sw_core.auth``；語意對齊其 ``parse_env_file``：
    支援 ``export`` 前綴、``#`` 註解、空行、單/雙引號值）。回傳值只供內部取值，不得落 evidence。
    """
    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
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


def _resolve_prod_bcm_credentials(template: dict[str, Any], base_dir: Path) -> tuple[str, str] | None:
    """依 prod bcm template 宣告的 ``env_file``／``user_env``／``pass_env`` 取得目前有效帳密
    （優先序同 ``sw_core/auth.py``：env_file 內的 key → ``os.environ`` fallback）。

    回傳值僅供呼叫端寫入 sandbox env_file；呼叫端**絕不得**把回傳值本身落 evidence／note
    （只能記「已取得，長度 N」）。取不到（env_file 缺失/不可讀/缺 key 且 os.environ 亦缺）
    → None（case 轉 SKIP，``creds_source_unavailable``）。
    """
    user_env = str(template.get("user_env") or "").strip()
    pass_env = str(template.get("pass_env") or "").strip()
    if not user_env or not pass_env:
        return None
    local_env: dict[str, str] = {}
    env_file = template.get("env_file")
    if env_file:
        env_path = Path(str(env_file)).expanduser()
        if not env_path.is_absolute():
            env_path = (base_dir / env_path).resolve()
        if env_path.is_file():
            try:
                local_env = _parse_env_file(env_path)
            except OSError:
                local_env = {}
    username = local_env.get(user_env) or os.environ.get(user_env)
    password = local_env.get(pass_env) or os.environ.get(pass_env)
    if not username or not password:
        return None
    return username, password


def _session_by_com(listing: dict[str, Any], com: str) -> dict[str, Any]:
    for s in listing.get("sessions") or []:
        if s.get("com") == com:
            return s
    return {}


def _wait_dynamic_sessions(ta: guards.ThrowawayDaemon, *, timeout_s: float = 20.0,
                           poll_s: float = 2.0) -> dict[str, Any]:
    """等 throwaway 的 DeviceWatcher 完成首掃、動態 session 出現在 ``session list``。

    round 2 實測教訓：``daemon status`` ok 只保證 RPC server 起來；session 註冊晚於
    PROBE 開始（WAL 已見探測 bytes、list 仍空）——單次查詢必撞時序競態（3/3 重現）。
    回傳最後一次 listing（逾時仍回，呼叫端自行判空）。
    """
    deadline = time.monotonic() + timeout_s
    listing: dict[str, Any] = {}
    while time.monotonic() < deadline:
        listing = ta.run("session", "list")
        if listing.get("sessions"):
            return listing
        time.sleep(poll_s)
    return listing


def _wait_state(ta: guards.ThrowawayDaemon, com: str, want: str, *,
                timeout_s: float, poll_s: float = 2.0) -> dict[str, Any]:
    """輪詢 throwaway ``session list`` 到指定 state（``ThrowawayDaemon`` 無 ``SwCli.wait_state``
    可用，本地補一份）。回傳最後一次該 com 的 session dict（逾時仍回最後一次輪詢結果）。
    """
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _session_by_com(ta.run("session", "list"), com)
        if last.get("state") == want:
            return last
        time.sleep(poll_s)
    return last


def _ta_com(listing: dict[str, Any]) -> str:
    """取 throwaway 唯一動態 session 的實際 COM 編號。

    round 3 實測教訓：sandbox 只有一條線，但動態編號**不保證是 COM0**（該輪拿到 COM1，
    硬編碼 COM0 → SESSION_NOT_FOUND 假陽性）——一律從 listing 動態解析。
    """
    sessions = listing.get("sessions") or [{}]
    return str(sessions[0].get("com") or "COM0")


def _wait_credentials_unresolved(ta: guards.ThrowawayDaemon, com: str, *,
                                 timeout_s: float) -> dict[str, Any]:
    """輪詢 sandbox ``session list``，等 ``last_error`` 落地 ``CREDENTIALS_UNRESOLVED``。

    回傳 ``{"found": bool, "listing": dict}``（``listing`` 為最後一次 ``session list`` 回應，
    供呼叫端落 evidence）。
    """
    deadline = time.monotonic() + timeout_s
    listing: dict[str, Any] = {}
    while time.monotonic() < deadline:
        listing = ta.run("session", "list")
        if _session_by_com(listing, com).get("last_error") == "CREDENTIALS_UNRESOLVED":
            return {"found": True, "listing": listing}
        time.sleep(2.0)
    return {"found": False, "listing": listing}


def _count_empty_login_tx(wal_text: str) -> int:
    """粗略掃描 sandbox WAL ndjson：計算 TX 方向（``dir": "TX"``）且 payload 解碼後為空/僅
    CR/LF 的筆數（寬鬆偵測「連續送空帳密」徵兆；#140 修復後應為 0——daemon 判定帳密解析失敗
    後直接落終態、不再對 UART 送任何 login 敲擊）。非 JSON 行（如 mirror.log 純文字）略過。
    """
    import base64
    import json as _json

    count = 0
    for line in wal_text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            rec = _json.loads(line)
        except ValueError:
            continue
        if rec.get("dir") != "TX":
            continue
        payload_b64 = rec.get("payload_b64")
        if not payload_b64:
            continue
        try:
            payload = base64.b64decode(payload_b64)
        except (ValueError, TypeError):
            continue
        if payload.strip(b"\r\n") == b"":
            count += 1
    return count


def _reclaim_and_ensure_ready(ctx: Any, com: str) -> CaseResult | None:
    """收尾共用：prod ``device attach`` 收回裝置＋等 ``READY``。成功回 None，失敗回 FAIL。"""
    reclaim = ctx.sw.run("device", "attach", "--selector", com)
    reclaim_note = ctx.note("device-reclaim.json", str(reclaim))
    ready = guards.ensure_ready(ctx, com, timeout_s=float(ctx.cfg["timeouts"]["ready_wait_s"]))
    if not ready:
        return CaseResult(
            "FAIL",
            reason=f"收尾 device attach 收回 {com} 後未回 READY（reclaim 失敗，prod 裝置懸空，需人工介入）",
            category="environment", reason_code="reclaim_failed",
            evidence={"reclaim": reclaim_note},
        )
    return None


@_case(
    "f10-unresolved-creds-terminal",
    "帳密解析失敗須落終態 CREDENTIALS_UNRESOLVED、不得靜默送空帳密迴圈",
    issues=("#140",),
    hints=(
        "prod device release 交出 COM1（bcm）後，sandbox by-id 只放這一條線；profile 照 prod bcm "
        "template 結構複製、只覆寫 env_file 指向不存在路徑，逼出 #140 情境。",
        "終態斷言涵蓋 attach 回應的頂層 error_code 與其後 session list 的 last_error 兩處（README "
        "#94：command-capable session 未達 READY 時 attach 會回非零 exit + 頂層 error_code）。",
        "不送空帳密驗證為寬鬆判定：等 20s 後狀態需維持穩定終態、且 WAL TX 方向空 payload 計數低於門檻。",
    ),
)
def f10_unresolved_creds_terminal(ctx: Any) -> CaseResult:
    """#140 oracle：帳密來源宣告但解析為空時，daemon 不得送空帳密迴圈，須落終態
    ``CREDENTIALS_UNRESOLVED`` 且不自動重探。全程在 throwaway daemon sandbox 內重現，
    prod 端只做 device release／attach 交接，組態零接觸。
    """
    board = _com1_board(ctx)
    if board is None:
        return CaseResult("SKIP", reason="testbed 未設定第二板（COM1/bcm）",
                          category="configuration", reason_code="board_com1_not_configured")
    com = str(board["com"])

    found = _find_prod_bcm_template()
    if found is None:
        return CaseResult(
            "SKIP", reason="prod profile 目錄找不到 platform=bcm 的 template（無法照樣板構造 sandbox profile）",
            category="environment", reason_code="bcm_template_not_found")
    _prod_profile_path, bcm_template = found

    by_id_dir = ctx.case_dir / "byid"
    try:
        by_id_name = _sandbox_symlink_for_board(board, by_id_dir)
    except LookupError as exc:
        return CaseResult("SKIP", reason=str(exc), category="environment", reason_code="board_by_id_not_found")

    release = ctx.sw.run("device", "release", "--selector", com,
                         "--source", "agent:swreg", "--reason", "f10 creds regression")
    ctx.note("device-release.json", str(release))
    if not release.get("ok"):
        return CaseResult("FAIL", reason=f"prod device release 失敗（selector={com}）：{release!r}",
                          category="environment", reason_code="release_failed")

    profile_yaml = _render_profile_yaml(bcm_template, profile_name="bcm-badcreds",
                                        env_file="/nonexistent/creds.env")
    workdir = ctx.case_dir / "ta"

    result: CaseResult | None = None
    try:
        try:
            with guards.ThrowawayDaemon(
                exe=str(ctx.cfg["serialwrap_exe"]), workdir=workdir,
                by_id_dir=by_id_dir, profile_yaml=profile_yaml,
            ) as ta:
                listing0 = _wait_dynamic_sessions(ta, timeout_s=20.0)
                ctx.note("ta-session-list-initial.json", str(listing0))
                if not (listing0.get("sessions") or []):
                    result = CaseResult(
                        "FAIL",
                        reason=f"throwaway 動態偵測未見任何 session（sandbox by-id 僅放 {by_id_name}，"
                               "疑似 sandbox 裝置未被偵測到，非 #140 本身的斷言範圍）",
                        category="environment", reason_code="no_dynamic_session",
                        evidence={"listing_initial": "ta-session-list-initial.json"},
                    )
                else:
                    ta_com = _ta_com(listing0)  # 動態解析，勿硬編碼 COM0（round 3 教訓）
                    attach = ta.run("session", "attach", "--selector", ta_com, timeout=60)
                    attach_note = ctx.note("ta-attach.json", str(attach))

                    poll = _wait_credentials_unresolved(ta, ta_com, timeout_s=60.0)
                    listing_note = ctx.note("ta-session-list-after-attach.json", str(poll["listing"]))
                    terminal = (attach.get("error_code") == "CREDENTIALS_UNRESOLVED") or poll["found"]

                    if not terminal:
                        result = CaseResult(
                            "FAIL",
                            reason="attach 回應與後續 session list 均未見 CREDENTIALS_UNRESOLVED"
                                   "（#140 回歸：終態未落地）",
                            category="test", reason_code="unresolved_creds_not_terminal",
                            evidence={"attach": attach_note, "listing": listing_note},
                        )
                    else:
                        # 不送空帳密驗證：終態落地後再等 20s，狀態須維持穩定（非持續重試迴圈）。
                        time.sleep(20)
                        listing2 = ta.run("session", "list")
                        listing2_note = ctx.note("ta-session-list-plus20s.json", str(listing2))
                        sess2 = _session_by_com(listing2, ta_com)
                        if sess2.get("last_error") != "CREDENTIALS_UNRESOLVED" or sess2.get("state") == "ATTACHING":
                            result = CaseResult(
                                "FAIL",
                                reason=f"等待 20s 後狀態非穩定終態（state={sess2.get('state')!r}, "
                                       f"last_error={sess2.get('last_error')!r}），疑似持續重試（#140 回歸）",
                                category="test", reason_code="not_stable_terminal",
                                evidence={"listing_plus20s": listing2_note},
                            )
                        else:
                            wal_text = ta.wal_text()
                            wal_note = ctx.note("ta-wal-tail.txt", wal_text[-4000:] if wal_text else "(空)")
                            spam = _count_empty_login_tx(wal_text)
                            if spam >= _EMPTY_LOGIN_SPAM_THRESHOLD:
                                result = CaseResult(
                                    "FAIL",
                                    reason=f"WAL TX 方向偵測到 {spam} 次空 payload 敲擊"
                                           "（#140 回歸：疑似靜默送空帳密迴圈）",
                                    category="test", reason_code="empty_creds_spam",
                                    evidence={"wal_tail": wal_note},
                                )
                            else:
                                result = CaseResult(
                                    "PASS",
                                    reason=f"CREDENTIALS_UNRESOLVED 終態落地、20s 內狀態穩定、"
                                           f"WAL 空 TX 計數={spam}（人工可覆核 wal_tail）",
                                    evidence={"attach": attach_note, "listing": listing_note, "wal_tail": wal_note},
                                )
        except RuntimeError as exc:
            log_path = workdir / "daemon.log"
            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else "(無 daemon.log)"
            ctx.note("throwaway-start-failed.txt", f"{exc}\n\n--- daemon.log ---\n{log_text}")
            result = CaseResult("SKIP", reason=f"throwaway daemon 未在時限內就緒：{exc}",
                                category="environment", reason_code="throwaway_start_failed")
    finally:
        # with 區塊結束會自動 kill throwaway daemon；此處只需 prod 側收回裝置。
        reclaim_fail = _reclaim_and_ensure_ready(ctx, com)
        if reclaim_fail is not None:
            result = reclaim_fail

    return result if result is not None else CaseResult(
        "FAIL", reason="case 邏輯未產生結果（不應發生）", category="test", reason_code="case_logic_error")


@_case(
    "f10-creds-fixed-then-ready",
    "補上有效帳密後 bcm 登入路徑恢復、session 回 READY",
    issues=("#140",),
    hints=(
        "帳密自 prod bcm template 宣告的來源（env_file 或 os.environ fallback）唯讀取得後寫入 "
        "sandbox creds.env；帳密值只落地檔案，evidence／note 只記長度，絕不記實際值。",
        "取不到有效帳密（env_file 缺失/缺 key 且 os.environ 亦缺）→ SKIP，不視為 FAIL（環境缺項）。",
    ),
)
def f10_creds_fixed_then_ready(ctx: Any) -> CaseResult:
    """#140 oracle 的另一半：補上有效帳密後，bcm 登入路徑須恢復、session 能到 READY——證明
    ``CREDENTIALS_UNRESOLVED`` 並非不可逆終態，補帳密＋re-attach 即可正常復原。
    """
    board = _com1_board(ctx)
    if board is None:
        return CaseResult("SKIP", reason="testbed 未設定第二板（COM1/bcm）",
                          category="configuration", reason_code="board_com1_not_configured")
    com = str(board["com"])

    found = _find_prod_bcm_template()
    if found is None:
        return CaseResult(
            "SKIP", reason="prod profile 目錄找不到 platform=bcm 的 template（無法照樣板構造 sandbox profile）",
            category="environment", reason_code="bcm_template_not_found")
    prod_profile_path, bcm_template = found

    creds = _resolve_prod_bcm_credentials(bcm_template, prod_profile_path.parent)
    if creds is None:
        return CaseResult(
            "SKIP", reason="prod bcm template 宣告的帳密來源（env_file／os.environ）目前取不到有效帳密",
            category="environment", reason_code="creds_source_unavailable")
    username, password = creds
    user_env = str(bcm_template.get("user_env") or "").strip()
    pass_env = str(bcm_template.get("pass_env") or "").strip()

    by_id_dir = ctx.case_dir / "byid"
    try:
        by_id_name = _sandbox_symlink_for_board(board, by_id_dir)
    except LookupError as exc:
        return CaseResult("SKIP", reason=str(exc), category="environment", reason_code="board_by_id_not_found")

    release = ctx.sw.run("device", "release", "--selector", com,
                         "--source", "agent:swreg", "--reason", "f10 creds regression")
    ctx.note("device-release.json", str(release))
    if not release.get("ok"):
        return CaseResult("FAIL", reason=f"prod device release 失敗（selector={com}）：{release!r}",
                          category="environment", reason_code="release_failed")

    # 真帳密檔的寫入必須落在 try/finally 保護內（finally 必刪）——任何早退路徑都不得殘留。
    creds_path = ctx.case_dir / "creds.env"
    workdir = ctx.case_dir / "ta"

    result: CaseResult | None = None
    try:
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        creds_path.write_text(f"{user_env}={username}\n{pass_env}={password}\n", encoding="utf-8")
        # 脫敏：evidence／note 只記已取得＋長度，絕不記實際帳密值。
        ctx.note(
            "creds-source.txt",
            f"已取得帳密（脫敏，僅記長度）：{user_env} 長度={len(username)}；{pass_env} 長度={len(password)}；"
            "來源＝prod bcm template 宣告的 env_file 或 os.environ fallback。",
        )
        profile_yaml = _render_profile_yaml(bcm_template, profile_name="bcm-fixedcreds", env_file=str(creds_path))
        try:
            with guards.ThrowawayDaemon(
                exe=str(ctx.cfg["serialwrap_exe"]), workdir=workdir,
                by_id_dir=by_id_dir, profile_yaml=profile_yaml,
            ) as ta:
                listing0 = _wait_dynamic_sessions(ta, timeout_s=20.0)
                ctx.note("ta-session-list-initial.json", str(listing0))
                if not (listing0.get("sessions") or []):
                    result = CaseResult(
                        "FAIL",
                        reason=f"throwaway 動態偵測未見任何 session（sandbox by-id 僅放 {by_id_name}）",
                        category="environment", reason_code="no_dynamic_session",
                        evidence={"listing_initial": "ta-session-list-initial.json"},
                    )
                else:
                    ta_com = _ta_com(listing0)  # 動態解析，勿硬編碼 COM0（round 3 教訓）
                    attach = ta.run("session", "attach", "--selector", ta_com, timeout=60)
                    attach_note = ctx.note("ta-attach.json", str(attach))

                    sess = _wait_state(ta, ta_com, "READY", timeout_s=60.0)
                    sess_note = ctx.note("ta-session-after-wait.json", str(sess))
                    if sess.get("state") != "READY":
                        wal_text = ta.wal_text()
                        wal_note = ctx.note("ta-wal-tail.txt", wal_text[-4000:] if wal_text else "(空)")
                        result = CaseResult(
                            "FAIL",
                            reason=f"補上有效帳密後 60s 內未見 READY（實得 state={sess.get('state')!r}, "
                                   f"last_error={sess.get('last_error')!r}，#140 回歸：登入路徑未恢復）",
                            category="test", reason_code="creds_fixed_still_not_ready",
                            evidence={"attach": attach_note, "session_after_wait": sess_note, "wal_tail": wal_note},
                        )
                    else:
                        result = CaseResult(
                            "PASS",
                            reason="補上有效帳密後 bcm 登入路徑恢復、session 回 READY",
                            evidence={"attach": attach_note, "session_after_wait": sess_note},
                        )
        except RuntimeError as exc:
            log_path = workdir / "daemon.log"
            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else "(無 daemon.log)"
            ctx.note("throwaway-start-failed.txt", f"{exc}\n\n--- daemon.log ---\n{log_text}")
            result = CaseResult("SKIP", reason=f"throwaway daemon 未在時限內就緒：{exc}",
                                category="environment", reason_code="throwaway_start_failed")
    finally:
        # 真帳密檔絕不可留在 evidence 目錄（case_dir 會隨報告留存）——收尾必刪。
        try:
            creds_path.unlink(missing_ok=True)
        except OSError:
            pass
        reclaim_fail = _reclaim_and_ensure_ready(ctx, com)
        if reclaim_fail is not None:
            result = reclaim_fail

    return result if result is not None else CaseResult(
        "FAIL", reason="case 邏輯未產生結果（不應發生）", category="test", reason_code="case_logic_error")
