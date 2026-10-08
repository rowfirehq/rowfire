"""Integration specs, templating, and request preparation.

Most of this needs no database. The templating tests matter most: a template
here produces an outbound HTTP request from customer data, so it is the same
class of surface as the `when` clause, at the end that actually sends.
"""

from __future__ import annotations

import base64
import json
import os

import pytest
import sqlalchemy
from sqlalchemy import text
from sqlmodel import Session, select

from rowfire.platform import crypto, dispatch, integrations, templating
from rowfire.platform.models import Action, AuthKind, Integration

# ------------------------------------------------------------- templating


def test_substitutes_a_value() -> None:
    assert templating.render_string("order {{ id }}", {"id": 42}) == "order 42"


def test_whole_placeholder_keeps_the_value_type() -> None:
    # So a numeric field stays numeric in the JSON body rather than becoming
    # a string, and a dict can be passed through whole.
    assert templating.render_string("{{ n }}", {"n": 42}) == 42
    assert templating.render_string("{{ row }}", {"row": {"a": 1}}) == {"a": 1}


def test_dotted_paths_walk_nested_dicts() -> None:
    assert templating.render_string("{{ a.b.c }}", {"a": {"b": {"c": "x"}}}) == "x"


def test_missing_value_fails_loudly() -> None:
    # Silently rendering an empty string would send a message with a hole in
    # it, which is worse than not sending.
    with pytest.raises(templating.TemplateError, match="nope"):
        templating.render_string("{{ nope }}", {"id": 1})


@pytest.mark.parametrize(
    "template",
    [
        "{{ 1 + 1 }}",
        "{{ ''.__class__ }}",
        "{{ config.items() }}",
        "{{ id | upper }}",
        "${id}",
        "{{ id }",
    ],
)
def test_expressions_are_not_a_language(template: str) -> None:
    """Nothing but `{{ name }}` is a placeholder.

    An expression language pointed at customer data and outbound HTTP is an
    injection surface, and template sandboxes are routinely escaped. Anything
    that is not a plain name is left as literal text.
    """
    rendered = templating.render_string(template, {"id": "x", "y": [1], "config": {}})
    assert rendered == template


def test_control_flow_syntax_is_inert() -> None:
    # The Jinja-looking block is left verbatim; only the plain placeholder
    # inside it is substituted. Nothing is iterated and nothing is executed.
    rendered = templating.render_string(
        "{% for x in y %}{{ x }}{% endfor %}", {"x": "VALUE", "y": [1, 2, 3]}
    )
    assert rendered == "{% for x in y %}VALUE{% endfor %}"


def test_attribute_traversal_into_python_objects_is_refused() -> None:
    # `__class__` is a syntactically valid name, so the check that matters is
    # that resolution walks dicts only and never getattr.
    with pytest.raises(templating.TemplateError):
        templating.render_string("{{ s.__class__ }}", {"s": "a string"})


def test_render_walks_nested_structures() -> None:
    body = {"a": ["{{ id }}", {"b": "n={{ id }}"}]}
    assert templating.render(body, {"id": 7}) == {"a": [7, {"b": "n=7"}]}


def test_a_dynamic_field_name_renders() -> None:
    # Regression. Dict keys used to pass through unrendered, so a body like
    # Braze's custom-attribute update sent the literal text `{{ field }}` as
    # the field name -- a call that succeeds and does nothing.
    rendered = templating.render(
        {"attributes": [{"external_id": "{{ id }}", "{{ field }}": "{{ value }}"}]},
        {"id": 7, "field": "tier", "value": "gold"},
    )
    assert rendered == {"attributes": [{"external_id": 7, "tier": "gold"}]}


def test_a_rendered_key_is_always_a_string() -> None:
    # render_string keeps the type when a template is the whole string, which
    # is right for values and impossible for a JSON object key.
    rendered = templating.render({"{{ n }}": "x"}, {"n": 42})
    assert rendered == {"42": "x"}


def test_placeholders_counts_dict_keys() -> None:
    # Otherwise a binding that does not supply them saves happily and fails at
    # send time, which is exactly what the up-front check exists to prevent.
    assert templating.placeholders({"{{ field }}": "{{ value }}"}) == {"field", "value"}


