"""sw_core.release_manifest — release manifest、部署程式碼 tree_sha256 與版本配對（P1 T18）。

對應規格 §9.5／OD-13 與 arch §10：發佈組合（bundle）位於 ``versions/<bundle_version>/``，
內含 ``release-manifest.json``，分別記錄 CLI／daemon 的 component 身分與 artifact digest。
本模組提供 Python 側三項能力：

- :func:`tree_sha256`：部署 Python 程式碼的樹狀雜湊（排除 ``__pycache__`` 目錄與 ``*.pyc``），
  演算法為 sha256 of sorted ``"<relpath>\\0<sha256>\\n"``（relpath 以 ``/`` 分隔、依 UTF-8 位元組序）。
- :func:`check_version_pairing`：doctor 的 ``version_pairing`` 檢查（非 advisory）；
  doctor 只在偵測到 manifest 時才附加此項，純 Python 安裝的輸出不變。
- :func:`daemon_launch_argv`：``daemon start`` 的 daemon 定位順序（DF-05，CLI-7.4）。

依賴僅限 stdlib；``sw_core.cli`` 只在解析預設 checkout 腳本路徑時延遲匯入，避免循環。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from importlib import resources
from typing import Any, Literal

MANIFEST_NAME = "release-manifest.json"
MANIFEST_ENV = "SERIALWRAP_RELEASE_MANIFEST"

# 必填欄位（與 contracts/release_manifest.schema.json 的 required 一致）。
# 每一列為 (父路徑, 必填鍵)；父路徑空字串代表 manifest 頂層。
_REQUIRED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("", (
        "schema_version", "bundle_version", "stage", "created_at", "target",
        "git_revision", "contract_revision", "components", "pairing",
    )),
    ("components", ("cli", "daemon", "python_cli_delegate", "assets", "supervisor")),
    ("components.cli", ("implementation", "version", "git_revision", "path", "sha256")),
    ("components.daemon", ("implementation", "version", "git_revision", "launcher", "entry", "runtime", "package")),
    ("components.daemon.launcher", ("path", "sha256")),
    ("components.daemon.runtime", ("path", "python_version", "sha256")),
    ("components.daemon.package", ("name", "version", "wheel_sha256", "tree_sha256")),
    ("components.python_cli_delegate", ("entry", "tree_sha256")),
    ("components.assets", ("skill_sha256", "skill_windows_sha256", "profiles_sha256")),
    ("components.supervisor", ("default_mode", "definition_sha256")),
    ("pairing", ("rule", "cli_version", "daemon_version")),
)

_HEX64 = re.compile(r"[0-9a-f]{64}")

# 必須為非空字串的葉節點（dotted path）。schema 的 type／pattern 與此對齊；load 時逐一驗證，
# 避免 launcher.path 等為 null／數字時於下游 ``.split()`` 拋出未捕捉的 AttributeError。
_STR_LEAVES: tuple[str, ...] = (
    "bundle_version", "stage", "created_at", "target", "git_revision", "contract_revision",
    "components.cli.implementation", "components.cli.version", "components.cli.git_revision",
    "components.cli.path", "components.cli.sha256",
    "components.daemon.implementation", "components.daemon.version", "components.daemon.git_revision",
    "components.daemon.launcher.path", "components.daemon.launcher.sha256",
    "components.daemon.runtime.path", "components.daemon.runtime.python_version",
    "components.daemon.runtime.sha256",
    "components.daemon.package.name", "components.daemon.package.version",
    "components.daemon.package.tree_sha256",
    "components.python_cli_delegate.tree_sha256",
    "components.assets.skill_sha256", "components.assets.skill_windows_sha256",
    "components.assets.profiles_sha256",
    "components.supervisor.default_mode", "components.supervisor.definition_sha256",
    "pairing.rule", "pairing.cli_version", "pairing.daemon_version",
)
# 必須為非空字串列表的葉節點（daemon／delegate 的 entry argv）。
_LIST_LEAVES: tuple[str, ...] = ("components.daemon.entry", "components.python_cli_delegate.entry")

# 套件內 SKILL 資產（wheel 安裝後仍可由 importlib.resources 讀取）。
_ASSET_RELPATHS: dict[str, str] = {
    "skill": "assets/skill/SKILL.md",
    "skill_windows": "assets/skill/SKILL_WINDOWS.md",
}


def _invalid(key: str) -> ValueError:
    return ValueError(f"invalid release manifest: {key}")


# ---------------------------------------------------------------------------
# tree_sha256
# ---------------------------------------------------------------------------


def file_sha256(path: str) -> str:
    """回傳檔案位元組的 sha256 十六進位字串。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raise_walk_error(err: OSError) -> None:
    """``os.walk`` 的 onerror：目錄讀取失敗（含根目錄不存在）直接拋出，不回傳不完整的雜湊。"""
    raise err


