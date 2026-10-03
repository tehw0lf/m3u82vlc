import curses
import os
import subprocess
import time
from threading import Timer
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError

import env
from streams import (
    DETECTION_TIMEOUT,
    Recording,
    SplitStream,
    find_m3u8_url,
    find_recordings,
    get_unique_file_name,
    process_input,
    recording_size,
    start_recording,
    stop_recording,
    wait_for_output,
)

timer = None


# Lines of the recordings panel, including its title and separator
PANEL_HEIGHT = 7


KEY_TAB = 9


KEY_ESCAPE = 27


def quit_curses(stdscr: curses.window) -> None:
    curses.endwin()
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


def vlc_http_options(headers: dict[str, str]) -> list[str]:
    options = []
    if "User-Agent" in headers:
        options.append(f"--http-user-agent={headers['User-Agent']}")
    if "Referer" in headers:
        options.append(f"--http-referrer={headers['Referer']}")
    return options


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
    recording.
    """
    start_recording(stream, output_file, headers)
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


def describe_recording(recording: Recording) -> str:
    name = os.path.basename(recording.output_file)
    minutes, seconds = divmod(int(recording.elapsed), 60)
    hours, minutes = divmod(minutes, 60)
    megabytes = recording_size(recording) / 1_000_000
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

            print_dot(stdscr)
            try:
                stream, headers = find_m3u8_url(video_url)
            except PlaywrightError as e:
                # The page could not be loaded, e.g. a mistyped URL
                reason = str(e).partition("\n")[0]
                curse_print(stdscr, f"\nError occurred: {reason}\n")
                continue
            finally:
                stop_dots()

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
                    f"\nNo .m3u8 URL detected within {DETECTION_TIMEOUT} seconds"
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
