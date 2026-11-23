"""CI must run exactly the definition of done from CLAUDE.md, plus the Docker build.

If someone drops a check from the workflow to make a PR go green, this test goes red.
"""

import re
from pathlib import Path

CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

DEFINITION_OF_DONE = [
    "ruff check .",
    "ruff format --check .",
    "mypy hostops",
    "pytest -q",
    "python -m evals.run --provider offline",
]


def test_ci_runs_every_definition_of_done_command() -> None:
    text = CI.read_text()
    for command in DEFINITION_OF_DONE:
        assert re.search(rf"run:\s*{re.escape(command)}\s*$", text, re.M), command


def test_ci_triggers_on_pull_requests_and_pushes_to_main() -> None:
    text = CI.read_text()
    assert re.search(r"^\s*pull_request:", text, re.M)
    assert re.search(r"push:\s*\n\s*branches:\s*\[main\]", text)


def test_ci_uploads_the_eval_report_and_builds_the_image() -> None:
    text = CI.read_text()
    assert "actions/upload-artifact" in text
    assert "evals/report.md" in text
    assert "docker build" in text


def test_ci_never_softens_a_failing_step() -> None:
    assert "continue-on-error" not in CI.read_text()
    assert "|| true" not in CI.read_text()


def test_ci_token_is_read_only() -> None:
    assert re.search(r"^permissions:\s*\n\s*contents:\s*read\s*$", CI.read_text(), re.M)
