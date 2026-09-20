from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile

import pytest
import yaml


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _script_path(name: str) -> Path:
    path = _REPO_ROOT / "tools" / name
    if not path.is_file():
        pytest.fail(
            f"tools/{name} 尚未實作；Task 3 先以 RED 測試鎖定 bench 工具腳本的 CLI 契約"
        )
    return path


def _run_script(name: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="bench-tools-home-") as home_dir:
        clean_env = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": home_dir,
            "USER": "tester",
            "LOGNAME": "tester",
            "PYTHONIOENCODING": "utf-8",
            "TERM": "dumb",
        }
        if env is not None:
            clean_env.update(env)
        return subprocess.run(
            ["bash", str(_script_path(name)), *args],
            cwd=_REPO_ROOT,
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_bench_issue_help_and_missing_domain_contract() -> None:
    help_result = _run_script("bench-issue.sh", "--help")

    assert help_result.returncode == 0
    help_output = _combined_output(help_result).lower()
    assert "usage" in help_output
    assert "domain" in help_output

    run_result = _run_script("bench-issue.sh", "eit-02")

    assert run_result.returncode != 0
    assert "domain" in _combined_output(run_result).lower()


def test_bench_enroll_help_and_required_bundle_contract() -> None:
    help_result = _run_script("bench-enroll.sh", "--help")

    assert help_result.returncode == 0
    help_output = _combined_output(help_result).lower()
    assert "usage" in help_output
    assert "--bundle" in help_output

    run_result = _run_script("bench-enroll.sh")

    assert run_result.returncode != 0
    assert "bundle" in _combined_output(run_result).lower()


def test_bench_issue_dry_run_writes_bundle_and_increments_local_port(tmp_path) -> None:
    handoff_root = tmp_path / "handoff"
    benches_path = tmp_path / "benches.yaml"
    benches_path.write_text(
        """\
benches:
  eit-01:
    target: tester@eit-01.example.com
    remote_socket: /run/serialwrap/serialwrapd.sock
    local_port: 7777
    ssh_opts:
      - "-o"
      - "ProxyCommand=cloudflared access ssh --hostname %h"
      - "-i"
      - "/tmp/id_ed25519"
    autossh: true
""",
        encoding="utf-8",
    )

    identity_file = tmp_path / "id_ed25519"
    identity_file.write_text("private-key-placeholder\n", encoding="utf-8")
    host_public_key = tmp_path / "id_ed25519.pub"
    host_public_key.write_text("ssh-ed25519 AAAATEST bench-host\n", encoding="utf-8")

    result = _run_script(
        "bench-issue.sh",
        "--dry-run",
        "eit-02",
        env={
            "BENCH_ISSUE_DOMAIN": "example.com",
            "BENCH_ISSUE_TARGET_USER": "tester",
            "BENCH_ISSUE_HANDOFF_ROOT": str(handoff_root),
            "SERIALWRAP_BENCHES_FILE": str(benches_path),
            "BENCH_ISSUE_HOST_PUBLIC_KEY": str(host_public_key),
            "BENCH_ISSUE_IDENTITY_FILE": str(identity_file),
            "BENCH_ISSUE_DRY_RUN_UUID": "11111111-2222-4333-8444-555555555555",
        },
    )

    assert result.returncode == 0, _combined_output(result)
    bundle_dir = handoff_root / "eit-02"
    assert (bundle_dir / "11111111-2222-4333-8444-555555555555.json").is_file()
    assert (bundle_dir / "host_authorized_key.pub").read_text(encoding="utf-8") == host_public_key.read_text(encoding="utf-8")
    config_text = (bundle_dir / "config.yml").read_text(encoding="utf-8")
    assert "hostname: eit-02.example.com" in config_text
    assert "service: ssh://localhost:22" in config_text

    benches = yaml.safe_load(benches_path.read_text(encoding="utf-8"))
    entry = benches["benches"]["eit-02"]
    assert entry["target"] == "tester@eit-02.example.com"
    assert entry["remote_socket"] == "/run/serialwrap/serialwrapd.sock"
    assert entry["local_port"] == 7778
    assert entry["autossh"] is True
    assert entry["ssh_opts"] == [
        "-o",
        "ProxyCommand=cloudflared access ssh --hostname %h",
        "-i",
        str(identity_file),
    ]


def test_bench_issue_rejects_missing_identity_private_key(tmp_path) -> None:
    host_public_key = tmp_path / "id_ed25519.pub"
    host_public_key.write_text("ssh-ed25519 AAAATEST bench-host\n", encoding="utf-8")
    missing_identity = tmp_path / "missing_id_ed25519"

    result = _run_script(
        "bench-issue.sh",
        "--dry-run",
        "eit-03",
        env={
            "BENCH_ISSUE_DOMAIN": "example.com",
            "BENCH_ISSUE_TARGET_USER": "tester",
            "BENCH_ISSUE_HANDOFF_ROOT": str(tmp_path / "handoff"),
            "SERIALWRAP_BENCHES_FILE": str(tmp_path / "benches.yaml"),
            "BENCH_ISSUE_HOST_PUBLIC_KEY": str(host_public_key),
            "BENCH_ISSUE_IDENTITY_FILE": str(missing_identity),
        },
    )

    assert result.returncode != 0
    assert "ssh 私鑰" in _combined_output(result)


def test_bench_issue_rejects_schema_invalid_existing_benches(tmp_path) -> None:
    benches_path = tmp_path / "benches.yaml"
    benches_path.write_text(
        """\
benches:
  eit-01:
    target: tester@eit-01.example.com
    remote_socket: /run/serialwrap/serialwrapd.sock
    local_port: 7777
    ssh_opts:
      - "-o"
      - "ProxyCommand=cloudflared access ssh --hostname %h"
      - "-i"
      - "/tmp/id_ed25519"
    autossh: true
    cloudflare:
      tunnel: eit-01
""",
        encoding="utf-8",
    )
    identity_file = tmp_path / "id_ed25519"
    identity_file.write_text("private-key-placeholder\n", encoding="utf-8")
    host_public_key = tmp_path / "id_ed25519.pub"
    host_public_key.write_text("ssh-ed25519 AAAATEST bench-host\n", encoding="utf-8")
    handoff_root = tmp_path / "handoff"

    result = _run_script(
        "bench-issue.sh",
        "--dry-run",
        "eit-02",
        env={
            "BENCH_ISSUE_DOMAIN": "example.com",
            "BENCH_ISSUE_TARGET_USER": "tester",
            "BENCH_ISSUE_HANDOFF_ROOT": str(handoff_root),
            "SERIALWRAP_BENCHES_FILE": str(benches_path),
            "BENCH_ISSUE_HOST_PUBLIC_KEY": str(host_public_key),
            "BENCH_ISSUE_IDENTITY_FILE": str(identity_file),
        },
    )

    assert result.returncode != 0
    output = _combined_output(result)
    assert "provider-neutral schema" in output
    assert not handoff_root.exists()


def test_bench_issue_reports_cloudflared_create_failure_output(tmp_path) -> None:
    identity_file = tmp_path / "id_ed25519"
    identity_file.write_text("private-key-placeholder\n", encoding="utf-8")
    host_public_key = tmp_path / "id_ed25519.pub"
    host_public_key.write_text("ssh-ed25519 AAAATEST bench-host\n", encoding="utf-8")
    fake_cloudflared = tmp_path / "fake-cloudflared.sh"
    _write_executable(
        fake_cloudflared,
        "#!/usr/bin/env bash\n"
        "echo 'create stdout marker'\n"
        "echo 'create stderr marker' >&2\n"
        "exit 23\n",
    )

    result = _run_script(
        "bench-issue.sh",
        "eit-04",
        env={
            "BENCH_ISSUE_DOMAIN": "example.com",
            "BENCH_ISSUE_TARGET_USER": "tester",
            "BENCH_ISSUE_HANDOFF_ROOT": str(tmp_path / "handoff"),
            "SERIALWRAP_BENCHES_FILE": str(tmp_path / "benches.yaml"),
            "BENCH_ISSUE_HOST_PUBLIC_KEY": str(host_public_key),
            "BENCH_ISSUE_IDENTITY_FILE": str(identity_file),
            "BENCH_ISSUE_CLOUDFLARED_BIN": str(fake_cloudflared),
            "BENCH_ISSUE_CLOUDFLARED_HOME": str(tmp_path / "cloudflared-home"),
        },
    )

    assert result.returncode != 0
    output = _combined_output(result)
    assert "create stdout marker" in output
    assert "create stderr marker" in output
    assert "cloudflared tunnel create 失敗" in output


@pytest.mark.parametrize(
    "invalid_target",
    [
        " tester@eit-03.example.com",
        "tester@@eit-03.example.com",
        "@eit-03.example.com",
        "tester@",
        "tester@eit-03.example.com:22",
        "-tester@eit-03.example.com",
        "tester@foo/bar",
    ],
)
def test_bench_issue_rejects_invalid_target_override(tmp_path, invalid_target: str) -> None:
    identity_file = tmp_path / "id_ed25519"
    identity_file.write_text("private-key-placeholder\n", encoding="utf-8")
    host_public_key = tmp_path / "id_ed25519.pub"
    host_public_key.write_text("ssh-ed25519 AAAATEST bench-host\n", encoding="utf-8")

    result = _run_script(
        "bench-issue.sh",
        "--dry-run",
        "--target",
        invalid_target,
        "eit-03",
        env={
            "BENCH_ISSUE_DOMAIN": "example.com",
            "BENCH_ISSUE_HANDOFF_ROOT": str(tmp_path / "handoff"),
            "SERIALWRAP_BENCHES_FILE": str(tmp_path / "benches.yaml"),
            "BENCH_ISSUE_HOST_PUBLIC_KEY": str(host_public_key),
            "BENCH_ISSUE_IDENTITY_FILE": str(identity_file),
        },
    )

    assert result.returncode != 0
    assert "user@host" in _combined_output(result)


def test_bench_issue_route_failure_preserves_old_bundle_and_cleans_up_tunnel(tmp_path) -> None:
    handoff_root = tmp_path / "handoff"
    bundle_dir = handoff_root / "eit-05"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "keep.txt").write_text("old bundle\n", encoding="utf-8")

    benches_path = tmp_path / "benches.yaml"
    benches_path.write_text("benches: {}\n", encoding="utf-8")
    identity_file = tmp_path / "id_ed25519"
    identity_file.write_text("private-key-placeholder\n", encoding="utf-8")
    host_public_key = tmp_path / "id_ed25519.pub"
    host_public_key.write_text("ssh-ed25519 AAAATEST bench-host\n", encoding="utf-8")
    provider_log = tmp_path / "cloudflared.log"
    fake_cloudflared = tmp_path / "fake-cloudflared.sh"
    _write_executable(
        fake_cloudflared,
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_CLOUDFLARED_LOG\"\n"
        "if [[ \"$1 $2\" == 'tunnel create' ]]; then\n"
        "  echo 'Created tunnel 33333333-4444-4555-8666-777777777777'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1 $2 $3\" == 'tunnel route dns' ]]; then\n"
        "  echo 'route stderr marker' >&2\n"
        "  exit 41\n"
        "fi\n"
        "if [[ \"$1 $2\" == 'tunnel delete' ]]; then\n"
        "  echo 'cleanup delete marker'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )

    result = _run_script(
        "bench-issue.sh",
        "eit-05",
        env={
            "BENCH_ISSUE_DOMAIN": "example.com",
            "BENCH_ISSUE_TARGET_USER": "tester",
            "BENCH_ISSUE_HANDOFF_ROOT": str(handoff_root),
            "SERIALWRAP_BENCHES_FILE": str(benches_path),
            "BENCH_ISSUE_HOST_PUBLIC_KEY": str(host_public_key),
            "BENCH_ISSUE_IDENTITY_FILE": str(identity_file),
            "BENCH_ISSUE_CLOUDFLARED_BIN": str(fake_cloudflared),
            "BENCH_ISSUE_CLOUDFLARED_HOME": str(tmp_path / "cloudflared-home"),
            "FAKE_CLOUDFLARED_LOG": str(provider_log),
        },
    )

    assert result.returncode != 0
    output = _combined_output(result)
    assert "route stderr marker" in output
    assert "cloudflared tunnel route dns 失敗" in output
    assert (bundle_dir / "keep.txt").read_text(encoding="utf-8") == "old bundle\n"
    assert not (bundle_dir / "config.yml").exists()
    assert yaml.safe_load(benches_path.read_text(encoding="utf-8")) == {"benches": {}}
    provider_actions = provider_log.read_text(encoding="utf-8")
    assert "tunnel delete 33333333-4444-4555-8666-777777777777" in provider_actions


def test_bench_issue_post_route_local_failure_restores_old_bundle_and_benches(tmp_path) -> None:
    handoff_root = tmp_path / "handoff"
    bundle_dir = handoff_root / "eit-06"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "keep.txt").write_text("old bundle\n", encoding="utf-8")

    benches_dir = tmp_path / "cfg"
    benches_dir.mkdir()
    benches_path = benches_dir / "benches.yaml"
    benches_path.write_text(
        """\
benches:
  eit-01:
    target: tester@eit-01.example.com
    remote_socket: /run/serialwrap/serialwrapd.sock
    local_port: 7777
    ssh_opts:
      - "-o"
      - "ProxyCommand=cloudflared access ssh --hostname %h"
      - "-i"
      - "/tmp/id_ed25519"
    autossh: true
""",
        encoding="utf-8",
    )

    identity_file = tmp_path / "id_ed25519"
    identity_file.write_text("private-key-placeholder\n", encoding="utf-8")
    host_public_key = tmp_path / "id_ed25519.pub"
    host_public_key.write_text("ssh-ed25519 AAAATEST bench-host\n", encoding="utf-8")
    cloudflared_home = tmp_path / "cloudflared-home"
    cloudflared_home.mkdir()
    (cloudflared_home / "44444444-5555-4666-8777-888888888888.json").write_text(
        '{"TunnelID":"44444444-5555-4666-8777-888888888888"}\n',
        encoding="utf-8",
    )
    provider_log = tmp_path / "cloudflared.log"
    fake_cloudflared = tmp_path / "fake-cloudflared.sh"
    _write_executable(
        fake_cloudflared,
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_CLOUDFLARED_LOG\"\n"
        "if [[ \"$1 $2\" == 'tunnel create' ]]; then\n"
        "  echo 'Created tunnel 44444444-5555-4666-8777-888888888888'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1 $2 $3\" == 'tunnel route dns' ]]; then\n"
        "  echo 'route ok'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1 $2\" == 'tunnel delete' ]]; then\n"
        "  echo 'cleanup delete marker'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )

    benches_dir.chmod(0o555)
    try:
        result = _run_script(
            "bench-issue.sh",
            "eit-06",
            env={
                "BENCH_ISSUE_DOMAIN": "example.com",
                "BENCH_ISSUE_TARGET_USER": "tester",
                "BENCH_ISSUE_HANDOFF_ROOT": str(handoff_root),
                "SERIALWRAP_BENCHES_FILE": str(benches_path),
                "BENCH_ISSUE_HOST_PUBLIC_KEY": str(host_public_key),
                "BENCH_ISSUE_IDENTITY_FILE": str(identity_file),
                "BENCH_ISSUE_CLOUDFLARED_BIN": str(fake_cloudflared),
                "BENCH_ISSUE_CLOUDFLARED_HOME": str(cloudflared_home),
                "FAKE_CLOUDFLARED_LOG": str(provider_log),
            },
        )
    finally:
        benches_dir.chmod(0o755)

    assert result.returncode != 0
    assert (bundle_dir / "keep.txt").read_text(encoding="utf-8") == "old bundle\n"
    assert not (bundle_dir / "config.yml").exists()
    assert yaml.safe_load(benches_path.read_text(encoding="utf-8")) == {
        "benches": {
            "eit-01": {
                "target": "tester@eit-01.example.com",
                "remote_socket": "/run/serialwrap/serialwrapd.sock",
                "local_port": 7777,
                "ssh_opts": [
                    "-o",
                    "ProxyCommand=cloudflared access ssh --hostname %h",
                    "-i",
                    "/tmp/id_ed25519",
                ],
                "autossh": True,
            }
        }
    }
    provider_actions = provider_log.read_text(encoding="utf-8")
    assert "tunnel delete 44444444-5555-4666-8777-888888888888" in provider_actions


def test_bench_enroll_rejects_missing_bundle_with_clean_guard(tmp_path) -> None:
    missing_bundle = tmp_path / "missing-bundle"

    result = _run_script(
        "bench-enroll.sh",
        "--bundle",
        str(missing_bundle),
    )

    output = _combined_output(result)
    assert result.returncode != 0
    assert f"bundle 目錄不存在：{missing_bundle}" in output
    assert "cd:" not in output


def _write_enroll_bundle(bundle_dir: Path) -> None:
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "22222222-3333-4444-8555-666666666666.json").write_text(
        '{"TunnelID":"22222222-3333-4444-8555-666666666666"}\n',
        encoding="utf-8",
    )
    (bundle_dir / "config.yml").write_text(
        """\
tunnel: 22222222-3333-4444-8555-666666666666
credentials-file: /etc/cloudflared/22222222-3333-4444-8555-666666666666.json
ingress:
  - hostname: eit-02.example.com
    service: ssh://localhost:22
  - service: http_status:404
""",
        encoding="utf-8",
    )
    (bundle_dir / "host_authorized_key.pub").write_text(
        "ssh-ed25519 AAAATEST remote-host\n",
        encoding="utf-8",
    )


