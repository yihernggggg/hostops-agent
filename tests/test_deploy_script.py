"""scripts/deploy_on_instance.sh runs as root on the instance, so it must reject unexpected input."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_on_instance.sh"
SHA = "a" * 40
IMAGE = f"036402400786.dkr.ecr.ap-southeast-1.amazonaws.com/hostops:{SHA}"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "HOSTOPS_DEPLOY_DRY_RUN": "1"}
    cmd = ["/bin/bash", str(SCRIPT), *args]
    # Arguments are fixed test strings, including deliberately hostile ones the script must reject.
    return subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)  # noqa: S603


def test_valid_arguments_pass_validation() -> None:
    r = run(SHA, IMAGE, "ap-southeast-1", "/hostops/api-token", "yihernggggg/hostops-agent")
    assert r.returncode == 0, r.stderr
    assert "dry run" in r.stdout


def test_rejects_a_short_or_non_hex_sha() -> None:
    assert run("abc123", IMAGE, "ap-southeast-1", "/hostops/api-token", "a/b").returncode == 2
    assert run("g" * 40, IMAGE, "ap-southeast-1", "/hostops/api-token", "a/b").returncode == 2


def test_rejects_an_image_that_is_not_this_sha_in_ecr() -> None:
    other = IMAGE.replace(SHA, "b" * 40)
    assert run(SHA, other, "ap-southeast-1", "/hostops/api-token", "a/b").returncode == 2
    assert run(SHA, f"docker.io/evil/hostops:{SHA}", "ap-southeast-1", "/x", "a/b").returncode == 2


def test_rejects_shell_metacharacters() -> None:
    assert run(SHA, IMAGE, "ap-southeast-1; rm -rf /", "/hostops/api-token", "a/b").returncode == 2
    assert run(SHA, IMAGE, "ap-southeast-1", "/x$(id)", "a/b").returncode == 2
    assert run(SHA, IMAGE, "ap-southeast-1", "/x", "a/b;id").returncode == 2


def test_wrong_argument_count_is_rejected() -> None:
    assert run(SHA).returncode == 2
