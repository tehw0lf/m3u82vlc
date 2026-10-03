import curses
import gzip
import io
import os
import re
import signal
import subprocess
import time
from contextlib import redirect_stderr
from dataclasses import dataclass
from threading import Timer
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from cloakbrowser import launch_context
from playwright.sync_api import Error as PlaywrightError

import env

timer = None

# Seconds to wait for the audio or video counterpart of a playlist
PARTNER_STREAM_TIMEOUT = 5

# Request headers of the browser that VLC and ffmpeg send as well
FORWARDED_HEADERS = ("user-agent", "referer", "origin")

# Lines of the recordings panel, including its title and separator
PANEL_HEIGHT = 7

KEY_TAB = 9
KEY_ESCAPE = 27


def quit_curses(stdscr: curses.window) -> None:
    curses.endwin()
    stdscr.refresh()


def quit_browser(context, stdscr: curses.window) -> None:
    try:
        context.close()
    except PlaywrightError as e:
        curse_print(stdscr, f"Error while quitting browser: {e}\n")
        stdscr.refresh()


def quit_vlc(vlc_process: subprocess.Popen[str]):
    vlc_process.terminate()
    vlc_process.wait()


def print_dot(stdscr: curses.window) -> None:
    global timer
    curse_print(stdscr, ".")
    timer = Timer(1, lambda: print_dot(stdscr))
    timer.start()


def stop_dots() -> None:
    if timer:
        timer.cancel()


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


def get_unique_file_name(base_name: str) -> str:
    """
    Return a unique file name based on a stream name, by incrementing a counter.
    """
    base_name = os.path.join(env.base_path, base_name)
    if not os.path.exists(base_name):
        return base_name
    name, ext = os.path.splitext(base_name)
    counter = 1
    while True:
        new_name = f"{name}_{counter}{ext}"
        if not os.path.exists(new_name):
            return new_name
        counter += 1


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


