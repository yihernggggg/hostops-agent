"""Security rules for infra/hostops.yml, enforced on every PR.

Text checks (no YAML parser: CloudFormation short tags like !Ref need a custom loader). The aim is to
catch the edits that matter: opening the instance to the internet, SSH, IMDSv1, or widening who can
assume the deploy role.
"""

import re
from pathlib import Path

TEMPLATE = (Path(__file__).resolve().parents[1] / "infra" / "hostops.yml").read_text()


def block(name: str) -> str:
    """Text of one top-level resource, up to the next resource at the same indent."""
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \w+:\n|^Outputs:)", TEMPLATE, re.M | re.S)
    assert match, name
    return match.group(1)


def test_instance_accepts_no_inbound_traffic() -> None:
    # Everything (API, Grafana, Prometheus) is reached through SSM port forwarding. A bearer token
    # over plain HTTP would be readable on the network path, so nothing is published.
    sg = block("InstanceSecurityGroup")
    assert "SecurityGroupIngress" not in sg
    assert "AWS::EC2::SecurityGroupIngress" not in TEMPLATE
    assert "AllowedCidr" not in TEMPLATE


def test_imdsv2_required_and_containers_cannot_reach_it() -> None:
    lt = block("LaunchTemplate")
    assert re.search(r"HttpTokens:\s*required", lt)
    assert re.search(r"HttpPutResponseHopLimit:\s*1\b", lt)
    assert re.search(r"Encrypted:\s*true", lt)


def test_deploy_role_trusts_only_main_of_this_repo() -> None:
    role = block("DeployRole")
    assert "token.actions.githubusercontent.com:aud: sts.amazonaws.com" in role
    # GitHub's immutable subject format: owner and repo carry their numeric IDs, so a re-created repo or
    # account with the same name cannot assume the role.
    assert "token.actions.githubusercontent.com:sub: !Sub ${GitHubSubjectPrefix}:ref:refs/heads/main" in role
    assert "StringLike" not in role  # no wildcards in the trust condition
    assert "AdministratorAccess" not in role


def test_deploy_role_can_only_run_commands_on_this_instance() -> None:
    send = re.search(r"Action: ssm:SendCommand\n\s+Resource:\n((?:\s+- .*\n)+)", block("DeployRole"))
    assert send
    resources = send.group(1)
    assert "instance/${Instance}" in resources
    assert "document/AWS-RunShellScript" in resources
    assert '"*"' not in resources


def test_image_tags_are_immutable_and_scanned() -> None:
    repo = block("ImageRepository")
    assert re.search(r"ImageTagMutability:\s*IMMUTABLE", repo)
    assert re.search(r"ScanOnPush:\s*true", repo)


def test_compose_plugin_download_is_checksum_verified() -> None:
    user_data = block("Instance")
    assert "sha256sum -c -" in user_data
    assert "${ComposeSha256}" in user_data


def test_subject_prefix_defaults_to_the_immutable_ids_of_this_repo() -> None:
    prefix = block("GitHubSubjectPrefix")
    assert "Default: repo:yihernggggg@165023723/hostops-agent@1468683679" in prefix
    assert r"^repo:[A-Za-z0-9_.-]+@[0-9]+/[A-Za-z0-9_.-]+@[0-9]+$" in prefix
