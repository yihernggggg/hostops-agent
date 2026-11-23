#!/usr/bin/env bash
# Deploys one commit on the EC2 instance. Run as root by the deploy workflow through SSM Run Command.
#
#   deploy_on_instance.sh <commit-sha> <ecr-image-uri> <region> <ssm-token-parameter> <owner/repo>
#
# Every argument is validated before use: this runs as root and its inputs come from CI.
# Rollback is the same call with an older commit SHA (its image is still in ECR).
set -euo pipefail

usage() { echo "usage: $0 <sha> <image> <region> <token-param> <owner/repo>" >&2; exit 2; }
[[ $# -eq 5 ]] || usage
SHA="$1" IMAGE="$2" REGION="$3" TOKEN_PARAM="$4" REPO="$5"

[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid sha" >&2; exit 2; }
[[ "$IMAGE" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9._/-]+:([0-9a-f]{40})$ ]] \
  || { echo "image must be an ECR URI tagged with a commit sha" >&2; exit 2; }
[[ "${BASH_REMATCH[1]}" == "$SHA" ]] || { echo "image tag does not match sha" >&2; exit 2; }
[[ "$REGION" =~ ^[a-z]{2}-[a-z]+-[0-9]$ ]] || { echo "invalid region" >&2; exit 2; }
[[ "$TOKEN_PARAM" =~ ^/[A-Za-z0-9_./-]+$ ]] || { echo "invalid parameter name" >&2; exit 2; }
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "invalid repo" >&2; exit 2; }

if [[ "${HOSTOPS_DEPLOY_DRY_RUN:-}" == "1" ]]; then
  echo "dry run: arguments valid for $SHA"
  exit 0
fi

ROOT=/opt/hostops
RELEASE="$ROOT/releases/$SHA"
REGISTRY="${IMAGE%%/*}"

echo "== fetching source $REPO@$SHA"
rm -rf "$RELEASE" && mkdir -p "$RELEASE"
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$SHA" | tar -xz -C "$RELEASE" --strip-components=1

echo "== writing API token from SSM $TOKEN_PARAM"
install -d -m 0700 "$RELEASE/secrets"
umask 022  # containers run as non-root users and must be able to read the mounted secret file
aws ssm get-parameter --region "$REGION" --name "$TOKEN_PARAM" --with-decryption \
  --query Parameter.Value --output text > "$RELEASE/secrets/api_token"

echo "== pulling $IMAGE"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"
docker pull "$IMAGE"

echo "== starting the stack"
cd "$RELEASE"
HOSTOPS_IMAGE="$IMAGE" docker compose -p hostops up -d --no-build --remove-orphans

echo "== waiting for /healthz"
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/healthz; then
    echo
    ln -sfn "$RELEASE" "$ROOT/current"
    # Keep the five most recent releases on disk.
    ls -1dt "$ROOT"/releases/* | tail -n +6 | xargs -r rm -rf
    docker image prune -f >/dev/null
    echo "== deployed $SHA"
    exit 0
  fi
  sleep 2
done

echo "== /healthz did not come up; container status:" >&2
docker compose -p hostops ps >&2
docker compose -p hostops logs --tail 50 hostops >&2
exit 1