def wait_for_output(output_file: str, timeout: float = 15) -> None:
    """
    Waits until the recorder has written data, so playback does not open
    an empty or missing file.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(output_file) and os.path.getsize(output_file):
            return
        time.sleep(0.25)


def vlc_http_options(headers: dict[str, str]) -> list[str]:
    options = []
    if "User-Agent" in headers:
        options.append(f"--http-user-agent={headers['User-Agent']}")
    if "Referer" in headers:
        options.append(f"--http-referrer={headers['Referer']}")
    return options


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


def preview_command(
    stream: str | SplitStream, headers: dict[str, str]
) -> list[str]:
    if isinstance(stream, SplitStream):
        return [
            "vlc",
            stream.video_url,
            f"--input-slave={stream.audio_url}",
            *vlc_http_options(headers),
        ]
    return ["vlc", stream, *vlc_http_options(headers)]


def record_stream(
    stream: str | SplitStream, output_file: str, headers: dict[str, str]
) -> None:
    """
    Starts the recording and playback processes detached from the main program.
    ffmpeg records in the background, so closing the player does not end the
    recording. It writes MPEG-TS, which stays playable while being written.
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
    subprocess.Popen(
        ["nohup", *record_command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    wait_for_output(output_file)
    subprocess.Popen(
        [
            "nohup",
            "vlc",
            output_file,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@dataclass
class Recording:
    pid: int
    output_file: str
    elapsed: float


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
    return sorted(recordings, key=lambda recording: -recording.elapsed)


def stop_recording(recording: Recording) -> None:
    try:
        os.kill(recording.pid, signal.SIGTERM)
    except OSError:
        pass


def describe_recording(recording: Recording) -> str:
    name = os.path.basename(recording.output_file)
    minutes, seconds = divmod(int(recording.elapsed), 60)
    hours, minutes = divmod(minutes, 60)
    try:
        megabytes = os.path.getsize(recording.output_file) / 1_000_000
    except OSError:
        megabytes = 0
    return (
        f" {name:<40.40} {hours}:{minutes:02}:{seconds:02}"
        f" {megabytes:>9.1f} MB"
    )


class RecordingsPanel:
    """
    Lists the running recordings above the prompt and lets the user stop
    them after a confirmation.
    """

    def __init__(self, window: curses.window) -> None:
        self.window = window
        self.selected_pid = None
        self.focused = False
        self.confirming = False
        # ffmpeg takes a moment to exit, so mark what was already stopped
        self.stopping_pids: set[int] = set()

    def draw(self) -> list[Recording]:
        recordings = find_recordings()
        pids = [recording.pid for recording in recordings]
        if self.selected_pid not in pids:
            self.selected_pid = pids[0] if pids else None
            self.confirming = False

        selected = pids.index(self.selected_pid) if pids else 0
        if self.confirming:
            name = os.path.basename(recordings[selected].output_file)
            title = f"Stop recording {name}? y = stop, any other key = keep"
        elif self.focused:
            title = "Recordings: UP/DOWN select, x stop, TAB back"
        elif recordings:
            title = f"Recordings ({len(recordings)}): TAB to manage"
        else:
            title = "No active recordings"

        height, width = self.window.getmaxyx()
        rows = height - 2
        # Scroll so the selected recording stays visible
        first = max(0, selected - rows + 1) if self.focused else 0
        try:
            self.window.erase()
            self.window.addnstr(0, 0, title, width - 1, curses.A_BOLD)
            for row, recording in enumerate(recordings[first : first + rows]):
                highlighted = self.focused and first + row == selected
                description = describe_recording(recording)
                if recording.pid in self.stopping_pids:
                    description += "  stopping..."
                self.window.addnstr(
                    row + 1,
                    0,
                    description,
                    width - 1,
                    curses.A_REVERSE if highlighted else curses.A_NORMAL,
                )
            self.window.hline(height - 1, 0, curses.ACS_HLINE, width)
            self.window.noutrefresh()
        except curses.error:
            pass
        return recordings

    def manage(self, stdscr: curses.window) -> None:
        """
        Hands the keyboard to the panel until the user leaves it or the
        last recording ends.
        """
        self.focused = True
        while True:
            recordings = self.draw()
            curses.doupdate()
            if not recordings:
                break
            key = stdscr.getch()
            pids = [recording.pid for recording in recordings]
            selected = pids.index(self.selected_pid)
            if key == -1:
                continue
            if self.confirming:
                if key in (ord("y"), ord("Y")):
                    stop_recording(recordings[selected])
                    self.stopping_pids.add(self.selected_pid)
                self.confirming = False
            elif key == curses.KEY_UP:
                self.selected_pid = pids[max(0, selected - 1)]
            elif key == curses.KEY_DOWN:
                self.selected_pid = pids[min(len(pids) - 1, selected + 1)]
            elif key in (ord("x"), ord("X"), curses.KEY_DC):
                self.confirming = True
            elif key in (KEY_TAB, KEY_ESCAPE, ord("q")):
                break
        self.focused = False
        self.confirming = False


def read_key(
    stdscr: curses.window, panel: RecordingsPanel, tab_manages: bool
) -> int:
    """
    Waits for a key while keeping the recordings panel up to date. TAB
    opens the panel instead of being returned if tab_manages is set.
    """
    stdscr.timeout(1000)
    try:
        while True:
            recordings = panel.draw()
            # Refresh the prompt last to put the cursor back into it
            stdscr.noutrefresh()
            curses.doupdate()
            key = stdscr.getch()
            if key in (-1, curses.KEY_RESIZE):
                continue
            if key == KEY_TAB and tab_manages:
                if recordings:
                    panel.manage(stdscr)
                continue
            return key
    finally:
        stdscr.timeout(-1)


def curse_print(stdscr: curses.window, input: str) -> None:
    try:
        stdscr.addstr(input)
        stdscr.refresh()
    except curses.error:
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
    stdscr: curses.window, video_url: str, use_headless: bool, timeout: int
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
                headless=use_headless,
                viewport={"width": 1920, "height": 1080},
            )
        page = context.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        print_dot(stdscr)

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
            deadline = time.monotonic() + timeout
            while not detection_done():
                now = time.monotonic()
                if now >= deadline or (
                    first_seen is not None
                    and now >= first_seen + PARTNER_STREAM_TIMEOUT
                ):
                    break
                page.wait_for_timeout(250)
        except PlaywrightError:
            # Browser window closed by the user: keep whatever was found
            if not page_was_closed(page):
                raise

    except Exception as e:
        curse_print(stdscr, f"Error occurred: {e}\n")
        raise

    finally:
        stop_dots()
        if context is not None:
            quit_browser(context, stdscr)

    stream = select_stream(m3u8_urls)
    if isinstance(stream, str):
        split_stream = find_split_stream(
            stream, headers, playlists.get(stream)
        )
        stream = split_stream or stream
    elif stream:
        stream = find_llhls_master(stream, headers) or stream
    return stream, headers


def main(screen: curses.window) -> None:
    # The recordings panel sits on top, everything else scrolls below it
    height, width = screen.getmaxyx()
    panel = RecordingsPanel(curses.newwin(PANEL_HEIGHT, width, 0, 0))
    stdscr = curses.newwin(height - PANEL_HEIGHT, width, PANEL_HEIGHT, 0)
    stdscr.scrollok(True)
    stdscr.keypad(True)

    history = []
    history_index = -1
    try:
        while True:
            raw_input = ""
            if env.favorites and len(env.favorites) > 0:
                history.extend(env.favorites)
            history_index = len(history)
            prompt = "Enter the stream URL you want to watch or record: "
            curse_print(stdscr, "\n" + prompt)

            while True:
                key = read_key(stdscr, panel, tab_manages=True)

                if key == curses.KEY_UP:
                    if history and history_index > 0:
                        history_index -= 1
                        raw_input = history[history_index]
                    elif history_index == 0:
                        continue
                    stdscr.move(stdscr.getyx()[0], len(prompt))
                    stdscr.clrtoeol()
                    curse_print(stdscr, raw_input)

                elif key == curses.KEY_DOWN:
                    if history and history_index < len(history) - 1:
                        history_index += 1
                        raw_input = history[history_index]
                    else:
                        history_index = len(history)
                        raw_input = ""
                    stdscr.move(stdscr.getyx()[0], len(prompt))
                    stdscr.clrtoeol()
                    curse_print(stdscr, raw_input)

                elif key in [10, 13]:
                    break

                elif key in [curses.KEY_BACKSPACE, 127, 8]:
                    if len(raw_input) > 0:
                        raw_input = raw_input[:-1]
                        stdscr.move(stdscr.getyx()[0], len(prompt))
                        stdscr.clrtoeol()
                        curse_print(stdscr, raw_input)

                else:
                    raw_input += chr(key)
                    stdscr.move(stdscr.getyx()[0], len(prompt))
                    stdscr.clrtoeol()
                    curse_print(stdscr, raw_input)

            video_url = raw_input.strip()
            if not video_url:
                continue
            if raw_input:
                history.append(raw_input)

            stream_name = process_input(video_url)
            output_file = get_unique_file_name(f"{stream_name}.ts")

            use_headless = not any(
                condition in video_url
                for condition in env.non_headless_mode_conditions
            )
            timer_duration = 15 if use_headless else 600
            stream, headers = find_m3u8_url(
                stdscr, video_url, use_headless, timer_duration
            )

            if stream:
                if isinstance(stream, SplitStream):
                    resolution = stream.resolution or "unknown resolution"
                    curse_print(
                        stdscr,
                        "\nSeparate audio and video detected"
                        f" ({resolution}).\n",
                    )
                else:
                    file_name = os.path.basename(urlparse(stream).path)
                    curse_print(stdscr, f"\nDetected {file_name}.\n")
                vlc_process = subprocess.Popen(
                    preview_command(stream, headers),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                curse_print(
                    stdscr,
                    "\nPress RETURN to start recording or TAB to switch streams: ",
                )

                while True:
                    # TAB switches streams here, so it cannot open the panel
                    key = read_key(stdscr, panel, tab_manages=False)

                    if key == curses.KEY_ENTER or key in [10, 13]:
                        curse_print(stdscr, "\nStarting recording...\n")
                        quit_vlc(vlc_process)
                        record_stream(stream, output_file, headers)
                        curse_print(
                            stdscr, "\nRecording and playback started.\n"
                        )
                        break

                    elif key == KEY_TAB:
                        curse_print(
                            stdscr, "\nRestarting for a new stream...\n"
                        )
                        quit_vlc(vlc_process)
                        break
            else:
                curse_print(
                    stdscr,
                    f"\nNo .m3u8 URL detected within {timer_duration} seconds"
                    " after page load. Restarting...\n",
                )

    except KeyboardInterrupt:
        stop_dots()
        quit_curses(stdscr)
    finally:
        try:
            time.sleep(0.01)
        except KeyboardInterrupt:
            stop_dots()
            quit_curses(stdscr)
        finally:
            pass


if __name__ == "__main__":
    curses.wrapper(main)
