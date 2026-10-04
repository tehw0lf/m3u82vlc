import gzip
import io
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Collection
from contextlib import redirect_stderr
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from cloakbrowser import launch_context
from playwright.sync_api import Error as PlaywrightError

import env

# Seconds to wait for the audio or video counterpart of a playlist
PARTNER_STREAM_TIMEOUT = 5


# Seconds to wait for a playlist after the stream page has loaded
DETECTION_TIMEOUT = 15

# Request headers of the browser that VLC and ffmpeg send as well
FORWARDED_HEADERS = ("user-agent", "referer", "origin")

# Recordings started by this program. Their exit status has to be collected
# once they end, otherwise they stay behind as defunct processes
started_processes: list[subprocess.Popen] = []
started_processes_lock = threading.Lock()
# Exit codes of the recordings that ended, by their output file
exit_codes: dict[str, int] = {}

# ffmpeg exits with this code when a signal ends it
FFMPEG_STOPPED = 255


def process_input(input: str) -> str:
    """
    Strips a stream name from a url
    """
    while input.endswith("/"):
        input = input[:-1]
    if "/" in input:
        stream_name = input.split("/")[-1]
    else:
        stream_name = input
    return stream_name


def get_unique_file_name(base_name: str, taken: Collection[str] = ()) -> str:
    """
    Return a unique file name based on a stream name, by incrementing a counter.
    Names in taken are skipped as well, for files that do not exist yet.
    """
    # Absolute, as find_recordings reports the output files that way
    base_name = os.path.join(os.path.abspath(env.base_path), base_name)
    name, ext = os.path.splitext(base_name)
    new_name = base_name
    counter = 1
    while os.path.exists(new_name) or new_name in taken:
        new_name = f"{name}_{counter}{ext}"
        counter += 1
    return new_name


@dataclass
class SplitStream:
    video_url: str
    audio_url: str
    resolution: str | None = None


LLHLS_INIT_PATTERN = re.compile(
    r"^init_(\d+)_(audio|video)_(.+)_llhls\.m4s$", re.IGNORECASE
)


def playlist_url(url: str) -> str | None:
    """
    Returns the playlist a request belongs to, or None for other requests.
    LLHLS init segments are mapped to the chunklist of their track, as the
    playlists themselves do not always show up.
    """
    parts = urlparse(url)
    directory, _, file_name = parts.path.rpartition("/")
    match = LLHLS_INIT_PATTERN.match(file_name)
    if match:
        track, kind, key = match.groups()
        parts = parts._replace(
            path=f"{directory}/chunklist_{track}_{kind}_{key}_llhls.m3u8"
        )
    elif ".m3u8" not in url:
        return None
    # Blocking reload parameters would pin the playlist to one segment
    query = "&".join(
        parameter
        for parameter in parts.query.split("&")
        if parameter and not parameter.startswith("_HLS_")
    )
    return parts._replace(query=query).geturl()


def stream_kind(m3u8_url: str) -> str | None:
    """
    Tells a separate audio or video playlist apart from a master playlist,
    based on its file name.
    """
    file_name = os.path.basename(urlparse(m3u8_url).path).lower()
    for kind in ("audio", "video"):
        if kind in file_name:
            return kind
    return None


def select_stream(m3u8_urls: list[str]) -> str | SplitStream | None:
    """
    Prefers a master playlist. Without one, pairs the first separate video
    and audio playlists, falling back to the first URL seen.
    """
    by_kind: dict[str | None, str] = {}
    for url in m3u8_urls:
        by_kind.setdefault(stream_kind(url), url)
    if None in by_kind:
        return by_kind[None]
    if "audio" in by_kind and "video" in by_kind:
        return SplitStream(
            video_url=by_kind["video"], audio_url=by_kind["audio"]
        )
    return m3u8_urls[0] if m3u8_urls else None


def stream_complete(m3u8_urls: list[str]) -> bool:
    kinds = {stream_kind(url) for url in m3u8_urls}
    return None in kinds or {"audio", "video"} <= kinds


ATTRIBUTE_PATTERN = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attributes(line: str) -> dict[str, str]:
    """
    Parses the attribute list of an HLS tag like #EXT-X-STREAM-INF.
    """
    attribute_list = line.partition(":")[2]
    return {
        key: value.strip('"')
        for key, value in ATTRIBUTE_PATTERN.findall(attribute_list)
    }


def fetch_playlist(m3u8_url: str, headers: dict[str, str]) -> str | None:
    request = Request(
        m3u8_url, headers={"User-Agent": "Mozilla/5.0", **headers}
    )
    try:
        with urlopen(request, timeout=10) as response:
            data = response.read()
        # Some servers compress playlists without being asked to
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        playlist = data.decode("utf-8")
    except OSError, ValueError:
        return None
    return playlist if playlist.lstrip().startswith("#EXTM3U") else None


def resolution_area(resolution: str) -> int:
    width, _, height = resolution.partition("x")
    try:
        return int(width) * int(height)
    except ValueError:
        return 0


