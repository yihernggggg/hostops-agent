#!/usr/bin/env bash
# Protect main: every change goes through a PR whose CI checks pass. Safe to re-run (PUT replaces).
#
#   scripts/protect_main.sh [owner/repo]
#
# Notes on the choices:
# - contexts are CI job *names*; tests/test_branch_protection.py keeps them in step with ci.yml.
# - strict: the PR branch must be up to date with main, so checks ran against what will be merged.
# - enforce_admins: without it the repo owner bypasses everything and a direct push succeeds.
# - required approvals are 0: a solo owner cannot approve their own PR. The PR + checks are the gate.
set -euo pipefail

REPO="${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"

gh api --method PUT "repos/${REPO}/branches/main/protection" \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "checks": [
      { "context": "lint, types, tests" },
      { "context": "offline eval gate" },
      { "context": "docker build" }
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON

echo "main is protected on ${REPO}"