def test_bench_enroll_dry_run_writes_sandbox_and_reports_pass(tmp_path) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_enroll_bundle(bundle_dir)
    sandbox_root = tmp_path / "sandbox"
    sshd_config_path = sandbox_root / "etc" / "ssh" / "sshd_config"
    sshd_config_path.parent.mkdir(parents=True)
    sshd_config_path.write_text(
        "# baseline sshd config\n"
        "Match User nobody\n"
        "    PasswordAuthentication yes\n",
        encoding="utf-8",
    )

    result = _run_script(
        "bench-enroll.sh",
        "--bundle",
        str(bundle_dir),
        "--dry-run",
        env={
            "BENCH_ENROLL_DRY_RUN_ROOT": str(sandbox_root),
            "BENCH_ENROLL_CP1_LOG": "ok\nRegistered tunnel connection\nok",
            "BENCH_ENROLL_CP2_RESULT": "pass",
            "BENCH_ENROLL_LOGIN_USER": "tester",
        },
    )

    assert result.returncode == 0, _combined_output(result)
    output = _combined_output(result)
    assert "CP-1 PASS" in output
    assert "CP-2 PASS" in output

    config_dst = sandbox_root / "etc" / "cloudflared" / "config.yml"
    credentials_dst = sandbox_root / "etc" / "cloudflared" / "22222222-3333-4444-8555-666666666666.json"
    sshd_dropin_path = sandbox_root / "etc" / "ssh" / "sshd_config.d" / "serialwrap-bench-key-only.conf"
    authorized_keys = sandbox_root / "home" / "tester" / ".ssh" / "authorized_keys"
    assert credentials_dst.is_file()
    assert config_dst.is_file()
    assert (
        "credentials-file: "
        f"{credentials_dst}"
    ) in config_dst.read_text(encoding="utf-8")
    sshd_main_text = sshd_config_path.read_text(encoding="utf-8")
    assert sshd_main_text.startswith(f"Include {sshd_dropin_path}\n")
    assert "Match User nobody" in sshd_main_text
    sshd_dropin_text = sshd_dropin_path.read_text(encoding="utf-8")
    assert "PasswordAuthentication no" in sshd_dropin_text
    assert "PubkeyAuthentication yes" in sshd_dropin_text
    assert "ssh-ed25519 AAAATEST remote-host" in authorized_keys.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("cp1_log", "cp2_result", "expected_marker"),
    [
        ("still starting", "pass", "CP-1 FAIL"),
        ("Registered tunnel connection", "fail", "CP-2 FAIL"),
    ],
)
def test_bench_enroll_dry_run_checkpoint_failures_exit_nonzero(
    tmp_path, cp1_log: str, cp2_result: str, expected_marker: str
) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_enroll_bundle(bundle_dir)

    result = _run_script(
        "bench-enroll.sh",
        "--bundle",
        str(bundle_dir),
        "--dry-run",
        env={
            "BENCH_ENROLL_DRY_RUN_ROOT": str(tmp_path / "sandbox"),
            "BENCH_ENROLL_CP1_LOG": cp1_log,
            "BENCH_ENROLL_CP2_RESULT": cp2_result,
        },
    )

    assert result.returncode != 0
    assert expected_marker in _combined_output(result)


