"""Exercise pull deployments with a real temporary Git repository and fake Docker."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/update.sh"


@pytest.fixture
def deployment(tmp_path):
    if not shutil.which("flock"):
        pytest.skip("Deployment tests require Linux util-linux flock")
    source = tmp_path / "source"
    source.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], stderr=subprocess.DEVNULL).decode().strip()
    git("init", "-b", "main")
    git("config", "user.name", "Deployment Test")
    git("config", "user.email", "test@example.invalid")
    (source / "compose.yaml").write_text("services: {}\n")
    git("add", ".")
    git("commit", "-m", "initial")
    first = git("rev-parse", "HEAD")
    config = tmp_path / "server.env"
    config.write_text("DAILY_LIMIT=30\n")
    executables = tmp_path / "bin"
    executables.mkdir()
    docker = executables / "docker"
    docker.write_text('''#!/usr/bin/env bash
set -eu
echo "$*" >> "$DOCKER_TEST_LOG"
if [[ "$1" == build && "${FAIL_BUILD:-0}" == 1 ]]; then exit 1; fi
if [[ "$1" == inspect ]]; then echo healthy; fi
if [[ "$1" == compose ]]; then
  if [[ "$*" == *"ps -q"* ]]; then echo fake-container; fi
  if [[ "$*" == *"up -d"* && "${FAIL_NEW_IMAGE:-}" == "$BOT_IMAGE" ]]; then exit 1; fi
fi
''')
    docker.chmod(0o755)
    env = dict(os.environ, PATH=f"{executables}:{os.environ['PATH']}",
               CONFLICTOGEN_REPO_URL=str(source), CONFLICTOGEN_STATE_DIR=str(tmp_path / "state"),
               CONFLICTOGEN_ENV_FILE=str(config), DOCKER_TEST_LOG=str(tmp_path / "docker.log"))
    def run(**extra):
        return subprocess.run(["bash", str(SCRIPT)], env=env | extra, capture_output=True, text=True)
    def advance():
        (source / "revision").write_text("new version")
        git("add", ".")
        git("commit", "-m", "update")
        return git("rev-parse", "HEAD")
    return tmp_path, first, run, advance


def test_deploy_and_unchanged_main_does_not_rebuild(deployment):
    root, first, run, _ = deployment
    assert run().returncode == 0
    assert (root / "state/current").read_text().strip() == first
    assert run().returncode == 0
    commands = (root / "docker.log").read_text().splitlines()
    assert sum(c.startswith("build ") for c in commands) == 1


def test_build_failure_preserves_current_container(deployment):
    root, first, run, advance = deployment
    assert run().returncode == 0
    advance()
    before = (root / "docker.log").read_text()
    assert run(FAIL_BUILD="1").returncode != 0
    assert (root / "state/current").read_text().strip() == first
    assert "up -d" not in (root / "docker.log").read_text()[len(before):]


def test_failed_start_attempts_previous_image(deployment):
    root, first, run, advance = deployment
    assert run().returncode == 0
    new = advance()
    assert run(FAIL_NEW_IMAGE=f"conflictogen-bot:{new}").returncode != 0
    assert (root / "state/current").read_text().strip() == first
    commands = (root / "docker.log").read_text().splitlines()
    startups = [c for c in commands if "up -d" in c]
    assert new in startups[-2] and first in startups[-1]
