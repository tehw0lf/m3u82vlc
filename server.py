import os
import queue
import secrets
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlparse

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import env
from page import INDEX_HTML
from streams import (
    FFMPEG_STOPPED,
    Recording,
    SplitStream,
    find_m3u8_url,
    find_recordings,
    get_unique_file_name,
    process_input,
    recording_size,
    start_recording,
    stop_recording,
    take_exit_code,
    wait_for_output,
)

HOST = getattr(env, "server_host", "127.0.0.1")
PORT = getattr(env, "server_port", 8338)
# Without a configured token a new one is generated on every start
TOKEN = getattr(env, "server_token", None) or secrets.token_urlsafe(16)

# Links kept in the list, including the finished ones
MAX_LINKS = 50

# States in which a link cannot be removed from the list
BUSY_STATES = ("searching", "starting", "recording")

# States in which a link is still being worked on
ACTIVE_STATES = ("waiting", *BUSY_STATES)


@dataclass
class Link:
    id: str
    url: str
    state: str = "waiting"
    detail: str = ""
    output_file: str | None = None


links: dict[str, Link] = {}
lock = threading.Lock()
# ffmpeg creates its output file only once data arrives, so the names
# handed out in the meantime are kept here to not use them twice
reserved_files: set[str] = set()
# Each detection launches its own browser, so the links are detected and
# recorded one after another
pending: queue.Queue[Link] = queue.Queue()


def detect_links() -> None:
    while True:
        detect_link(pending.get())


def detect_link(link: Link) -> None:
    with lock:
        if link.id not in links:
            return
        link.state = "searching"
    stream, headers = None, {}
    try:
        stream, headers = find_m3u8_url(link.url)
    # Any failure must only fail this link and not end the worker
    except Exception as e:  # noqa: BLE001
        state, detail = "error", first_line(e)
    else:
        if isinstance(stream, SplitStream):
            state, detail = "starting", stream.resolution or ""
        elif stream:
            state, detail = "starting", ""
        else:
            state, detail = "not_found", ""
    with lock:
        link.state, link.detail = state, detail
    if stream:
        # There is no preview to confirm here, so a found stream is recorded
        record_link(link, stream, headers)


def record_link(
    link: Link, stream: str | SplitStream, headers: dict[str, str]
) -> None:
    with lock:
        output_file = get_unique_file_name(
            f"{process_input(link.url)}.ts", reserved_files
        )
        reserved_files.add(output_file)
    process = None
    started = False
    detail = "Recording did not start, send the link again"
    try:
        process = start_recording(stream, output_file, headers)
        started = wait_for_output(output_file)
    # Any failure must only fail this link and not end the worker
    except Exception as e:  # noqa: BLE001
        detail = first_line(e)
    if process is not None and not started:
        process.terminate()
    with lock:
        reserved_files.discard(output_file)
        if started:
            link.state, link.output_file = "recording", output_file
        else:
            link.state, link.detail = "error", detail


def first_line(error: Exception) -> str:
    return str(error).partition("\n")[0][:200]


def describe_exit(exit_code: int | None) -> tuple[str, str]:
    """
    Turns the exit code of an ended recording into the state and detail of
    its link.
    """
    if exit_code is None:
        # Started by an earlier run of the server, so the code is unknown
        return "finished", ""
    if exit_code == 0:
        return "finished", "Stream ended"
    if exit_code == FFMPEG_STOPPED:
        return "finished", "Stopped"
    return "error", f"Recording failed, ffmpeg exit code {exit_code}"


def update_ended_links(recordings: list[Recording]) -> None:
    """
    Marks the links whose recording is no longer running. The lock must be
    held.
    """
    output_files = {recording.output_file for recording in recordings}
    for link in links.values():
        if link.state == "recording" and link.output_file not in output_files:
            link.state, link.detail = describe_exit(
                take_exit_code(link.output_file)
            )


def require_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = f"Bearer {TOKEN}".encode()
    if authorization is None or not secrets.compare_digest(
        authorization.encode(), expected
    ):
        raise HTTPException(status_code=401, detail="Invalid token")


def describe_link(link: Link) -> dict[str, str]:
    return {
        "id": link.id,
        "url": link.url,
        "name": process_input(link.url),
        "state": link.state,
        "detail": link.detail,
    }


def find_link(link_id: str) -> Link:
    link = links.get(link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Unknown link")
    return link


class LinkRequest(BaseModel):
    url: str


api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@api.get("/state")
def get_state() -> dict[str, list[dict]]:
    recordings = find_recordings()
    with lock:
        update_ended_links(recordings)
        described_links = [describe_link(link) for link in links.values()]
    return {
        "links": described_links,
        "recordings": [
            {
                "pid": recording.pid,
                "name": os.path.basename(recording.output_file),
                "elapsed": int(recording.elapsed),
                "size": recording_size(recording),
            }
            for recording in recordings
        ],
    }


@api.post("/links", status_code=201)
def add_link(request: LinkRequest) -> dict[str, str]:
    url = request.url.strip()
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(status_code=422, detail="Not a http(s) URL")
    link = Link(id=secrets.token_hex(8), url=url)
    with lock:
        if len(links) >= MAX_LINKS:
            done = [
                link_id
                for link_id, old_link in links.items()
                if old_link.state not in ACTIVE_STATES
            ]
            if not done:
                raise HTTPException(status_code=429, detail="Queue is full")
            del links[done[0]]
        links[link.id] = link
    pending.put(link)
    return describe_link(link)


@api.delete("/links/{link_id}", status_code=204)
def remove_link(link_id: str) -> None:
    recordings = find_recordings()
    with lock:
        link = find_link(link_id)
        update_ended_links(recordings)
        if link.state in BUSY_STATES:
            raise HTTPException(status_code=409, detail="Link is in use")
        del links[link_id]


@api.delete("/recordings/{pid}", status_code=204)
def stop(pid: int) -> None:
    # Only processes recognized as recordings may be stopped
    for recording in find_recordings():
        if recording.pid == pid:
            stop_recording(recording)
            return
    raise HTTPException(status_code=404, detail="Unknown recording")


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=detect_links, daemon=True).start()
    yield


app = FastAPI(
    title="m3u82vlc", lifespan=lifespan, docs_url=None, redoc_url=None
)
app.include_router(api)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


if __name__ == "__main__":
    print(f"Open http://{HOST}:{PORT}/?token={TOKEN}")
    uvicorn.run(app, host=HOST, port=PORT)
