from __future__ import annotations

import pytest

import rowfire_sdk
from rowfire_sdk.testing import FakeIngest

# Fast enough that a test never waits on the clock for long.
FAST = {
    "flush_interval": 0.02,
    "flush_jitter": 0.0,
    "backoff_base": 0.02,
    "backoff_max": 0.1,
    "timeout": 2.0,
}


@pytest.fixture
def ingest():
    with FakeIngest() as server:
        yield server


@pytest.fixture
def start(ingest):
    """Start the global client against the fake ingest; closed after the test."""

    def _start(subscriptions=None, url=None, **options):
        return rowfire_sdk.init(
            url or ingest.url,
            "test-key",
            subscriptions=subscriptions,
            **{**FAST, **options},
        )

    yield _start
    rowfire_sdk.close(timeout=0.5)