def test_placeholders_lists_what_a_template_needs() -> None:
    found = templating.placeholders({"text": "{{ a }} and {{ b.c }}", "x": ["{{ d }}"]})
    assert found == {"a", "b.c", "d"}


# ------------------------------------------------------------------ specs


def test_every_built_in_spec_parses() -> None:
    assert integrations.available()
    for name in integrations.available():
        spec = integrations.load_template(name)
        assert spec.actions, f"{name} defines no actions"


def test_slack_is_described_in_the_same_format_a_customer_would_use() -> None:
    # The test of the abstraction: if Slack needed special-casing in Python,
    # "engineers can add their own integrations" would not hold.
    spec = integrations.load_template("slack")
    assert spec.auth.kind is AuthKind.bearer
    assert "send_message" in spec.actions
    assert spec.actions["send_message"].path == "/chat.postMessage"


def test_braze_proves_the_format_is_not_slack_shaped() -> None:
    spec = integrations.load_template("braze")
    body = spec.actions["track_event"].body
    # A nested array body, which Slack never exercises.
    assert isinstance(body["events"], list)


def test_unknown_spec_names_the_available_ones() -> None:
    with pytest.raises(integrations.IntegrationError, match="slack"):
        integrations.load_template("nope")


def test_spec_rejects_an_unknown_method() -> None:
    with pytest.raises(integrations.IntegrationError, match="method"):
        integrations.parse_template("name: x\nactions:\n  a:\n    method: TELEPORT\n    path: /x\n")


def test_spec_rejects_unknown_fields() -> None:
    with pytest.raises(integrations.IntegrationError):
        integrations.parse_template("name: x\nnonsense: true\nactions:\n  a:\n    path: /x\n")


def test_header_auth_must_name_its_header() -> None:
    with pytest.raises(integrations.IntegrationError, match="header_name"):
        integrations.parse_template(
            "name: x\nauth:\n  kind: header\nactions:\n  a:\n    path: /x\n"
        )


def test_spec_inputs_reports_what_a_binding_must_supply() -> None:
    spec = integrations.load_template("slack")
    assert spec.inputs("send_message") == {"channel", "text"}


# ------------------------------------------------------------- preparation


def _integration(**kwargs) -> Integration:
    defaults = dict(
        name="slack",
        base_url="https://slack.com/api",
        auth_kind=AuthKind.bearer,
        timeout_ms=5000,
    )
    defaults.update(kwargs)
    return Integration(**defaults)


def _action(**kwargs) -> Action:
    import uuid

    defaults = dict(
        integration_id=uuid.uuid4(),
        name="send_message",
        method="POST",
        path_template="/chat.postMessage",
        body_template={"channel": "{{ channel }}", "text": "{{ text }}"},
        headers_template={},
        retry_on=[429],
    )
    defaults.update(kwargs)
    return Action(**defaults)


def test_prepare_builds_the_request_without_any_secret() -> None:
    prepared = dispatch.prepare(
        _integration(),
        _action(),
        {"channel": "#ops", "text": "order {{ id }}"},
        {"id": 5},
    )
    assert prepared.url == "https://slack.com/api/chat.postMessage"
    assert prepared.body == {"channel": "#ops", "text": "order 5"}
    # This is what gets persisted, so it must carry no credential.
    assert "authorization" not in json.dumps(prepared.as_record()).lower()


def test_send_applies_bearer_auth_at_call_time() -> None:
    seen: dict = {}

    def transport(method, url, headers, body, timeout_ms):
        seen.update(headers=headers, url=url)
        return 200, {"ok": True, "ts": "1.2"}

    integration, action = _integration(), _action()
    prepared = dispatch.prepare(integration, action, {"channel": "#c", "text": "t"}, {})
    outcome = dispatch.send(prepared, integration, action, "tok", transport=transport)

    assert seen["headers"]["authorization"] == "Bearer tok"
    assert seen["url"] == "https://slack.com/api/chat.postMessage"
    assert outcome.status == 200 and outcome.error is None


def test_send_refuses_a_non_http_url() -> None:
    integration = _integration(base_url="file:///etc/passwd")
    action = _action(path_template="")
    prepared = dispatch.prepare(integration, action, {"channel": "c", "text": "t"}, {})
    with pytest.raises(dispatch.DispatchError, match="http"):
        dispatch.send(prepared, integration, action, "tok", transport=lambda *a: (200, {}))


