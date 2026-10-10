"""OAuth grants: storing a customer's tokens, and keeping them fresh.

The access token behind a Supabase source expires (an hour, typically), and
the refresh token that replaces it may itself be single-use. So a refresh
must happen exactly once even when the worker and the UI both reach for the
token at the same moment: the grant row is locked for the length of the
check-and-refresh, and whoever waits on the lock finds a fresh token already
written and simply uses it.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

from sqlmodel import Session, select

from .. import supabase
from . import crypto
from . import db as platform_db
from .models import DEFAULT_WORKSPACE_ID, OAuthGrant


class GrantError(Exception):
    """A grant that is missing or cannot be used. Never carries a token."""


def save_grant(
    session: Session,
    tokens: supabase.Tokens,
    *,
    provider: str = "supabase",
    master_key: bytes | None = None,
    workspace_id: uuid.UUID = DEFAULT_WORKSPACE_ID,
) -> OAuthGrant:
    grant = OAuthGrant(workspace_id=workspace_id, provider=provider, algorithm="")
    _seal(grant, tokens, master_key)
    session.add(grant)
    session.flush()
    return grant


def get_grant(
    session: Session, grant_id: str | uuid.UUID, workspace_id: uuid.UUID | None = None
) -> OAuthGrant | None:
    try:
        key = grant_id if isinstance(grant_id, uuid.UUID) else uuid.UUID(str(grant_id))
    except ValueError:
        return None
    grant = session.get(OAuthGrant, key)
    if grant is None or (workspace_id is not None and grant.workspace_id != workspace_id):
        return None
    return grant


def reveal(grant: OAuthGrant, master_key: bytes | None = None) -> supabase.Tokens:
    envelope = crypto.Envelope(
        ciphertext=grant.tokens_ciphertext,
        nonce=grant.tokens_nonce,
        wrapped_data_key=grant.wrapped_data_key,
        wrap_nonce=grant.wrap_nonce,
        key_id=grant.key_id,
        algorithm=grant.algorithm,
    )
    return supabase.Tokens.loads(crypto.decrypt(envelope, master_key=master_key))


def _seal(grant: OAuthGrant, tokens: supabase.Tokens, master_key: bytes | None) -> None:
    envelope = crypto.encrypt(tokens.dumps(), master_key=master_key)
    grant.tokens_ciphertext = envelope.ciphertext
    grant.tokens_nonce = envelope.nonce
    grant.wrapped_data_key = envelope.wrapped_data_key
    grant.wrap_nonce = envelope.wrap_nonce
    grant.key_id = envelope.key_id
    grant.algorithm = envelope.algorithm
    grant.expires_at = tokens.expires_at


def fresh_tokens(
    session: Session,
    grant_id: str | uuid.UUID,
    *,
    master_key: bytes | None = None,
    now: datetime | None = None,
) -> supabase.Tokens:
    """The grant's tokens, refreshed first if the access token is about to expire.

    Commits when it refreshes: the new refresh token must be durable before
    the old one is forgotten, or a crash in between strands the grant.
    """
    try:
        key = uuid.UUID(str(grant_id))
    except ValueError:
        raise GrantError("not a grant id") from None

    grant = session.exec(select(OAuthGrant).where(OAuthGrant.id == key).with_for_update()).first()
    if grant is None:
        raise GrantError("this Supabase connection's grant no longer exists; reconnect it")

    tokens = reveal(grant, master_key)
    if not tokens.expired(now):
        session.commit()  # release the lock
        return tokens

    client = supabase.oauth_client()
    if client is None:
        session.rollback()
        raise GrantError(
            f"the Supabase token expired and cannot be refreshed: set "
            f"{supabase.CLIENT_ID_ENV} and {supabase.CLIENT_SECRET_ENV}"
        )
    if not tokens.refresh_token:
        session.rollback()
        raise GrantError("the Supabase token expired and has no refresh token; reconnect it")

    try:
        tokens = supabase.refresh(client, tokens.refresh_token)
    except supabase.SupabaseError as exc:
        session.rollback()
        raise GrantError(f"could not refresh the Supabase token: {exc}") from None

    _seal(grant, tokens, master_key)
    grant.refreshed_at = datetime.now(UTC)
    session.add(grant)
    session.commit()
    return tokens


def access_token(grant_id: str) -> str:
    """A usable access token for a grant, in a session of its own.

    What `supabase.connect` calls. A session of its own so that locking and
    committing the grant never touches whatever transaction the caller (the
    scheduler mid-poll, say) has open.
    """
    try:
        with platform_db.session_scope() as session:
            return fresh_tokens(session, grant_id).access_token
    except (GrantError, crypto.CryptoError) as exc:
        raise supabase.SupabaseError(str(exc)) from None


# ------------------------------------------------------------ flow cookie


def seal_flow(flow: dict[str, str], master_key: bytes | None = None) -> str:
    """An in-progress OAuth flow (state, PKCE verifier) as an opaque cookie value.

    Encrypted, not merely signed: the PKCE verifier is what makes a stolen
    authorization code useless, so it must not be readable in the browser.
    """
    envelope = crypto.encrypt(json.dumps(flow), master_key=master_key)
    packed = {
        "c": envelope.ciphertext,
        "n": envelope.nonce,
        "k": envelope.wrapped_data_key,
        "w": envelope.wrap_nonce,
    }
    raw = json.dumps(
        {
            **{key: base64.urlsafe_b64encode(value).decode() for key, value in packed.items()},
            "i": envelope.key_id,
        }
    )
    return base64.urlsafe_b64encode(raw.encode()).decode()


def open_flow(value: str, master_key: bytes | None = None) -> dict[str, str]:
    if not value:
        raise GrantError("no OAuth flow in progress")
    try:
        raw = json.loads(base64.urlsafe_b64decode(value.encode()))
        envelope = crypto.Envelope(
            ciphertext=base64.urlsafe_b64decode(raw["c"]),
            nonce=base64.urlsafe_b64decode(raw["n"]),
            wrapped_data_key=base64.urlsafe_b64decode(raw["k"]),
            wrap_nonce=base64.urlsafe_b64decode(raw["w"]),
            key_id=str(raw["i"]),
        )
        flow = json.loads(crypto.decrypt(envelope, master_key=master_key))
    except (ValueError, KeyError, TypeError, crypto.CryptoError):
        raise GrantError("the OAuth flow cookie is not readable") from None
    if not isinstance(flow, dict):
        raise GrantError("the OAuth flow cookie is not readable")
    return flow