def test_bench_enroll_live_mode_uses_login_user_paths_and_fresh_cp1_polling(tmp_path) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_enroll_bundle(bundle_dir)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()

    sudo_log = tmp_path / "sudo.log"
    apt_log = tmp_path / "apt.log"
    cloudflared_log = tmp_path / "cloudflared.log"
    systemctl_log = tmp_path / "systemctl.log"
    ssh_log = tmp_path / "ssh.log"
    journal_args_log = tmp_path / "journal-args.log"
    journal_count_file = tmp_path / "journal-count.txt"

    _write_executable(
        fake_bin / "fake-sudo.sh",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_SUDO_LOG\"\n"
        "exec \"$@\"\n",
    )
    _write_executable(
        fake_bin / "fake-apt-get.sh",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_APT_LOG\"\n",
    )
    _write_executable(
        fake_bin / "fake-cloudflared.sh",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_CLOUDFLARED_LOG\"\n",
    )
    _write_executable(
        fake_bin / "fake-systemctl.sh",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_SYSTEMCTL_LOG\"\n",
    )
    _write_executable(
        fake_bin / "fake-journalctl.sh",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_JOURNAL_ARGS_LOG\"\n"
        "if [[ \" $* \" == *\" --show-cursor \"* ]]; then\n"
        "  printf '%s\\n' '-- cursor: cursor-123'\n"
        "  exit 0\n"
        "fi\n"
        "count=0\n"
        "if [[ -f \"$FAKE_JOURNAL_COUNT_FILE\" ]]; then\n"
        "  count=$(cat \"$FAKE_JOURNAL_COUNT_FILE\")\n"
        "fi\n"
        "printf '%s' \"$((count + 1))\" > \"$FAKE_JOURNAL_COUNT_FILE\"\n"
        "if [[ \" $* \" != *\" --after-cursor cursor-123 \"* ]]; then\n"
        "  printf 'Registered tunnel connection\\n'\n"
        "  exit 0\n"
        "fi\n"
        "case \"$count\" in\n"
        "  0) printf 'old logs before fresh registration\\n' ;;\n"
        "  1) printf 'cloudflared still starting\\n' ;;\n"
        "  *) printf 'Registered tunnel connection\\n' ;;\n"
        "esac\n",
    )
    _write_executable(
        fake_bin / "fake-ssh-keygen.sh",
        "#!/usr/bin/env bash\n"
        "out=''\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  if [[ \"$1\" == '-f' && $# -ge 2 ]]; then\n"
        "    out=$2\n"
        "    shift 2\n"
        "    continue\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "[[ -n \"$out\" ]]\n"
        "printf 'PRIVATE\\n' > \"$out\"\n"
        "printf 'ssh-ed25519 AAAATEMP temporary-self-check\\n' > \"$out.pub\"\n",
    )
    _write_executable(
        fake_bin / "fake-ssh.sh",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_SSH_LOG\"\n",
    )

    login_home = tmp_path / "home" / "benchsvc"
    wrong_home = tmp_path / "root-home"
    sshd_config_path = tmp_path / "etc" / "ssh" / "sshd_config"
    sshd_config_path.parent.mkdir(parents=True)
    sshd_config_path.write_text(
        "# keep existing config\n"
        "Match User root\n"
        "    X11Forwarding no\n",
        encoding="utf-8",
    )

    result = _run_script(
        "bench-enroll.sh",
        "--bundle",
        str(bundle_dir),
        env={
            "HOME": str(wrong_home),
            "SUDO_USER": "benchsvc",
            "BENCH_ENROLL_LOGIN_HOME": str(login_home),
            "BENCH_ENROLL_CLOUDFLARED_DIR": str(tmp_path / "etc" / "cloudflared"),
            "BENCH_ENROLL_SSHD_CONFIG_PATH": str(sshd_config_path),
            "BENCH_ENROLL_APT_GET_BIN": str(fake_bin / "fake-apt-get.sh"),
            "BENCH_ENROLL_CLOUDFLARED_BIN": str(fake_bin / "fake-cloudflared.sh"),
            "BENCH_ENROLL_JOURNALCTL_BIN": str(fake_bin / "fake-journalctl.sh"),
            "BENCH_ENROLL_SYSTEMCTL_BIN": str(fake_bin / "fake-systemctl.sh"),
            "BENCH_ENROLL_SSH_BIN": str(fake_bin / "fake-ssh.sh"),
            "BENCH_ENROLL_SSH_KEYGEN_BIN": str(fake_bin / "fake-ssh-keygen.sh"),
            "BENCH_ENROLL_SUDO_BIN": str(fake_bin / "fake-sudo.sh"),
            "BENCH_ENROLL_CP1_TIMEOUT_SEC": "1",
            "BENCH_ENROLL_CP1_POLL_INTERVAL_SEC": "0.01",
            "FAKE_SUDO_LOG": str(sudo_log),
            "FAKE_APT_LOG": str(apt_log),
            "FAKE_CLOUDFLARED_LOG": str(cloudflared_log),
            "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
            "FAKE_SSH_LOG": str(ssh_log),
            "FAKE_JOURNAL_ARGS_LOG": str(journal_args_log),
            "FAKE_JOURNAL_COUNT_FILE": str(journal_count_file),
        },
    )

    assert result.returncode == 0, _combined_output(result)
    output = _combined_output(result)
    assert "CP-1 PASS" in output
    assert "CP-2 PASS" in output
    assert "login_user=benchsvc" in output

    authorized_keys = login_home / ".ssh" / "authorized_keys"
    assert authorized_keys.is_file()
    authorized_text = authorized_keys.read_text(encoding="utf-8")
    assert "ssh-ed25519 AAAATEST remote-host" in authorized_text
    assert "temporary-self-check" not in authorized_text
    assert not (wrong_home / ".ssh" / "authorized_keys").exists()

    ssh_args = ssh_log.read_text(encoding="utf-8")
    assert "-F /dev/null" in ssh_args
    assert "-o IdentitiesOnly=yes" in ssh_args
    assert "-o PreferredAuthentications=publickey" in ssh_args
    assert "-o PubkeyAuthentication=yes" in ssh_args
    assert "-o PasswordAuthentication=no" in ssh_args
    assert "-o KbdInteractiveAuthentication=no" in ssh_args
    assert "benchsvc@localhost" in ssh_args

    journal_args = journal_args_log.read_text(encoding="utf-8").splitlines()
    assert any("--show-cursor" in line for line in journal_args)
    assert any("--after-cursor cursor-123" in line for line in journal_args)
    assert int(journal_count_file.read_text(encoding="utf-8")) >= 2

    sshd_dropin_path = tmp_path / "etc" / "ssh" / "sshd_config.d" / "serialwrap-bench-key-only.conf"
    sshd_main_text = sshd_config_path.read_text(encoding="utf-8")
    assert sshd_main_text.startswith(f"Include {sshd_dropin_path}\n")
    assert "Match User root" in sshd_main_text
    sshd_dropin_text = sshd_dropin_path.read_text(encoding="utf-8")
    assert "PasswordAuthentication no" in sshd_dropin_text
    assert "PubkeyAuthentication yes" in sshd_dropin_text
