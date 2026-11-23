"""Rules for .github/workflows/deploy.yml: OIDC only, least privilege, no unsafe interpolation."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
RUN_BLOCKS = re.findall(r"run: \|\n((?:\s{10,}.*\n?)+)", WORKFLOW)


def test_triggers_on_main_and_manual_rollback_only() -> None:
    assert re.search(r"push:\s*\n\s*branches:\s*\[main\]", WORKFLOW)
    assert "workflow_dispatch:" in WORKFLOW
    assert "pull_request" not in WORKFLOW  # PR code must never get deploy credentials


def test_uses_oidc_and_no_stored_aws_keys() -> None:
    assert re.search(r"^permissions:\s*\n\s*contents:\s*read\s*\n\s*id-token:\s*write\s*$", WORKFLOW, re.M)
    assert "aws-actions/configure-aws-credentials@v6" in WORKFLOW
    assert "role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}" in WORKFLOW
    for forbidden in ("aws-access-key-id", "aws-secret-access-key", "AWS_SECRET_ACCESS_KEY", "secrets."):
        assert forbidden not in WORKFLOW, forbidden


def test_deploys_never_overlap_or_get_cancelled_midway() -> None:
    assert re.search(
        r"concurrency:\s*\n\s*group:\s*deploy-production\s*\n\s*cancel-in-progress:\s*false", WORKFLOW
    )


def test_expressions_never_appear_inside_run_scripts() -> None:
    # Inputs and context reach shell scripts through env: only, never ${{ }} inside run: (injection).
    assert RUN_BLOCKS
    for block in RUN_BLOCKS:
        assert "${{" not in block, block


def test_manual_sha_input_is_validated() -> None:
    assert "INPUT_SHA: ${{ inputs.sha }}" in WORKFLOW
    assert '[[ "$SHA" =~ ^[0-9a-f]{40}$ ]]' in WORKFLOW


def test_deploy_runs_the_repo_script_and_a_separate_smoke_test() -> None:
    assert "scripts/deploy_on_instance.sh" in WORKFLOW
    assert re.search(r"name: Smoke test /healthz", WORKFLOW)
    assert "http://127.0.0.1:8000/healthz" in WORKFLOW