def find_split_stream(
    m3u8_url: str, headers: dict[str, str], playlist: str | None = None
) -> SplitStream | None:
    """
    Reads a master playlist and returns the best video variant together with
    its separate audio rendition, or None if audio and video are not split.
    The browser picks a variant to fit the screen, so always take the
    largest one to get the best possible quality. The playlist is fetched
    unless the browser already captured its content.
    """
    if playlist is None:
        playlist = fetch_playlist(m3u8_url, headers)
    if playlist is None:
        return None

    audio_groups: dict[str, list[dict[str, str]]] = {}
    variants: list[tuple[dict[str, str], str]] = []
    pending_variant = None
    for line in (line.strip() for line in playlist.splitlines()):
        if line.startswith("#EXT-X-MEDIA:"):
            attributes = parse_attributes(line)
            if attributes.get("TYPE") == "AUDIO" and "URI" in attributes:
                group = attributes.get("GROUP-ID", "")
                audio_groups.setdefault(group, []).append(attributes)
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending_variant = parse_attributes(line)
        elif line and not line.startswith("#") and pending_variant:
            variants.append((pending_variant, line))
            pending_variant = None

    # Renditions without a URI are muxed into the variant, so only
    # variants referencing a group with separate playlists are split
    split_variants = [
        (attributes, uri)
        for attributes, uri in variants
        if attributes.get("AUDIO") in audio_groups
    ]
    if not split_variants:
        return None

    attributes, video_uri = max(
        split_variants,
        key=lambda variant: (
            resolution_area(variant[0].get("RESOLUTION", "")),
            int(variant[0].get("BANDWIDTH", "0") or 0),
        ),
    )
    renditions = audio_groups[attributes["AUDIO"]]
    audio = next(
        (r for r in renditions if r.get("DEFAULT") == "YES"), renditions[0]
    )
    return SplitStream(
        video_url=urljoin(m3u8_url, video_uri),
        audio_url=urljoin(m3u8_url, audio["URI"]),
        resolution=attributes.get("RESOLUTION", "unknown resolution"),
    )


def find_llhls_master(
    split_stream: SplitStream, headers: dict[str, str]
) -> SplitStream | None:
    """
    Looks for the master playlist next to LLHLS chunklists that were seen
    without it, to pick the best variant instead of the one the browser
    happened to load.
    """
    parts = urlparse(split_stream.video_url)
    directory, _, file_name = parts.path.rpartition("/")
    if not file_name.endswith("_llhls.m3u8"):
        return None
    master_url = parts._replace(path=f"{directory}/llhls.m3u8").geturl()
    return find_split_stream(master_url, headers)


