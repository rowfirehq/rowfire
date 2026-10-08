"""`rowfire cloud`: what a managed platform's few settings turn into."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlsplit

import pytest

from rowfire import cloud

RENDER = {
    "DATABASE_URL": "postgres://owner:s3cret@dpg-abc.frankfurt-postgres.render.com/rowfire",
    "ROWFIRE_MODE": "demo",
    "ROWFIRE_MASTER_KEY": "whatever the platform generated",
    "RENDER_EXTERNAL_HOSTNAME": "rowfire-demo.onrender.com",
}


def test_a_demo_deploy_needs_nothing_but_the_platforms_settings() -> None:
    values = cloud.settings(RENDER)

    platform = urlsplit(values["ROWFIRE_PLATFORM_DSN"])
    assert platform.scheme == "postgresql+psycopg"
    assert parse_qs(platform.query)["options"] == ["-csearch_path=rowfire_platform"]
    assert values["ROWFIRE_HOSTED"] == "1" and values["ROWFIRE_EGRESS"] == "inbox-only"
    assert values["ROWFIRE_ALLOWED_HOSTS"] == "rowfire-demo.onrender.com"
    assert values["ROWFIRE_DEMO_TEMPLATE_SCHEMA"] == "sample"
    # Visitors' queries run as the read-only role, never as the owner.
    reader = urlsplit(values["ROWFIRE_DEMO_DSN"])
    assert reader.username == cloud.READER_ROLE and reader.password
    assert urlsplit(values["ROWFIRE_DEMO_ACTIVITY_DSN"]).username == "owner"


def test_any_platform_secret_becomes_the_same_valid_master_key() -> None:
    first = cloud.settings(RENDER)["ROWFIRE_MASTER_KEY"]
    assert len(base64.urlsafe_b64decode(first)) == 32
    # Every service derives the same key from the same secret.
    assert cloud.settings(RENDER)["ROWFIRE_MASTER_KEY"] == first

    proper = base64.urlsafe_b64encode(bytes(range(32))).decode()
    assert cloud.settings({**RENDER, "ROWFIRE_MASTER_KEY": proper})["ROWFIRE_MASTER_KEY"] == proper


def test_a_setting_already_given_wins_over_the_derived_one() -> None:
    values = cloud.settings({**RENDER, "ROWFIRE_WORKSPACE_IDLE_HOURS": "6", "ROWFIRE_EGRESS": ""})
    # Empty is not a choice: the safe default still applies.
    assert values["ROWFIRE_EGRESS"] == "inbox-only"
    values = cloud.settings({**RENDER, "ROWFIRE_ALLOWED_HOSTS": "demo.example.com"})
    assert values["ROWFIRE_ALLOWED_HOSTS"] == "demo.example.com"


def test_a_custom_domain_is_allowed_alongside_the_platforms() -> None:
    values = cloud.settings({**RENDER, "ROWFIRE_PUBLIC_HOSTNAME": "demo.rowfire.dev"})
    assert values["ROWFIRE_ALLOWED_HOSTS"] == "rowfire-demo.onrender.com,demo.rowfire.dev"


def test_private_mode_is_refused_until_there_is_sign_in() -> None:
    # On a platform it has a public address; with no sign-in, anyone who
    # found it could connect databases and send messages.
    with pytest.raises(cloud.CloudError, match="no sign-in"):
        cloud.settings({**RENDER, "ROWFIRE_MODE": "private"})


@pytest.mark.parametrize("missing", ["DATABASE_URL", "ROWFIRE_MODE", "ROWFIRE_MASTER_KEY"])
def test_a_missing_setting_is_named(missing: str) -> None:
    with pytest.raises(cloud.CloudError, match=missing):
        cloud.settings({k: v for k, v in RENDER.items() if k != missing})


def test_a_doubled_entrypoint_still_runs_the_command() -> None:
    from click.testing import CliRunner

    from rowfire.cli import cli

    result = CliRunner().invoke(cli, ["rowfire", "cloud", "--help"])
    assert result.exit_code == 0, result.output
    assert "predeploy" in result.output
    # And it does not clutter the real help.
    assert "rowfire " not in CliRunner().invoke(cli, ["--help"]).output.split("Commands:")[1]
