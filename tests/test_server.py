import pytest
from fastapi.testclient import TestClient

import server
from streams import Recording

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(monkeypatch):
    # No lifespan, so the detection worker and its browser never start
    server.links.clear()
    while not server.pending.empty():
        server.pending.get()
    monkeypatch.setattr(server, "find_recordings", list)
    return TestClient(server.app)


def test_page_is_served_without_token(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "m3u82vlc" in response.text


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "test-token"}],
)
def test_api_requires_token(client, headers):
    assert client.get("/api/state", headers=headers).status_code == 401
    assert (
        client.post(
            "/api/links", json={"url": "https://a.example/x"}, headers=headers
        )
    ).status_code == 401
    assert (
        client.delete("/api/recordings/1", headers=headers).status_code == 401
    )


def test_link_is_queued(client):
    response = client.post(
        "/api/links",
        json={"url": "https://site.example/nyancat/"},
        headers=AUTH,
    )
    assert response.status_code == 201
    link = response.json()
    assert link["name"] == "nyancat" and link["state"] == "waiting"
    assert server.pending.qsize() == 1
    assert client.get("/api/state", headers=AUTH).json() == {
        "links": [link],
        "recordings": [],
    }


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "nyancat", "ftp://a.example/x"]
)
def test_only_http_urls_are_accepted(client, url):
    response = client.post("/api/links", json={"url": url}, headers=AUTH)
    assert response.status_code == 422
    assert server.pending.empty()


def test_full_queue_drops_finished_links_first(client):
    for number in range(server.MAX_LINKS):
        client.post(
            "/api/links",
            json={"url": f"https://a.example/{number}"},
            headers=AUTH,
        )
    full = client.post(
        "/api/links", json={"url": "https://a.example/x"}, headers=AUTH
    )
    assert full.status_code == 429

    first = next(iter(server.links.values()))
    first.state = "not_found"
    added = client.post(
        "/api/links", json={"url": "https://a.example/x"}, headers=AUTH
    )
    assert added.status_code == 201
    assert first.id not in server.links


def test_record_requires_found_stream(client):
    link = client.post(
        "/api/links", json={"url": "https://a.example/x"}, headers=AUTH
    ).json()
    response = client.post(f"/api/links/{link['id']}/record", headers=AUTH)
    assert response.status_code == 409
    assert (
        client.post("/api/links/unknown/record", headers=AUTH).status_code
        == 404
    )


def test_record_starts_recording(client, monkeypatch):
    started = []
    monkeypatch.setattr(
        server, "start_recording", lambda *arguments: started.append(arguments)
    )
    monkeypatch.setattr(server, "wait_for_output", lambda output_file: True)
    link = client.post(
        "/api/links", json={"url": "https://a.example/nyancat"}, headers=AUTH
    ).json()
    stored = server.links[link["id"]]
    stored.state, stored.stream, stored.headers = (
        "found",
        "m.m3u8",
        {"Referer": "r"},
    )

    response = client.post(f"/api/links/{link['id']}/record", headers=AUTH)
    assert response.json()["state"] == "recording"
    assert started == [
        ("m.m3u8", "/nonexistent-recordings/nyancat.ts", {"Referer": "r"})
    ]

    # The recording is not in the process list, so it counts as finished
    state = client.get("/api/state", headers=AUTH).json()
    assert state["links"][0]["state"] == "finished"


def test_failed_recording_is_reported(client, monkeypatch):
    class Process:
        terminated = False

        def terminate(self):
            self.terminated = True

    process = Process()
    monkeypatch.setattr(server, "start_recording", lambda *arguments: process)
    monkeypatch.setattr(server, "wait_for_output", lambda output_file: False)
    link = client.post(
        "/api/links", json={"url": "https://a.example/x"}, headers=AUTH
    ).json()
    server.links[link["id"]].state = "found"

    response = client.post(f"/api/links/{link['id']}/record", headers=AUTH)
    assert response.json()["state"] == "error"
    assert process.terminated


def test_only_recordings_can_be_stopped(client, monkeypatch):
    stopped = []
    recording = Recording(pid=4242, output_file="/r/x.ts", elapsed=5)
    monkeypatch.setattr(server, "find_recordings", lambda: [recording])
    monkeypatch.setattr(server, "stop_recording", stopped.append)

    assert client.delete("/api/recordings/1", headers=AUTH).status_code == 404
    assert stopped == []
    assert (
        client.delete("/api/recordings/4242", headers=AUTH).status_code == 204
    )
    assert stopped == [recording]


def test_link_can_be_removed_unless_in_use(client):
    link = client.post(
        "/api/links", json={"url": "https://a.example/x"}, headers=AUTH
    ).json()
    server.links[link["id"]].state = "searching"
    assert (
        client.delete(f"/api/links/{link['id']}", headers=AUTH).status_code
        == 409
    )
    server.links[link["id"]].state = "found"
    assert (
        client.delete(f"/api/links/{link['id']}", headers=AUTH).status_code
        == 204
    )
    assert server.links == {}