def test_the_demo_inbox_is_delivered_without_touching_the_network() -> None:
    # The inbox is what someone tries before connecting anything real. A
    # request to it must succeed, and must never be handed to a transport --
    # not even the one a caller passes in.
    def transport(*args, **kwargs):
        raise AssertionError("the inbox must not open a connection")

    template = integrations.load_template("demo_inbox")
    integration = _integration(base_url=template.base_url, auth_kind=AuthKind.none)
    action = _action(path_template="/messages")
    prepared = dispatch.prepare(integration, action, {"channel": "#ops", "text": "hi"}, {})

    outcome = dispatch.send(prepared, integration, action, None, transport=transport)

    assert prepared.url == "inbox://demo/messages"
    assert outcome.status == 200 and outcome.error is None


def test_inbox_only_egress_refuses_every_other_url(monkeypatch) -> None:
    # The hosted demo's setting: a public instance must not make requests to
    # wherever a visitor points an integration.
    monkeypatch.setenv(dispatch.EGRESS_ENV, "inbox-only")
    integration, action = _integration(), _action()
    prepared = dispatch.prepare(integration, action, {"channel": "#c", "text": "t"}, {})
    with pytest.raises(dispatch.DispatchError, match="switched off"):
        dispatch.send(prepared, integration, action, "tok", transport=lambda *a: (200, {}))

    inbox = _integration(base_url="inbox://demo", auth_kind=AuthKind.none)
    to_inbox = dispatch.prepare(
        inbox, _action(path_template="/messages"), {"channel": "c", "text": "t"}, {}
    )
    assert dispatch.send(to_inbox, inbox, action, None).status == 200


def test_an_optional_parameter_left_out_takes_its_default_or_null() -> None:
    # bind() accepts a binding without the optional ones, so rendering must
    # too. This used to fail at send time: "template refers to
    # `requester_name`, which is not available".
    template = integrations.load_template("zendesk")
    spec = template.actions["create_ticket"]
    action = _action(
        name="create_ticket",
        path_template=spec.path,
        body_template=spec.body,
        parameters={k: v.model_dump(mode="json") for k, v in spec.parameters.items()},
    )
    prepared = dispatch.prepare(
        _integration(base_url=template.base_url),
        action,
        {"subject": "Order {{ id }}", "body": "b", "requester_email": "a@b.example"},
        {"id": 3},
    )
    ticket = prepared.body["ticket"]
    assert ticket["subject"] == "Order 3"
    assert ticket["priority"] == "normal" and ticket["tags"] == []
    assert ticket["requester"]["name"] is None


def test_only_the_inbox_scheme_skips_the_http_check() -> None:
    integration = _integration(base_url="inboxes://demo")
    action = _action(path_template="")
    prepared = dispatch.prepare(integration, action, {"channel": "c", "text": "t"}, {})
    with pytest.raises(dispatch.DispatchError, match="http"):
        dispatch.send(prepared, integration, action, "tok", transport=lambda *a: (200, {}))


def test_a_secret_never_reaches_an_error_message() -> None:
    def transport(*args, **kwargs):
        raise RuntimeError("connection failed for token sk-live-abcdef")

    integration, action = _integration(), _action()
    prepared = dispatch.prepare(integration, action, {"channel": "#c", "text": "t"}, {})
    outcome = dispatch.send(prepared, integration, action, "sk-live-abcdef", transport=transport)
    assert "sk-live-abcdef" not in (outcome.error or "")
    assert "<redacted>" in (outcome.error or "")


def test_missing_credential_for_authenticated_integration_is_explicit() -> None:
    # And it names *which* credential, because an integration can hold several
    # and "needs a secret" would leave you guessing which one is absent.
    integration, action = _integration(), _action()
    prepared = dispatch.prepare(integration, action, {"channel": "#c", "text": "t"}, {})
    with pytest.raises(dispatch.DispatchError, match="authenticates with"):
        dispatch.send(prepared, integration, action, None, transport=lambda *a: (200, {}))


# --------------------------------------------------------- database setup
#
# Only the section below needs one. Everything above is pure and stays
# runnable anywhere, which is why the skip is on these tests rather than on
# the module.

