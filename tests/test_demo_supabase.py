"""The demo's optional Supabase source: offered only where it can be read."""

from __future__ import annotations

from pathlib import Path

import yaml

from rowfire import hosted, supabase
from rowfire.definitions import loads as loads_definitions

SAAS_DEFINITIONS = Path(__file__).resolve().parents[1] / "examples" / "saas" / "definitions.yaml"


def test_a_supabase_source_is_readable_only_with_a_server_token(monkeypatch) -> None:
    monkeypatch.delenv(supabase.ACCESS_TOKEN_ENV, raising=False)
    assert hosted.readable("supabase://abcdefghijklmnopqrst") is False
    assert hosted.readable("mysql://ro:ro@support:3306/rowfire_support") is True

    monkeypatch.setenv(supabase.ACCESS_TOKEN_ENV, "sbp_token")
    assert hosted.readable("supabase://abcdefghijklmnopqrst") is True


def test_the_sample_definitions_include_a_supabase_trigger_that_loads() -> None:
    text = SAAS_DEFINITIONS.read_text()
    loads_definitions(text)
    raw = yaml.safe_load(text)
    assert raw["triggers"]["free_workspace_near_quota"]["source"] == "supabase"
    assert raw["rules"]["nudge_upgrade"]["trigger"] == "free_workspace_near_quota"


def test_a_demo_without_supabase_drops_its_trigger_and_rule() -> None:
    text = SAAS_DEFINITIONS.read_text()

    without = yaml.safe_load(hosted._readable_only(text, {"primary", "support"}))
    assert "free_workspace_near_quota" not in without["triggers"]
    assert "nudge_upgrade" not in without["rules"]
    assert "urgent_ticket_unanswered" in without["triggers"]

    with_it = yaml.safe_load(hosted._readable_only(text, {"primary", "support", "supabase"}))
    assert "free_workspace_near_quota" in with_it["triggers"]
    assert "nudge_upgrade" in with_it["rules"]