def _walk_files(base: str, rel_root: str) -> list[str]:
    """走訪 ``base`` 下所有一般檔，回傳相對於 ``rel_root`` 的 POSIX relpath。

    排除任一層的 ``__pycache__`` 目錄與 ``*.pyc`` 檔案；任何目錄讀取失敗都會拋出 ``OSError``。
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base, onerror=_raise_walk_error):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in filenames:
            if name.endswith(".pyc"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), rel_root)
            out.append(rel.replace(os.sep, "/"))
    return out


def _digest_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    """``pairs`` 為 ``(POSIX relpath, 絕對路徑)``；依 relpath 的 UTF-8 位元組序彙總。"""
    lines = [
        f"{rel}\0{file_sha256(abs_path)}\n"
        for rel, abs_path in sorted(pairs, key=lambda kv: kv[0].encode("utf-8"))
    ]
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def tree_sha256(root: str, files: Iterable[str] | None = None) -> str:
    """計算部署樹的 ``tree_sha256``（規格 §9.5）。

    ``files`` 為 ``None`` 時遞迴 ``root`` 下所有檔案（排除 ``__pycache__`` 與 ``*.pyc``）；
    否則只計算列出的 relpath（相對於 ``root``，以 ``/`` 分隔）。
    """
    if files is None:
        rels = _walk_files(root, root)
    else:
        rels = [str(f).replace(os.sep, "/") for f in files]
    return _digest_pairs((rel, os.path.join(root, *rel.split("/"))) for rel in rels)


def package_tree_sha256() -> str:
    """計算目前執行中的 ``sw_core`` 套件樹雜湊（relpath 以 ``sw_core/`` 開頭）。"""
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(pkg_dir)
    rels = _walk_files(pkg_dir, parent)
    return _digest_pairs((rel, os.path.join(parent, *rel.split("/"))) for rel in rels)


def package_asset_sha256(name: Literal["skill", "skill_windows"]) -> str:
    """回傳套件內 SKILL 資產的 sha256（以 importlib.resources 讀取，涵蓋 wheel 安裝）。"""
    node = resources.files(__package__)
    for part in _ASSET_RELPATHS[name].split("/"):
        node = node.joinpath(part)
    return hashlib.sha256(node.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# manifest 尋找與讀取
# ---------------------------------------------------------------------------


def find_release_manifest(
    env: Mapping[str, str] | None = None, argv0: str | None = None
) -> str | None:
    """尋找 release manifest：``SERIALWRAP_RELEASE_MANIFEST`` > ``argv0`` 所在 bundle 根 > ``None``。

    bundle 根為 ``realpath(argv0)`` 的上一層目錄（``bin/`` 的父目錄）。
    """
    environ = os.environ if env is None else env
    raw = (environ.get(MANIFEST_ENV) or "").strip()
    if raw and os.path.isfile(raw):
        return raw
    arg0 = sys.argv[0] if argv0 is None else argv0
    if arg0:
        bin_dir = os.path.dirname(os.path.realpath(arg0))
        candidate = os.path.join(bin_dir, os.pardir, MANIFEST_NAME)
        if os.path.isfile(candidate):
            return os.path.normpath(candidate)
    return None


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _lookup(data: dict[str, Any], dotted: str) -> Any:
    """依 dotted path 取值；呼叫前必須已由必填鍵檢查確認路徑存在。"""
    node: Any = data
    for part in dotted.split("."):
        node = node[part]
    return node


def load_release_manifest(path: str) -> dict[str, Any]:
    """讀取並驗證 manifest；缺欄或值不合法時拋 ``ValueError("invalid release manifest: <key>")``。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError) as exc:
        raise _invalid("unreadable") from exc
    if not isinstance(data, dict):
        raise _invalid("root")

    if data.get("schema_version") != 1 or isinstance(data.get("schema_version"), bool):
        raise _invalid("schema_version")

    for prefix, keys in _REQUIRED:
        node: Any = data
        if prefix:
            for part in prefix.split("."):
                node = node.get(part) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                raise _invalid(prefix)
        for key in keys:
            if key not in node:
                raise _invalid(f"{prefix}.{key}" if prefix else key)

    for dotted in _STR_LEAVES:
        if not _is_nonempty_str(_lookup(data, dotted)):
            raise _invalid(dotted)
    for dotted in _LIST_LEAVES:
        value = _lookup(data, dotted)
        if not isinstance(value, list) or not value or not all(_is_nonempty_str(x) for x in value):
            raise _invalid(dotted)
    wheel = _lookup(data, "components.daemon.package.wheel_sha256")
    if wheel is not None and not _is_nonempty_str(wheel):
        raise _invalid("components.daemon.package.wheel_sha256")

    pairing = data["pairing"]
    if pairing.get("rule") != "exact":
        raise _invalid("pairing.rule")
    return data