PLATFORM_DSN = os.environ.get(
    "ROWFIRE_PLATFORM_DSN",
    "postgresql+psycopg://rowfire:rowfire@localhost:5434/rowfire_platform",
)


def _platform_available() -> bool:
    try:
        probe = sqlalchemy.create_engine(PLATFORM_DSN)
        with probe.connect() as conn:
            conn.execute(text("SELECT 1 FROM integration LIMIT 1"))
        probe.dispose()
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _platform_available(), reason="control-plane database unavailable"
)


def _key() -> bytes:
    return base64.urlsafe_b64decode(crypto.generate_master_key())


@pytest.fixture
def engine():
    engine = sqlalchemy.create_engine(PLATFORM_DSN)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE integration RESTART IDENTITY CASCADE"))
    yield engine
    engine.dispose()


# ------------------------------------------- instances, credentials, actions
#
# The three things the integration/action split exists to make possible, and
# none of which the old single-connector-per-provider shape could express.


@needs_db
def test_two_integrations_can_share_a_provider(engine) -> None:
    """ "Acme Slack" and "Support Slack" are two integrations.

    An integration is an instance, not a type. Keying on the provider would
    mean a workspace could talk to exactly one Slack, which is wrong the first
    time anyone has a staging workspace.
    """
    template = integrations.load_template("slack")
    master = _key()

    with Session(engine) as session:
        first = integrations.install(
            session,
            template,
            name="acme-slack",
            credentials={"bot_token": "xoxb-production"},
            master_key=master,
        )
        second = integrations.install(
            session,
            template,
            name="support-slack",
            credentials={"bot_token": "xoxb-support"},
            master_key=master,
        )
        session.commit()

        assert first.id != second.id
        assert first.provider == second.provider == "slack"
        # Each carries its own credential, and each gets its own copy of the
        # provider's actions.
        assert integrations.auth_secret(first, master) == "xoxb-production"
        assert integrations.auth_secret(second, master) == "xoxb-support"
        for integration in (first, second):
            actions = session.exec(
                select(Action).where(Action.integration_id == integration.id)
            ).all()
            assert {a.name for a in actions} == {"send_message"}


@needs_db
def test_credentials_are_a_map_not_a_single_secret(engine) -> None:
    # Braze declares two: a key that signs, and an app id that is merely
    # configuration. Both are stored; only one authenticates.
    master = _key()
    with Session(engine) as session:
        integration = integrations.install(
            session,
            integrations.load_template("braze"),
            name="acme-braze",
            credentials={"api_key": "braze-secret", "app_id": "app-123"},
            master_key=master,
        )
        session.commit()

        revealed = integrations.reveal_credentials(integration, master)
        assert revealed == {"api_key": "braze-secret", "app_id": "app-123"}
        assert integrations.auth_secret(integration, master) == "braze-secret"


@needs_db
def test_credentials_are_encrypted_at_rest(engine) -> None:
    master = _key()
    with Session(engine) as session:
        integration = integrations.install(
            session,
            integrations.load_template("slack"),
            name="sealed",
            credentials={"bot_token": "xoxb-very-secret"},
            master_key=master,
        )
        session.commit()
        blob = integration.credentials_ciphertext or b""

    assert b"xoxb-very-secret" not in blob
    assert b"bot_token" not in blob, "even the key names should not be readable"


@needs_db
def test_a_legacy_bare_secret_still_decrypts(engine) -> None:
    """The column used to hold one secret; it now holds a JSON map.

    Rows written before that change decrypt to a bare string. Reading them as
    the credential the auth config names means the upgrade needs no
    re-encryption, and therefore no master key at migration time.
    """
    from rowfire.platform import crypto

    master = _key()
    with Session(engine) as session:
        integration = integrations.install(
            session,
            integrations.load_template("slack"),
            name="legacy",
            credentials={"bot_token": "placeholder"},
            master_key=master,
        )
        # Reseal the way the old code did: the secret alone, not a map.
        envelope = crypto.encrypt("xoxb-from-before", master_key=master)
        integration.credentials_ciphertext = envelope.ciphertext
        integration.credentials_nonce = envelope.nonce
        integration.wrapped_data_key = envelope.wrapped_data_key
        integration.wrap_nonce = envelope.wrap_nonce
        integration.key_id = envelope.key_id
        integration.algorithm = envelope.algorithm
        session.add(integration)
        session.commit()

        assert integrations.auth_secret(integration, master) == "xoxb-from-before"


