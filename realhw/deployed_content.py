"""量測此次執行使用的已部署內容與 daemon 映像。"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import pathlib
import stat
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Callable

SCHEMA = "deployed-content.v1"
WINDOWS_ENTRY_FILES = frozenset({"serialwrap.exe", "serialwrapd.exe", "current-release.json"})
_PATH_CLASS = type(pathlib.Path("."))


class DeployedContentError(RuntimeError):
    """安裝內容含無法安全摘要的檔案。"""


def _path(value: os.PathLike[str] | str) -> pathlib.Path:
    if isinstance(value, pathlib.PurePath):
        return value  # type: ignore[return-value]
    return _PATH_CLASS(os.fspath(value))  # type: ignore[call-arg,return-value]


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_files(root: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    entries: list[tuple[str, pathlib.Path]] = []
    if not root.is_dir():
        raise DeployedContentError(f"non-regular file: .")
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise DeployedContentError(f"non-regular file: {rel}") from exc
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise DeployedContentError(f"non-regular file: {rel}")
        entries.append((rel, path))
    entries.sort(key=lambda item: item[0].encode("utf-8"))
    return entries


def tree_digests(bundle_dir: pathlib.Path) -> tuple[str, str]:
    """單次走訪後計算完整 bundle 與 runtime 的 sorted path/hash 摘要。"""
    root = _path(bundle_dir).resolve()
    entries = _regular_files(root)
    bundle = bytearray()
    runtime = bytearray()
    for rel, path in entries:
        line = f"{rel}\0{file_sha256(path)}\n".encode("utf-8")
        bundle.extend(line)
        if rel not in {"release-manifest.json", "SHA256SUMS"}:
            runtime.extend(line)
    return hashlib.sha256(bundle).hexdigest(), hashlib.sha256(runtime).hexdigest()


def install_dir(cli_exe: pathlib.Path) -> pathlib.Path | None:
    """依 POSIX symlink bundle 或 Windows current-release sidecar 尋找安裝。

    Windows 的 `dir` 為相對安裝根（`bin` 的上一層）的 `versions\\<V>`（plan 04 Task 17；rollback.rs 寫入 `versions/<V>`），
    故以 `cli.parent.parent` 為基準；反斜線分隔先正規化為 `/`，絕對路徑照舊。
    """
    cli = _path(cli_exe).resolve()
    if os.name == "nt":
        pointer = cli.parent / "current-release.json"
        if not pointer.is_file():
            return None
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
            candidate = _path(str(data["dir"]).replace("\\", "/"))
            if not candidate.is_absolute():
                candidate = cli.parent.parent / candidate
            candidate = candidate.resolve()
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return candidate if (candidate / "release-manifest.json").is_file() else None
    candidate = cli.parent.parent
    return candidate if (candidate / "release-manifest.json").is_file() else None


def entry_dir_check(cli_exe: pathlib.Path, bundle_dir: pathlib.Path) -> tuple[str | None, str | None]:
    """驗證 Windows 穩定 bin 目錄無旁載內容且入口複本與 bundle 相同。"""
    if os.name != "nt":
        return None, None
    cli = _path(cli_exe).resolve()
    bundle = _path(bundle_dir).resolve()
    entry_dir = cli.parent
    allowed = WINDOWS_ENTRY_FILES
    try:
        for path in entry_dir.iterdir():
            rel = path.name
            mode = path.lstat().st_mode
            if rel not in allowed or not stat.S_ISREG(mode):
                return None, f"entry_dir_extra: {rel}"
        for name in ("serialwrap.exe", "serialwrapd.exe"):
            stable_copy = entry_dir / name
            bundle_copy = bundle / "bin" / name
            if stable_copy.exists() or bundle_copy.exists():
                if not stable_copy.is_file() or not bundle_copy.is_file():
                    return None, f"entry_copy_mismatch: {name}"
                if file_sha256(stable_copy) != file_sha256(bundle_copy):
                    return None, f"entry_copy_mismatch: {name}"
        bundle_sha, _ = tree_digests(entry_dir)
        return bundle_sha, None
    except (OSError, DeployedContentError) as exc:
        return None, f"entry_dir_invalid: {exc}"


def _manifest_component(manifest: Mapping[str, Any], *path: str) -> Mapping[str, Any] | None:
    current: Any = manifest
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current if isinstance(current, Mapping) else None


def _is_within(path: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def measure(
    cli_exe: pathlib.Path,
    *,
    daemon_argv: Sequence[str] | None = None,
    now: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """重新雜湊安裝樹，並核對實際 CLI／daemon 與 release manifest。"""
    configured = os.fspath(cli_exe)
    cli = _path(cli_exe).expanduser().resolve()
    daemon_args = list(daemon_argv) if daemon_argv is not None else None
    result: dict[str, Any] = {
        "schema_version": SCHEMA,
        "measured_at": now() if now else datetime.now(timezone.utc).isoformat(),
        "cli_configured": configured,
        "cli_resolved": str(cli),
        "cli_sha256": None,
        "install_dir": None,
        "release_manifest_sha256": None,
        "bundle_tree_sha256": None,
        "runtime_tree_sha256": None,
        "entry_dir_sha256": None,
        "daemon_configured": daemon_args,
        "daemon_resolved": None,
        "daemon_sha256": None,
        "error": None,
    }
    reason: str | None = None
    install = install_dir(cli)
    if install is None:
        reason = "unresolved"
    else:
        result["install_dir"] = str(install)
        manifest_path = install / "release-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, Mapping):
                raise ValueError("manifest root must be an object")
            expected_cli = _manifest_component(manifest, "components", "cli")
            expected_cli_sha = expected_cli.get("sha256") if expected_cli else None
            actual_cli_sha = file_sha256(cli)
            if not isinstance(expected_cli_sha, str) or actual_cli_sha != expected_cli_sha:
                reason = "cli_mismatch"
            if reason is None:
                try:
                    bundle_sha, runtime_sha = tree_digests(install)
                except DeployedContentError as exc:
                    reason = f"tree_invalid: {exc.args[0].partition(': ')[2]}"
                if reason is None:
                    entry_sha, entry_error = entry_dir_check(cli, install)
                    if entry_error:
                        reason = entry_error
                    else:
                        result["cli_sha256"] = actual_cli_sha
                        result["release_manifest_sha256"] = file_sha256(manifest_path)
                        result["bundle_tree_sha256"] = bundle_sha
                        result["runtime_tree_sha256"] = runtime_sha
                        result["entry_dir_sha256"] = entry_sha
                        if daemon_args:
                            daemon_path = _path(daemon_args[0]).expanduser().resolve()
                            result["daemon_resolved"] = str(daemon_path)
                            result["daemon_sha256"] = file_sha256(daemon_path) if daemon_path.is_file() else None
                            if not _is_within(daemon_path, install):
                                reason = "daemon_outside_install"
                            else:
                                components = manifest.get("components", {})
                                daemon_components = components.get("daemon", {}) if isinstance(components, Mapping) else {}
                                allowed = {
                                    digest
                                    for entry in daemon_components.values()
                                    if isinstance(entry, Mapping)
                                    for digest in (entry.get("sha256"),)
                                    if isinstance(digest, str)
                                } if isinstance(daemon_components, Mapping) else set()
                                if (
                                    not isinstance(result["daemon_sha256"], str)
                                    or result["daemon_sha256"] not in allowed
                                ):
                                    reason = "daemon_mismatch"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            reason = "unresolved"
    if reason is not None:
        result["error"] = reason
        for key in (
            "cli_sha256", "release_manifest_sha256", "bundle_tree_sha256",
            "runtime_tree_sha256", "entry_dir_sha256", "daemon_sha256",
        ):
            result[key] = None
    return result


def live_daemon(pid: int, install: pathlib.Path | None) -> dict[str, Any]:
    """量測執行中 daemon 行程的實際 executable image。"""
    image: str | None = None
    error: str | None = None
    try:
        if os.name == "nt":
            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not process:
                raise OSError("OpenProcess failed")
            try:
                buffer = ctypes.create_unicode_buffer(32768)
                size = ctypes.c_ulong(len(buffer))
                if not ctypes.windll.kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                    raise OSError("QueryFullProcessImageNameW failed")
                image = buffer.value
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        else:
            image = os.path.realpath(f"/proc/{int(pid)}/exe")
            if not os.path.isfile(image):
                raise OSError("process image is unavailable")
    except Exception:
        error = "image_unavailable"
    resolved = _path(image).resolve() if image else None
    install_path = _path(install).resolve() if install is not None else None
    in_install = bool(resolved and install_path and _is_within(resolved, install_path))
    return {
        "pid": int(pid),
        "image_path": str(resolved) if resolved else None,
        "image_sha256": file_sha256(resolved) if resolved and resolved.is_file() else None,
        "in_install": in_install,
        "error": error,
    }


def bracket(
    start: Mapping[str, Any], end: Mapping[str, Any], *, live: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    return {"start": dict(start), "end": dict(end), "daemon_live": [dict(item) for item in live]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="量測 serialwrap 實際部署內容")
    parser.add_argument("--cli", required=True)
    parser.add_argument("--daemon-pid", type=int)
    parser.add_argument("--daemon", nargs="*")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = measure(_path(args.cli), daemon_argv=args.daemon)
    live: dict[str, Any] | None = None
    if args.daemon_pid is not None:
        live = live_daemon(
            args.daemon_pid,
            _path(result["install_dir"]) if result["install_dir"] else None,
        )
    target = _path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    if live is not None:
        live_target = target.with_name(target.name + ".live-daemon.json")
        live_target.write_text(
            json.dumps(live, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
