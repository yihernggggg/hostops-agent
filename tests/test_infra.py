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


def test_no_ingress_from_the_whole_internet_and_no_ssh() -> None:
    ingress = block("ApiSecurityGroup").split("SecurityGroupIngress:")[1].split("SecurityGroupEgress")[0]
    assert "0.0.0.0/0" not in ingress
    assert "::/0" not in ingress
    assert not re.search(r"(FromPort|ToPort):\s*22\b", ingress)
    assert re.findall(r"FromPort:\s*(\d+)", ingress) == ["8000"]


def test_allowed_cidr_cannot_be_wider_than_a_slash_24() -> None:
    assert r"/(2[4-9]|3[0-2])$" in block("AllowedCidr")


def test_imdsv2_required_and_containers_cannot_reach_it() -> None:
    lt = block("LaunchTemplate")
    assert re.search(r"HttpTokens:\s*required", lt)
    assert re.search(r"HttpPutResponseHopLimit:\s*1\b", lt)
    assert re.search(r"Encrypted:\s*true", lt)


def test_deploy_role_trusts_only_main_of_this_repo() -> None:
    role = block("DeployRole")
    assert "token.actions.githubusercontent.com:aud: sts.amazonaws.com" in role
    assert "token.actions.githubusercontent.com:sub: !Sub repo:${GitHubRepo}:ref:refs/heads/main" in role
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