@needs_db
def test_an_action_can_be_defined_by_hand(engine) -> None:
    """The open-ended path: a REST call described as data, with no code.

    This is the whole claim of the design -- so it is asserted end to end,
    from definition through to the request that would be sent.
    """
    master = _key()
    with Session(engine) as session:
        integration = integrations.install(
            session,
            integrations.IntegrationTemplate(
                name="custom",
                base_url="https://api.example.com/v2",
                auth=integrations.AuthSpec(
                    kind=AuthKind.header, header_name="X-API-Key", credential="api_key"
                ),
            ),
            name="our-crm",
            credentials={"api_key": "k-123"},
            master_key=master,
        )
        action = integrations.upsert_action(
            session,
            integration,
            "update_field",
            integrations.ActionSpec(
                description="Set a field on a contact",
                method="PATCH",
                path="/contacts/{{ contact_id }}",
                headers={"X-Request-Source": "rowfire"},
                body={"fields": {"{{ field }}": "{{ value }}"}},
            ),
        )
        session.commit()

        assert integrations.action_inputs(action) == {"contact_id", "field", "value"}

        prepared = dispatch.prepare(
            integration,
            action,
            {"contact_id": "{{ id }}", "field": "tier", "value": "gold"},
            {"id": 42},
        )

    assert prepared.method == "PATCH"
    assert prepared.url == "https://api.example.com/v2/contacts/42"
    assert prepared.body == {"fields": {"tier": "gold"}}
    assert prepared.headers["X-Request-Source"] == "rowfire"
    # Auth is applied at send time, so nothing here carries the key.
    assert "k-123" not in json.dumps(prepared.as_record())


@needs_db
def test_a_hand_made_action_authenticates_like_any_other(engine) -> None:
    master = _key()
    calls: list[dict] = []

    with Session(engine) as session:
        integration = integrations.install(
            session,
            integrations.IntegrationTemplate(
                name="custom",
                base_url="https://api.example.com",
                auth=integrations.AuthSpec(
                    kind=AuthKind.header, header_name="X-API-Key", credential="api_key"
                ),
            ),
            name="our-crm",
            credentials={"api_key": "k-123"},
            master_key=master,
        )
        action = integrations.upsert_action(
            session,
            integration,
            "ping",
            integrations.ActionSpec(method="GET", path="/ping"),
        )
        session.commit()

        prepared = dispatch.prepare(integration, action, {}, {})
        dispatch.send(
            prepared,
            integration,
            action,
            integrations.auth_secret(integration, master),
            transport=lambda m, u, h, b, t: calls.append({"headers": h}) or (200, {}),
        )

    assert calls[0]["headers"]["x-api-key"] == "k-123"


@needs_db
def test_editing_an_action_replaces_it_rather_than_duplicating(engine) -> None:
    master = _key()
    with Session(engine) as session:
        integration = integrations.install(
            session,
            integrations.load_template("slack"),
            name="slack",
            credentials={"bot_token": "xoxb"},
            master_key=master,
        )
        integrations.upsert_action(
            session, integration, "send_message", integrations.ActionSpec(path="/changed")
        )
        session.commit()

        actions = session.exec(select(Action).where(Action.integration_id == integration.id)).all()
        assert len(actions) == 1
        assert actions[0].path_template == "/changed"


def test_auth_naming_an_undeclared_credential_is_refused() -> None:
    # Otherwise the integration saves happily and fails at send time with
    # "no credential stored", which points at the wrong thing.
    with pytest.raises(integrations.IntegrationError, match="not declared"):
        integrations.parse_template(
            """
            name: broken
            base_url: https://example.com
            credentials:
              api_key: { label: Key }
            auth:
              kind: bearer
              credential: token
            actions:
              ping: { method: GET, path: /ping }
            """,
            label="broken",
        )


# ------------------------------------------------- the declared contract
#
# An action says what it needs: name, type, label, required. Inference gave
# you names and nothing else, which is enough to render unlabelled text boxes
# and not enough to ask a good question or send a correct request.


