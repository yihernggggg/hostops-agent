# Deploying hostops-agent to AWS

One EC2 instance in `ap-southeast-1` runs the same `docker-compose.yml` as local development (service, Prometheus,
Alertmanager, Grafana). Infrastructure is `infra/hostops.yml` (CloudFormation stack `hostops`). Deploys run from
`.github/workflows/deploy.yml` on every merge to `main`.

```
merge to main ──▶ GitHub Actions ──OIDC──▶ IAM DeployRole (main of this repo only)
                       │
                       ├─ docker build, tag <commit-sha>, push ──▶ ECR (immutable tags, scan on push)
                       └─ SSM Run Command ──▶ EC2: scripts/deploy_on_instance.sh <sha> <image> ...
                                                   ├─ token from SSM Parameter Store (/hostops/api-token)
                                                   ├─ docker compose up (all ports on 127.0.0.1)
                                                   └─ wait for /healthz
                       └─ SSM Run Command ──▶ smoke test: curl 127.0.0.1:8000/healthz
```

## Access model

- **No inbound ports.** The security group has egress rules only; there is no SSH. The API token is sent over plain
  HTTP, so the API is never published.
- **Operators** reach the instance through SSM Session Manager, authenticated by IAM.
- **GitHub Actions** never holds AWS keys. It exchanges its OIDC token for a one-hour session on `DeployRole`.

### IAM trust policy of the deploy role

```json
{
  "Effect": "Allow",
  "Principal": { "Federated": "arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com" },
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": {
    "StringEquals": {
      "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub": "repo:yihernggggg@165023723/hostops-agent@1468683679:ref:refs/heads/main"
    }
  }
}
```

`StringEquals` on `sub`, with no wildcards: only workflow runs on `main` of this repository can assume the role.
Pull requests, other branches, tags, forks and other repositories are refused.

The repository uses GitHub's immutable OIDC subject format, so the subject carries the numeric owner and repository
IDs (`yihernggggg@165023723/hostops-agent@1468683679`). A repository re-created under the same name has a different
ID and cannot assume the role. The current prefix is shown by
`gh api repos/yihernggggg/hostops-agent/actions/oidc/customization/sub` (`sub_claim_prefix`). The role's permissions:

| Permission | Scope |
| --- | --- |
| Push and read images | the stack's ECR repository only (`ecr:GetAuthorizationToken` needs `*`) |
| `ssm:SendCommand` | the stack's instance and the `AWS-RunShellScript` document only |
| `ssm:GetCommandInvocation`, `ssm:ListCommandInvocations` | `*` (read-only, no resource-level scoping) |

`tests/test_infra.py` fails if the trust condition is widened, ingress is added, IMDSv2 is relaxed, or `SendCommand`
gets a wildcard resource.

## First-time setup

1. Billing alarm and budget (done before the first deploy): CloudWatch alarm `billing-over-1-usd` and budget
   `credit-burn-usd5`, both notifying the `billing-alerts` SNS topic in `us-east-1`.
2. API token (never in git):
   ```bash
   aws ssm put-parameter --region ap-southeast-1 --name /hostops/api-token --type SecureString \
     --value "file://<path to a file holding a random token>"
   ```
3. Stack:
   ```bash
   aws cloudformation deploy --region ap-southeast-1 --stack-name hostops \
     --template-file infra/hostops.yml --capabilities CAPABILITY_IAM \
     --parameter-overrides VpcId=<default-vpc-id>
   ```
4. GitHub repository variables, from the stack outputs:
   `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`, `ECR_REPOSITORY_URI`, `INSTANCE_ID`.

## Using the live service

Install the Session Manager plugin once (`brew install --cask session-manager-plugin`), then forward a port:

```bash
# API on localhost:8001 (send the token from SSM as a bearer token)
aws ssm start-session --region ap-southeast-1 --target <instance-id> \
  --document-name AWS-StartPortForwardingSession --parameters portNumber=8000,localPortNumber=8001

# Grafana on localhost:3001, Prometheus on localhost:9091
aws ssm start-session --region ap-southeast-1 --target <instance-id> \
  --document-name AWS-StartPortForwardingSession --parameters portNumber=3000,localPortNumber=3001
```

Read the token without echoing it into shell history:
`aws ssm get-parameter --region ap-southeast-1 --name /hostops/api-token --with-decryption --query Parameter.Value --output text`.

## Rollback

Every deployed commit has an image in ECR (the 15 most recent are kept). To roll back, open
**Actions → deploy → Run workflow**, keep the branch on `main`, and enter the SHA of the earlier commit. The workflow
reuses that commit's image and runs the same deploy and smoke test. From the CLI:

```bash
gh workflow run deploy.yml --ref main -f sha=<earlier-commit-sha>
```

## Cost and teardown

- The account is on the AWS Free plan; usage draws down credits. The main costs are the t3.small instance and its
  public IPv4 address, which it needs for outbound access to ECR, SSM and GitHub.
- Stop the instance between demos: `aws ec2 stop-instances --region ap-southeast-1 --instance-ids <instance-id>`.
  Start it again with `start-instances`. The stack and images remain.
- Remove everything: `aws cloudformation delete-stack --region ap-southeast-1 --stack-name hostops`.
  The ECR repository is emptied on delete. The SSM parameter, billing alarm and budget are separate and stay.
