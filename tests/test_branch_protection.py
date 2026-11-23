"""The branch protection script must require exactly the checks CI produces.

Renaming a CI job silently drops it from protection (GitHub just waits for a check that never
reports, or stops requiring it). This test keeps the two in step.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "protect_main.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def ci_job_names() -> set[str]:
    return set(re.findall(r"^    name:\s*(.+?)\s*$", CI.read_text(), re.M))


def required_checks() -> set[str]:
    return set(re.findall(r'"context":\s*"([^"]+)"', SCRIPT.read_text()))


def test_required_checks_match_ci_jobs() -> None:
    assert ci_job_names() == {"lint, types, tests", "offline eval gate", "docker build"}
    assert required_checks() == ci_job_names()


def test_protection_applies_to_admins_and_blocks_direct_and_force_pushes() -> None:
    text = SCRIPT.read_text()
    assert re.search(r'"enforce_admins":\s*true', text)
    assert re.search(r'"strict":\s*true', text)
    assert re.search(r'"required_pull_request_reviews":\s*\{', text)
    assert re.search(r'"allow_force_pushes":\s*false', text)
    assert re.search(r'"allow_deletions":\s*false', text)
    assert "branches/main/protection" in text