def test_basic_auth_sends_a_base64_credential_pair() -> None:
    """An API token usually means HTTP Basic, not a bearer token.

    Zendesk, Twilio and Stripe all work this way. Without it, "describe any
    REST API" would exclude one of the two schemes most of them use.
    """
    integration = _integration(
        auth_kind=AuthKind.basic,
        auth_credential="api_token",
        auth_username_credential="email",
    )
    action = _action()
    prepared = dispatch.prepare(integration, action, {"channel": "#c", "text": "t"}, {})

    calls: list[dict] = []
    dispatch.send(
        prepared,
        integration,
        action,
        "agent@example.com/token:tok-123",
        transport=lambda m, u, h, b, t: calls.append({"headers": h}) or (200, {}),
    )

    import base64

    sent = calls[0]["headers"]["authorization"]
    assert sent.startswith("Basic ")
    assert base64.b64decode(sent[6:]).decode() == "agent@example.com/token:tok-123"


def test_basic_auth_assembles_the_pair_from_two_credentials(engine) -> None:
    master = _key()
    with Session(engine) as session:
        integration = integrations.install(
            session,
            integrations.load_template("zendesk"),
            name="acme-zendesk",
            base_url="https://acme.zendesk.com/api/v2",
            credentials={"email": "agent@acme.com/token", "api_token": "tok-123"},
            master_key=master,
        )
        session.commit()
        # One opaque string by the time it leaves, so the dispatcher needs no
        # second code path.
        assert integrations.auth_secret(integration, master) == "agent@acme.com/token:tok-123"


def test_basic_auth_must_name_its_username_credential() -> None:
    with pytest.raises(integrations.IntegrationError, match="username_credential"):
        integrations.parse_template(
            """
            name: half
            base_url: https://example.com
            credentials:
              api_token: { label: Token }
            auth: { kind: basic, credential: api_token }
            actions:
              ping: { method: GET, path: /ping }
            """,
            label="half",
        )


def test_zendesk_proves_the_format_handles_a_per_account_host() -> None:
    # Slack has one host for everybody; Zendesk has one per customer. The
    # difference is a field, not a code path.
    template = integrations.load_template("zendesk")
    assert "your-subdomain" in template.base_url
    assert template.auth.kind is AuthKind.basic
    assert set(template.actions) == {"create_ticket", "add_tags"}


def test_a_catalogue_action_declares_its_parameters() -> None:
    spec = integrations.load_template("slack").actions["send_message"]
    assert set(spec.parameters) == {"channel", "text"}
    assert spec.parameters["channel"].label == "Channel"
    assert spec.parameters["channel"].type is integrations.ParameterType.string
    assert spec.parameters["channel"].required


def test_a_placeholder_nothing_declares_is_an_error() -> None:
    # This used to invent a parameter called `txt` and ask the user to fill
    # it in. A typo is the overwhelmingly likely cause, so it is reported.
    with pytest.raises(integrations.IntegrationError, match="not a declared parameter"):
        integrations.parse_template(
            """
            name: typo
            base_url: https://example.com
            actions:
              send:
                parameters:
                  text: { type: string }
                body: { message: "{{ txt }}" }
            """,
            label="typo",
        )


def test_a_declared_parameter_nothing_uses_is_an_error() -> None:
    # The other direction: a field the form would ask for and then discard.
    with pytest.raises(integrations.IntegrationError, match="never used"):
        integrations.parse_template(
            """
            name: unused
            base_url: https://example.com
            actions:
              send:
                parameters:
                  text: { type: string }
                  ignored: { type: string }
                body: { message: "{{ text }}" }
            """,
            label="unused",
        )


def test_a_row_context_name_may_be_used_without_being_declared() -> None:
    # The dispatcher supplies these either way, so requiring a declaration
    # would be noise.
    template = integrations.parse_template(
        """
        name: ctx
        base_url: https://example.com
        actions:
          send:
            body: { at: "{{ fired_at }}", id: "{{ entity_id }}" }
        """,
        label="ctx",
    )
    assert template.actions["send"].inputs() == set()


def test_declaring_a_row_context_name_lets_a_binding_override_it() -> None:
    """Braze wants an event_time on the event it records.

    That collides with the name the dispatcher fills from the fired row. The
    right answer is that declaring it makes it bindable, while leaving it
    undeclared keeps the automatic value -- so both are possible.
    """
    spec = integrations.load_template("braze").actions["track_event"]
    assert "event_time" in spec.parameters
    assert not spec.parameters["event_time"].required


