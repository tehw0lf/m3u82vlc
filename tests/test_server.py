import pytest
from fastapi.testclient import TestClient

import server
from streams import Recording, SplitStream

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(monkeypatch):
    # No lifespan, so the detection worker and its browser never start
    server.links.clear()
    server.reserved_files.clear()
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


def queue_link(client, url="https://a.example/nyancat"):
    link = client.post("/api/links", json={"url": url}, headers=AUTH).json()
    return server.links[link["id"]]


def test_found_stream_is_recorded(client, monkeypatch):
    started = []
    monkeypatch.setattr(
        server, "find_m3u8_url", lambda url: ("m.m3u8", {"Referer": "r"})
    )
    monkeypatch.setattr(
        server, "start_recording", lambda *arguments: started.append(arguments)
    )
    monkeypatch.setattr(
        server, "wait_for_output", lambda output_file, process: True
    )
    link = queue_link(client)

    server.detect_link(server.pending.get_nowait())

    assert link.state == "recording"
    assert link.output_file == "/nonexistent-recordings/nyancat.ts"
    assert started == [
        ("m.m3u8", "/nonexistent-recordings/nyancat.ts", {"Referer": "r"})
    ]

    # The recording is not in the process list, so it counts as finished
    state = client.get("/api/state", headers=AUTH).json()
    assert state["links"][0]["state"] == "finished"


@pytest.mark.parametrize(
    ("exit_code", "state", "detail"),
    [
        (None, "finished", ""),
        (0, "finished", "Stream ended"),
        (255, "finished", "Stopped"),
        (145, "error", "Recording failed, ffmpeg exit code 145"),
    ],
)
def test_ended_recording_reports_its_exit_code(
    client, monkeypatch, exit_code, state, detail
):
    taken = []
    monkeypatch.setattr(
        server,
        "take_exit_code",
        lambda output_file: taken.append(output_file) or exit_code,
    )
    link = client.post(
        "/api/links", json={"url": "https://a.example/nyancat"}, headers=AUTH
    ).json()
    stored = server.links[link["id"]]
    stored.state, stored.output_file = "recording", "/r/nyancat.ts"

    described = client.get("/api/state", headers=AUTH).json()["links"][0]
    assert (described["state"], described["detail"]) == (state, detail)
    assert taken == ["/r/nyancat.ts"]


class Process:
    """A recorder that never writes data"""

    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        assert self.terminated


def test_failed_recording_is_reported(client, monkeypatch):
    processes = []

    def start_recording(*arguments):
        processes.append(Process())
        return processes[-1]

    monkeypatch.setattr(server, "start_recording", start_recording)
    monkeypatch.setattr(
        server, "wait_for_output", lambda output_file, process: False
    )
    link = queue_link(client)

    server.record_link(link, "m.m3u8", {})

    assert link.state == "error"
    assert link.detail == "Recording did not start"
    assert len(processes) == server.START_ATTEMPTS
    assert all(process.terminated for process in processes)


def test_recording_is_started_again_after_failed_start(client, monkeypatch):
    processes = []

    def start_recording(*arguments):
        processes.append(Process())
        return processes[-1]

    monkeypatch.setattr(server, "start_recording", start_recording)
    # The first recorder ran into a dropped connection
    monkeypatch.setattr(
        server,
        "wait_for_output",
        lambda output_file, process: process is not processes[0],
    )
    link = queue_link(client)

    server.record_link(link, "m.m3u8", {})

    assert link.state == "recording"
    assert [process.terminated for process in processes] == [True, False]


