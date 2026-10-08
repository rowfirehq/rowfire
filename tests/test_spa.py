"""The built UI is served so that every page is a deep link.

/rules/<name> and /integrations/<name>/actions/<action> are routes in the
browser, not files, so the server answers them with the app. These pin down
the edges of that: real files are still files, an unknown API path is still a
404, and a path that climbs out of the build directory serves nothing from
outside it.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rowfire import api


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    dist = tmp_path / "ui_dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>")
    (dist / "favicon.svg").write_text("<svg/>")
    (dist / "assets" / "index.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("outside the build")

    app = FastAPI()
    app.include_router(api.router)
    api._mount_ui(app, dist)
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/rules",
        "/rules/billing_ticket",
        "/triggers/payment_failed",
        "/integrations/Acme%20Zendesk",
        "/integrations/Acme%20Zendesk/actions/create_ticket",
        "/activity",
    ],
)
def test_a_deep_link_is_answered_with_the_app(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert "<title>app</title>" in response.text


def test_real_files_are_still_served(client: TestClient) -> None:
    assert client.get("/favicon.svg").text == "<svg/>"
    assert client.get("/assets/index.js").text == "console.log(1)"


def test_an_unknown_api_path_is_a_404_not_the_app(client: TestClient) -> None:
    response = client.get("/api/no-such-endpoint")
    assert response.status_code == 404
    assert "<title>app</title>" not in response.text


@pytest.mark.parametrize("path", ["/%2e%2e/secret.txt", "/..%2Fsecret.txt"])
def test_a_path_outside_the_build_is_never_served(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert "outside the build" not in response.text


# ROWFIRE_HEAD_HTML: one instance's own markup, such as an analytics tag, kept
# in its environment and out of the repository.

TAG = '<script src="https://analytics.example/tag.js"></script>'


def _client_with_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, head: str | None
) -> TestClient:
    dist = tmp_path / "ui_dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><head><title>app</title></head><body></body></html>"
    )
    if head is None:
        monkeypatch.delenv("ROWFIRE_HEAD_HTML", raising=False)
    else:
        monkeypatch.setenv("ROWFIRE_HEAD_HTML", head)
    app = FastAPI()
    api._mount_ui(app, dist)
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.mark.parametrize("path", ["/", "/rules/billing_ticket", "/index.html"])
def test_head_html_is_added_to_every_page_of_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    response = _client_with_head(tmp_path, monkeypatch, TAG).get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    head, _, body = response.text.partition("</head>")
    assert TAG in head
    assert TAG not in body


def test_without_head_html_the_page_is_served_as_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _client_with_head(tmp_path, monkeypatch, None).get("/")
    assert (
        response.text == "<!doctype html><html><head><title>app</title></head><body></body></html>"
    )