def test_parameters_fall_back_to_inference_when_undeclared(engine) -> None:
    # Actions stored before parameters existed keep working, which is why the
    # migration needed no backfill.
    with Session(engine) as session:
        integration = integrations.install(
            session,
            integrations.IntegrationTemplate(name="custom", base_url="https://example.com"),
            name="legacy-actions",
        )
        action = integrations.upsert_action(
            session,
            integration,
            "send",
            integrations.ActionSpec(body={"a": "{{ one }}", "b": "{{ two }}"}),
        )
        action.parameters = {}  # as a pre-declaration row would be
        session.add(action)
        session.commit()

        declared = integrations.action_parameters(action)
        assert set(declared) == {"one", "two"}
        assert all(p.type is integrations.ParameterType.string for p in declared.values())


def test_an_optional_parameter_need_not_be_bound(engine) -> None:
    with Session(engine) as session:
        integration = integrations.install(
            session,
            integrations.IntegrationTemplate(name="custom", base_url="https://example.com"),
            name="optional-params",
        )
        integrations.upsert_action(
            session,
            integration,
            "send",
            integrations.ActionSpec(
                parameters={
                    "needed": {"type": "string"},
                    "spare": {"type": "string", "required": False},
                },
                body={"a": "{{ needed }}", "b": "{{ spare }}"},
            ),
        )
        session.commit()

        # Only the required one is insisted on.
        binding = integrations.bind(session, "a_rule", "optional-params", "send", {"needed": "x"})
        assert binding.parameters == {"needed": "x"}

        with pytest.raises(integrations.IntegrationError, match="needed"):
            integrations.bind(session, "b_rule", "optional-params", "send", {"spare": "y"})


# --------------------------------------------------------------- coercion


def _typed_action(**parameters):
    return Action(
        integration_id=None,
        name="typed",
        method="POST",
        path_template="",
        parameters=parameters,
        body_template={name: f"{{{{ {name} }}}}" for name in parameters},
        headers_template={},
        retry_on=[],
    )


def test_a_number_parameter_is_sent_as_a_number() -> None:
    """The reason a declared type earns its keep.

    Row values reach the template context as strings -- a Decimal total is
    str()'d on the way in -- so an API expecting {"amount": 12.5} was being
    sent {"amount": "12.5"} and rejecting it. There is nothing to coerce by
    unless the action said what it wanted.
    """
    action = _typed_action(amount={"type": "number"}, count={"type": "number"})
    prepared = dispatch.prepare(
        _integration(),
        action,
        {"amount": "{{ total }}", "count": "{{ n }}"},
        {"total": "12.50", "n": "3"},
    )
    assert prepared.body == {"amount": 12.5, "count": 3}


def test_a_boolean_parameter_is_sent_as_a_boolean() -> None:
    action = _typed_action(flag={"type": "boolean"})
    prepared = dispatch.prepare(_integration(), action, {"flag": "{{ v }}"}, {"v": "true"})
    assert prepared.body == {"flag": True}


def test_a_json_parameter_is_passed_through_whole() -> None:
    action = _typed_action(properties={"type": "json"})
    prepared = dispatch.prepare(
        _integration(), action, {"properties": "{{ p }}"}, {"p": '{"tier": "gold"}'}
    )
    assert prepared.body == {"properties": {"tier": "gold"}}


def test_a_value_that_will_not_convert_is_left_alone() -> None:
    # The API's own error is more informative than a guess from here, and
    # refusing to send is the wrong call for a possibly-cosmetic mismatch.
    action = _typed_action(amount={"type": "number"})
    prepared = dispatch.prepare(
        _integration(), action, {"amount": "{{ v }}"}, {"v": "not a number"}
    )
    assert prepared.body == {"amount": "not a number"}


def test_an_undeclared_action_coerces_nothing() -> None:
    action = _typed_action()
    action.body_template = {"amount": "{{ amount }}"}
    prepared = dispatch.prepare(_integration(), action, {"amount": "12.50"}, {})
    assert prepared.body == {"amount": "12.50"}