def wait_for_output(output_file: str, timeout: float = 15) -> bool:
    """
    Waits until the recorder has written data, so playback does not open
    an empty or missing file. Returns whether data showed up in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(output_file) and os.path.getsize(output_file):
            return True
        time.sleep(0.25)
    return False


def ffmpeg_input(url: str, headers: dict[str, str]) -> list[str]:
    options = []
    if "User-Agent" in headers:
        options += ["-user_agent", headers["User-Agent"]]
    other_headers = "".join(
        f"{name}: {value}\r\n"
        for name, value in headers.items()
        if name != "User-Agent"
    )
    if other_headers:
        options += ["-headers", other_headers]
    return [*options, "-i", url]


@dataclass
class Recording:
    pid: int
    output_file: str
    elapsed: float


def reap_recordings() -> None:
    """
    Collects the exit status of the recordings that ended, for example
    because the stream is over, and keeps it for take_exit_code().
    """
    with started_processes_lock:
        running = []
        for process in started_processes:
            if process.poll() is None:
                running.append(process)
            else:
                exit_codes[process.args[-1]] = process.returncode
        started_processes[:] = running


def take_exit_code(output_file: str) -> int | None:
    """
    Returns the exit code of an ended recording once, or None if it was not
    started by this program or has not been collected yet.
    """
    with started_processes_lock:
        return exit_codes.pop(output_file, None)


def find_recordings() -> list[Recording]:
    """
    Finds the running ffmpeg recordings by their output file in base_path.
    They are read from the process list, as recordings outlive the program
    that started them.
    """
    base_path = os.path.abspath(env.base_path)
    with open("/proc/uptime") as file:
        uptime = float(file.read().split()[0])
    recordings = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as file:
                arguments = file.read().decode(errors="replace").split("\0")
            with open(f"/proc/{entry}/stat") as file:
                # Field 22 is the start time, counted after the process
                # name, which may itself contain spaces
                start_ticks = int(file.read().rpartition(")")[2].split()[19])
        except OSError, ValueError, IndexError:
            continue
        arguments = [argument for argument in arguments if argument]
        if len(arguments) < 2 or "mpegts" not in arguments:
            continue
        if os.path.basename(arguments[0]) != "ffmpeg":
            continue
        output_file = os.path.abspath(arguments[-1])
        if os.path.dirname(output_file) != base_path:
            continue
        started = start_ticks / os.sysconf("SC_CLK_TCK")
        recordings.append(Recording(int(entry), output_file, uptime - started))
    # Collected after reading the process list, so a recording that is no
    # longer listed always has its exit code available
    reap_recordings()
    return sorted(recordings, key=lambda recording: -recording.elapsed)


def recording_size(recording: Recording) -> int:
    try:
        return os.path.getsize(recording.output_file)
    except OSError:
        return 0


def stop_recording(recording: Recording) -> None:
    try:
        os.kill(recording.pid, signal.SIGTERM)
    except OSError:
        pass


def page_was_closed(page) -> bool:
    """
    Tells whether a Playwright error came from the page being closed. The
    navigation error can arrive before the close event, so give the event
    loop one more turn before checking.
    """
    if not page.is_closed():
        try:
            page.wait_for_timeout(100)
        except PlaywrightError:
            pass
    return page.is_closed()


def find_m3u8_url(
    video_url: str,
) -> tuple[str | SplitStream | None, dict[str, str]]:
    """
    Opens the stream page and returns the stream to play along with the
    HTTP headers the browser sent for it. The stream is the separate video
    and audio playlists if they are split, otherwise the first playlist
    seen, or None if no .m3u8 URL shows up within the timeout.
    """
    m3u8_urls: list[str] = []
    playlists: dict[str, str] = {}
    headers: dict[str, str] = {}
    first_seen = None

    def register_m3u8_url(url: str) -> None:
        nonlocal first_seen
        url = playlist_url(url)
        if url is None:
            return
        if url not in m3u8_urls:
            m3u8_urls.append(url)
        if first_seen is None:
            first_seen = time.monotonic()

    def detection_done() -> bool:
        if not stream_complete(m3u8_urls):
            return False
        stream = select_stream(m3u8_urls)
        return not isinstance(stream, str) or stream in playlists

    def on_request(request) -> None:
        register_m3u8_url(request.url)
        if m3u8_urls and not headers:
            for name in FORWARDED_HEADERS:
                if name in request.headers:
                    headers[name.title()] = request.headers[name]

    def on_response(response) -> None:
        # Keep the playlist as the browser received it, as fetching it
        # again outside the browser session can fail
        if ".m3u8" in response.url:
            try:
                playlists[playlist_url(response.url)] = response.text()
            except PlaywrightError, ValueError:
                pass
            return
        try:
            body = response.json()
        except PlaywrightError, ValueError:
            return
        if isinstance(body, dict):
            url = body.get("url")
            if isinstance(url, str):
                register_m3u8_url(url)

    context = None
    try:
        # cloakbrowser writes its welcome banner and font warning to
        # stderr, which would corrupt the curses screen
        with redirect_stderr(io.StringIO()):
            context = launch_context(
                headless=True,
                viewport={"width": 1920, "height": 1080},
            )
        page = context.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        try:
            page.goto(video_url, timeout=30000, wait_until="domcontentloaded")

            # The elements form a chain, so stop at the first missing one
            try:
                for element in env.elements_to_click_on_load:
                    page.locator(f"#{element}").click(timeout=3000)
            except PlaywrightError:
                if page_was_closed(page):
                    raise

            # The sync Playwright API only dispatches request/response
            # events while the main thread is inside a Playwright call.
            # A blocking wait would stop dispatching entirely, so poll
            # with page.wait_for_timeout() to keep the loop running.
            # Once a playlist shows up, wait a little longer for its
            # audio or video counterpart, or for the content of a master
            # playlist, instead of the full timeout
            deadline = time.monotonic() + DETECTION_TIMEOUT
            while not detection_done():
                now = time.monotonic()
                if now >= deadline or (
                    first_seen is not None
                    and now >= first_seen + PARTNER_STREAM_TIMEOUT
                ):
                    break
                page.wait_for_timeout(250)
        except PlaywrightError:
            # Browser closed during detection: keep whatever was found
            if not page_was_closed(page):
                raise

    finally:
        if context is not None:
            try:
                context.close()
            except PlaywrightError:
                pass

    stream = select_stream(m3u8_urls)
    if isinstance(stream, str):
        split_stream = find_split_stream(
            stream, headers, playlists.get(stream)
        )
        stream = split_stream or stream
    elif stream:
        stream = find_llhls_master(stream, headers) or stream
    return stream, headers


def start_recording(
    stream: str | SplitStream, output_file: str, headers: dict[str, str]
) -> subprocess.Popen:
    """
    Starts ffmpeg detached from the calling program, so the recording keeps
    running when the program, the player or the terminal is closed. It
    writes MPEG-TS, which stays playable while being written.
    """
    if isinstance(stream, SplitStream):
        inputs = [
            # Keep the timestamps of the server. Otherwise each input is
            # shifted to start at zero, and audio and video drift apart
            # by however far apart they joined the live stream
            "-copyts",
            *ffmpeg_input(stream.video_url, headers),
            *ffmpeg_input(stream.audio_url, headers),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
    else:
        inputs = ffmpeg_input(stream, headers)
    record_command = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        *inputs,
        "-c",
        "copy",
        "-f",
        "mpegts",
        output_file,
    ]
    process = subprocess.Popen(
        ["nohup", *record_command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    with started_processes_lock:
        started_processes.append(process)
    return process
