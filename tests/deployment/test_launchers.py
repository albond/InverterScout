"""Static and executable checks for the cross-platform Quick Start launchers."""

from __future__ import annotations

import gzip
import os
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
START_SH = ROOT / "start.sh"
START_BAT = ROOT / "start.bat"
REMOTE_INSTALL = ROOT / "scripts" / "deployment" / "remote-install.sh"


def run_bash(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_shell_launchers_have_valid_bash_syntax():
    for script in (START_SH, REMOTE_INSTALL):
        result = run_bash("-n", str(script))
        assert result.returncode == 0, result.stderr


def test_unix_launcher_help_does_not_require_docker():
    result = run_bash(str(START_SH), "--help")

    assert result.returncode == 0
    assert "Docker running on this computer" in result.stdout
    assert "remote Linux Docker host over SSH" in result.stdout


def test_unix_launcher_suggests_a_free_port_when_the_requested_port_is_occupied(
    tmp_path,
):
    requested_port = 49140
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_lsof = fake_bin / "lsof"
    fake_lsof.write_text(
        f"""#!/usr/bin/env bash
if [[ " $* " == *" -iTCP:{requested_port} "* ]]; then
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_lsof.chmod(0o755)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["INVERTERSCOUT_LAUNCHER_LIBRARY"] = "1"

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source ./start.sh; select_web_port; printf "SELECTED=%s\\n" "$SELECTED_WEB_PORT"',
        ],
        cwd=ROOT,
        env=environment,
        input=f"{requested_port}\ny\n",
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"Port {requested_port} is occupied" in result.stdout
    assert f"SELECTED={requested_port + 1}" in result.stdout


def test_unix_launcher_lan_mode_uses_the_detected_private_address():
    environment = os.environ.copy()
    environment["INVERTERSCOUT_LAUNCHER_LIBRARY"] = "1"

    result = subprocess.run(
        [
            "bash",
            "-c",
            "source ./start.sh; "
            "detect_local_lan_ipv4() { printf '192.168.1.20'; }; "
            "address_belongs_to_host() { return 0; }; "
            "select_bind_address local; "
            'printf "SELECTED=%s\\n" "$SELECTED_BIND_ADDRESS"',
        ],
        cwd=ROOT,
        env=environment,
        input="2\n\n",
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Other devices on this trusted home LAN" in result.stdout
    assert "SELECTED=192.168.1.20" in result.stdout


def test_unix_launcher_lan_mode_rejects_public_addresses():
    environment = os.environ.copy()
    environment["INVERTERSCOUT_LAUNCHER_LIBRARY"] = "1"

    result = subprocess.run(
        ["bash", "-c", "source ./start.sh; is_private_ipv4 203.0.113.20"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0


def test_unix_launcher_home_nas_or_arduino_defaults_to_lan_publication():
    environment = os.environ.copy()
    environment["INVERTERSCOUT_LAUNCHER_LIBRARY"] = "1"

    result = subprocess.run(
        [
            "bash",
            "-c",
            "source ./start.sh; "
            "SSH_HOST=192.168.1.30; "
            "select_bind_address home_remote; "
            'printf "SELECTED=%s\\n" "$SELECTED_BIND_ADDRESS"',
        ],
        cwd=ROOT,
        env=environment,
        input="\n\n",
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "recommended for a NAS or Arduino Linux board" in result.stdout
    assert "SELECTED=192.168.1.30" in result.stdout


def test_launchers_do_not_read_or_store_an_ssh_password():
    unix_launcher = START_SH.read_text(encoding="utf-8")
    windows_launcher = START_BAT.read_text(encoding="utf-8")
    combined = f"{unix_launcher}\n{windows_launcher}".lower()

    assert "sshpass" not in combined
    assert "ssh_password" not in combined
    assert 'set /p "password' not in combined
    assert "read -s" not in combined
    assert "entered securely by openssh and never stored" in combined


def test_unix_remote_bundle_excludes_macos_extended_attributes(tmp_path):
    bundle = tmp_path / "inverterscout.tar.gz"
    environment = os.environ.copy()
    environment["INVERTERSCOUT_LAUNCHER_LIBRARY"] = "1"
    environment["TEST_BUNDLE"] = str(bundle)

    result = subprocess.run(
        ["bash", "-c", 'source ./start.sh; create_remote_bundle "$TEST_BUNDLE"'],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    raw_tar = gzip.decompress(bundle.read_bytes())
    assert b"LIBARCHIVE.xattr" not in raw_tar
    assert b"SCHILY.xattr" not in raw_tar


def test_windows_launcher_contains_the_same_deployment_contract():
    content = START_BAT.read_text(encoding="utf-8")

    for required in (
        "docker info",
        "docker compose",
        "scp -P",
        "ssh -p",
        "remote-install.sh",
        "INVERTERSCOUT_BIND_ADDRESS",
        "INVERTERSCOUT_WEB_PORT",
        "Test inverter reachability from the container now?",
        "--self-test",
    ):
        assert required in content

    for windows_lan_safety in (
        ":detectLocalLanIPv4",
        ":validatePrivateIPv4",
        "New-NetFirewallRule",
        "-RemoteAddress LocalSubnet",
        "-Profile Private",
        "Other devices on this trusted home LAN",
        "Docker-capable Arduino Linux board",
        "home_remote",
        "COPYFILE_DISABLE",
    ):
        assert windows_lan_safety in content


def test_remote_installer_rejects_invalid_arguments_before_using_docker(tmp_path):
    archive = tmp_path / "not-an-approved-path.tar.gz"
    archive.write_bytes(b"not an archive")

    result = run_bash(
        str(REMOTE_INSTALL),
        str(archive),
        "127.0.0.1",
        "invalid-port",
        "192.0.2.10",
        "8000",
        "required",
        "20260809000000-1",
    )

    assert result.returncode != 0
    assert "Unexpected upload archive path" in result.stderr
    assert archive.exists()


def test_remote_installer_selects_another_port_and_keeps_inverter_target_out_of_env(
    tmp_path, monkeypatch
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
if [[ "$1" == "inspect" ]]; then
  printf 'healthy\\n'
elif [[ " $* " == *" ps -a -q inverterscout "* ]]; then
  printf 'fake-container-id\\n'
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    requested_port = 49150
    fake_lsof = fake_bin / "lsof"
    fake_lsof.write_text(
        f"""#!/usr/bin/env bash
if [[ " $* " == *" -iTCP:{requested_port} "* ]]; then
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_lsof.chmod(0o755)

    release_id = f"20260809000000-{os.getpid()}"
    archive = Path(f"/tmp/inverterscout-{release_id}.tar.gz")
    if archive.exists():
        archive.unlink()
    with tarfile.open(archive, "w:gz") as bundle:
        for path in (
            ROOT / ".dockerignore",
            ROOT / "Dockerfile",
            ROOT / "LICENSE",
            ROOT / "README.md",
            ROOT / "docker-compose.yml",
            ROOT / "pyproject.toml",
            ROOT / "src",
        ):
            bundle.add(path, arcname=path.relative_to(ROOT))

    remote_home = tmp_path / "remote-home"
    remote_home.mkdir()
    monkeypatch.setenv("HOME", str(remote_home))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(docker_log))
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    result = run_bash(
        str(REMOTE_INSTALL),
        str(archive),
        "127.0.0.1",
        str(requested_port),
        "192.0.2.10",
        "8000",
        "required",
        release_id,
    )

    assert result.returncode == 0, result.stderr
    assert f"INVERTERSCOUT_WEB_PORT={requested_port + 1}" in result.stdout
    assert not archive.exists()
    release = remote_home / ".local/share/inverterscout/releases" / release_id
    env_content = (release / ".env").read_text(encoding="utf-8")
    assert env_content == (
        f"INVERTERSCOUT_BIND_ADDRESS=127.0.0.1\nINVERTERSCOUT_WEB_PORT={requested_port + 1}\n"
    )
    assert "192.0.2.10" not in env_content
    assert (remote_home / ".local/share/inverterscout/current").resolve() == release
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "compose -p inverterscout build inverterscout" in docker_calls
    assert "compose -p inverterscout run --rm --no-deps" in docker_calls
    assert "compose -p inverterscout up -d --no-build" in docker_calls


def test_compose_publishes_only_the_local_web_interface():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    published_ports = [
        line.strip() for line in compose.splitlines() if "INVERTERSCOUT_BIND_ADDRESS" in line
    ]
    assert published_ports == [
        '- "${INVERTERSCOUT_BIND_ADDRESS:-127.0.0.1}:${INVERTERSCOUT_WEB_PORT:-8080}:8080"'
    ]
    assert ":8000" not in compose