def test_failed_link_can_be_retried(client, monkeypatch):
    link = queue_link(client)
    server.pending.get_nowait()
    retry = f"/api/links/{link.id}/retry"
    for state in ("waiting", "searching", "starting", "recording"):
        link.state = state
        monkeypatch.setattr(
            server,
            "find_recordings",
            lambda: [Recording(pid=1, output_file="/r/x.ts", elapsed=1)],
        )
        link.output_file = "/r/x.ts"
        assert client.post(retry, headers=AUTH).status_code == 409
    assert server.pending.empty()

    link.state, link.detail = "error", "Recording did not start"
    response = client.post(retry, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["state"] == "waiting"
    assert (link.detail, link.output_file) == ("", None)
    assert server.pending.get_nowait() is link

    assert (
        client.post("/api/links/unknown/retry", headers=AUTH).status_code
        == 404
    )


def test_adopted_recording_cannot_be_retried(client, monkeypatch):
    recording = Recording(pid=1, output_file="/r/older.ts", elapsed=90)
    monkeypatch.setattr(server, "find_recordings", lambda: [recording])
    adopted = client.get("/api/state", headers=AUTH).json()["links"][0]
    monkeypatch.setattr(server, "find_recordings", list)

    response = client.post(f"/api/links/{adopted['id']}/retry", headers=AUTH)
    assert response.status_code == 409
    assert server.pending.empty()


def test_failing_start_does_not_leave_link_starting(client, monkeypatch):
    def fail(*arguments):
        raise OSError("ffmpeg is missing")

    monkeypatch.setattr(server, "start_recording", fail)
    link = queue_link(client)
    link.state = "starting"

    server.record_link(link, "m.m3u8", {})

    assert (link.state, link.detail) == ("error", "ffmpeg is missing")
    assert server.reserved_files == set()
    assert (
        client.delete(f"/api/links/{link.id}", headers=AUTH).status_code == 204
    )


def test_recordings_of_the_same_name_get_different_files(client, monkeypatch):
    first, second = queue_link(client), queue_link(client)
    output_files = []

    def wait_for_output(output_file, process):
        # ffmpeg has not created the file of the first recording yet
        output_files.append(output_file)
        assert output_file in server.reserved_files
        return True

    monkeypatch.setattr(server, "start_recording", lambda *arguments: None)
    monkeypatch.setattr(server, "wait_for_output", wait_for_output)
    server.reserved_files.add("/nonexistent-recordings/nyancat.ts")
    server.record_link(first, "m.m3u8", {})
    server.reserved_files.discard("/nonexistent-recordings/nyancat.ts")
    server.record_link(second, "m.m3u8", {})

    assert output_files == [
        "/nonexistent-recordings/nyancat_1.ts",
        "/nonexistent-recordings/nyancat.ts",
    ]
    assert server.reserved_files == set()


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
    for state in ("searching", "starting"):
        server.links[link["id"]].state = state
        assert (
            client.delete(f"/api/links/{link['id']}", headers=AUTH).status_code
            == 409
        )
    server.links[link["id"]].state = "not_found"
    assert (
        client.delete(f"/api/links/{link['id']}", headers=AUTH).status_code
        == 204
    )
    assert server.links == {}


@pytest.mark.parametrize(
    ("result", "state", "detail"),
    [
        (
            (SplitStream("v.m3u8", "a.m3u8", "1920x1080"), {}),
            "recording",
            "1920x1080",
        ),
        (("master.m3u8", {"Referer": "r"}), "recording", ""),
        ((None, {}), "not_found", ""),
        (
            RuntimeError("browser crashed\ncall log"),
            "error",
            "browser crashed",
        ),
    ],
)
def test_worker_takes_link_to_its_final_state(
    client, monkeypatch, result, state, detail
):
    def find_m3u8_url(url):
        assert link.state == "searching"
        if isinstance(result, Exception):
            raise result
        return result

    def start_recording(*arguments):
        assert link.state == "starting"
        recorded.append(arguments[0])

    recorded = []
    monkeypatch.setattr(server, "find_m3u8_url", find_m3u8_url)
    monkeypatch.setattr(server, "start_recording", start_recording)
    monkeypatch.setattr(
        server, "wait_for_output", lambda output_file, process: True
    )
    link = queue_link(client, "https://a.example/x")

    server.detect_link(server.pending.get_nowait())

    assert (link.state, link.detail) == (state, detail)
    assert recorded == ([result[0]] if state == "recording" else [])


def test_link_stays_in_list_while_recording(client, monkeypatch):
    recording = Recording(pid=4242, output_file="/r/nyancat.ts", elapsed=5)
    monkeypatch.setattr(server, "find_recordings", lambda: [recording])
    link = queue_link(client)
    link.state, link.output_file = "recording", "/r/nyancat.ts"
    assert (
        client.delete(f"/api/links/{link.id}", headers=AUTH).status_code == 409
    )

    # Once the recording has ended the link can go without a state request
    monkeypatch.setattr(server, "find_recordings", list)
    monkeypatch.setattr(server, "take_exit_code", lambda output_file: 0)
    assert (
        client.delete(f"/api/links/{link.id}", headers=AUTH).status_code == 204
    )


def test_recording_without_link_is_adopted(client, monkeypatch):
    recordings = [
        Recording(pid=1, output_file="/r/older_1.ts", elapsed=90),
        # Two processes writing the same file still are one entry
        Recording(pid=3, output_file="/r/older_1.ts", elapsed=80),
        Recording(pid=2, output_file="/r/nyancat.ts", elapsed=5),
    ]
    monkeypatch.setattr(server, "find_recordings", lambda: recordings)
    link = queue_link(client)
    link.state, link.output_file = "recording", "/r/nyancat.ts"

    for _ in range(2):
        described = client.get("/api/state", headers=AUTH).json()["links"]
        assert [(entry["name"], entry["state"]) for entry in described] == [
            ("nyancat", "recording"),
            ("older_1", "recording"),
        ]
    adopted = described[1]
    assert adopted["url"] == ""
    assert (
        client.delete(f"/api/links/{adopted['id']}", headers=AUTH).status_code
        == 409
    )

    # Its exit code is unknown, as another program started it
    monkeypatch.setattr(server, "find_recordings", lambda: recordings[2:])
    described = client.get("/api/state", headers=AUTH).json()["links"]
    assert (described[1]["state"], described[1]["detail"]) == ("finished", "")
    assert (
        client.delete(f"/api/links/{adopted['id']}", headers=AUTH).status_code
        == 204
    )


def test_starting_recording_is_not_adopted(client, monkeypatch):
    link = queue_link(client)

    def wait_for_output(output_file, process):
        # ffmpeg is already in the process list while the link is starting
        recording = Recording(pid=1, output_file=output_file, elapsed=1)
        monkeypatch.setattr(server, "find_recordings", lambda: [recording])
        assert len(client.get("/api/state", headers=AUTH).json()["links"]) == 1
        return False

    monkeypatch.setattr(
        server, "start_recording", lambda *arguments: Process()
    )
    monkeypatch.setattr(server, "wait_for_output", wait_for_output)
    server.record_link(link, "m.m3u8", {})

    # It may still be listed for a moment after it was terminated
    described = client.get("/api/state", headers=AUTH).json()["links"]
    assert [entry["state"] for entry in described] == ["error"]


def test_full_list_adopts_no_recording(client, monkeypatch):
    for number in range(server.MAX_LINKS):
        queue_link(client, f"https://a.example/{number}")
    recording = Recording(pid=1, output_file="/r/nyancat.ts", elapsed=5)
    monkeypatch.setattr(server, "find_recordings", lambda: [recording])

    state = client.get("/api/state", headers=AUTH).json()
    assert len(state["links"]) == server.MAX_LINKS
    assert all(entry["state"] == "waiting" for entry in state["links"])

    next(iter(server.links.values())).state = "not_found"
    described = client.get("/api/state", headers=AUTH).json()["links"]
    assert len(described) == server.MAX_LINKS
    assert (described[-1]["name"], described[-1]["state"]) == (
        "nyancat",
        "recording",
    )


def test_recording_links_are_not_dropped_from_full_queue(client):
    for number in range(server.MAX_LINKS):
        queue_link(client, f"https://a.example/{number}").state = "recording"
    full = client.post(
        "/api/links", json={"url": "https://a.example/x"}, headers=AUTH
    )
    assert full.status_code == 429


def test_worker_skips_removed_link(client, monkeypatch):
    monkeypatch.setattr(server, "find_m3u8_url", pytest.fail)
    link = client.post(
        "/api/links", json={"url": "https://a.example/x"}, headers=AUTH
    ).json()
    assert (
        client.delete(f"/api/links/{link['id']}", headers=AUTH).status_code
        == 204
    )

    server.detect_link(server.pending.get_nowait())