# ---------------------------------------------------------------------------
# version_pairing
# ---------------------------------------------------------------------------


def _resolve(base_dir: str, rel: str) -> str:
    """把 manifest 內的路徑解析為實際路徑。

    絕對路徑原樣保留（dev bundle 的 launcher 指向 repo 內的 ``serialwrapd.py``）；
    相對路徑以 ``base_dir`` 做字串 join；不做 realpath／canonicalize（venv 的 python3 為 symlink，解析後 ``-I -m`` 會失效）。
    """
    p = rel.replace("/", os.sep)
    return p if os.path.isabs(p) else os.path.join(base_dir, p)


def _same_file(
    sha256_of: Callable[[str], str], manifest_dir: str, rel: str, expected: str
) -> bool:
    try:
        return sha256_of(_resolve(manifest_dir, rel)) == expected
    except OSError:
        return False


def check_version_pairing(
    manifest: dict[str, Any],
    *,
    manifest_dir: str,
    cli_version: str,
    daemon_version: str | None,
    sha256_of: Callable[[str], str] = file_sha256,
    package_tree: Callable[[], str] = package_tree_sha256,
    asset_sha256: Callable[[Literal["skill", "skill_windows"]], str] = package_asset_sha256,
) -> dict[str, Any]:
    """回傳 doctor 的 ``version_pairing`` 項目（判定規則凍結於 T18 計畫）。

    ``pairing.rule`` 已由 :func:`load_release_manifest` 驗證為 ``exact``。
    ``profiles_sha256`` 與 ``supervisor.definition_sha256`` 在 Python 側只驗格式（arch §10.3）。
    """
    pairing = manifest["pairing"]
    if pairing.get("rule") != "exact":
        raise _invalid("pairing.rule")
    comps = manifest["components"]
    bundle = manifest["bundle_version"]
    mismatches: list[str] = []

    # 規格 §9.5、arch §10.3：CLI 與 daemon 皆須等於 bundle_version（不得以 pairing 自洽的舊版通過）。
    cli_declared = pairing["cli_version"] == bundle and comps["cli"]["version"] == bundle
    if cli_version != bundle or not cli_declared:
        mismatches.append("cli_version")
    daemon_declared = pairing["daemon_version"] == bundle and comps["daemon"]["version"] == bundle
    if daemon_version is None:
        if not daemon_declared:
            mismatches.append("daemon_version")
        mismatches.append("daemon_unreachable")
    elif daemon_version != bundle or not daemon_declared:
        mismatches.append("daemon_version")

    if not _same_file(sha256_of, manifest_dir, comps["cli"]["path"], comps["cli"]["sha256"]):
        mismatches.append("cli_sha256")
    daemon = comps["daemon"]
    if not _same_file(sha256_of, manifest_dir, daemon["launcher"]["path"], daemon["launcher"]["sha256"]):
        mismatches.append("launcher_sha256")
    if not _same_file(sha256_of, manifest_dir, daemon["runtime"]["path"], daemon["runtime"]["sha256"]):
        mismatches.append("runtime_sha256")

    actual_tree = package_tree()
    if daemon["package"]["tree_sha256"] != actual_tree:
        mismatches.append("package_tree_sha256")
    if comps["python_cli_delegate"]["tree_sha256"] != actual_tree:
        mismatches.append("python_cli_delegate_tree_sha256")

    assets = comps["assets"]
    if assets["skill_sha256"] != asset_sha256("skill"):
        mismatches.append("skill_sha256")
    if assets["skill_windows_sha256"] != asset_sha256("skill_windows"):
        mismatches.append("skill_windows_sha256")
    profiles = assets["profiles_sha256"]
    if not (isinstance(profiles, str) and _HEX64.fullmatch(profiles)):
        mismatches.append("profiles_sha256_format")
    definition = comps["supervisor"]["definition_sha256"]
    if not (isinstance(definition, str) and _HEX64.fullmatch(definition)):
        mismatches.append("supervisor_definition_sha256_format")

    return {
        "check": "version_pairing",
        "ok": not mismatches,
        "advisory": False,
        "detail": "ok" if not mismatches else ", ".join(mismatches),
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# DF-05 daemon 定位
# ---------------------------------------------------------------------------


def daemon_launch_argv(
    *,
    env: Mapping[str, str] | None = None,
    argv0: str | None = None,
    python: str | None = None,
    frozen: bool | None = None,
    script_path: str | None = None,
) -> list[str]:
    """回傳 ``daemon start`` 的 daemon 啟動前綴 argv（不含 ``--socket`` 等參數）。

    順序（DF-05）：

    1. manifest 的 ``components.daemon.launcher.path`` 存在 → 該絕對路徑；
    2. frozen（PyInstaller）→ ``sys.executable`` 同層 ``serialwrapd.exe``，否則 PATH 上的
       ``serialwrapd``；都找不到回 ``[]``（呼叫端回 ``DAEMON_BINARY_NOT_FOUND``）；
    3. ``realpath(argv0)`` 同目錄的可執行 ``serialwrapd``（Windows 為 ``serialwrapd.exe``）；
    4. 原始碼 checkout 的 ``serialwrapd.py`` 存在 → ``[python, script]``；
    5. 否則 ``[python, "-m", "sw_core.daemon"]``（pipx 等安裝佈局）。
    """
    environ = os.environ if env is None else env
    arg0 = sys.argv[0] if argv0 is None else argv0
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    py = python or sys.executable

    manifest_path = find_release_manifest(env=environ, argv0=arg0)
    if manifest_path is not None:
        try:
            manifest = load_release_manifest(manifest_path)
            launcher = _resolve(
                os.path.dirname(manifest_path), manifest["components"]["daemon"]["launcher"]["path"]
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            launcher = None  # 無效 manifest 由 doctor 回報；啟動流程退回後續順序
        if launcher is not None and os.path.isfile(launcher):
            return [os.path.abspath(launcher)]

    if is_frozen:
        sibling = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "serialwrapd.exe")
        if os.path.isfile(sibling):
            return [sibling]
        found = shutil.which("serialwrapd")
        return [found] if found else []

    bin_name = "serialwrapd.exe" if os.name == "nt" else "serialwrapd"
    same_dir = os.path.join(os.path.dirname(os.path.realpath(arg0)), bin_name)
    if os.path.isfile(same_dir) and os.access(same_dir, os.X_OK):
        return [same_dir]

    if script_path is None:
        from sw_core import cli  # noqa: PLC0415 — 延遲匯入避免 cli ↔ release_manifest 循環

        script_path = cli._daemon_script_path()
    if os.path.isfile(script_path):
        return [py, script_path]
    return [py, "-m", "sw_core.daemon"]
