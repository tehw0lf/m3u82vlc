import curses
import os
import subprocess
import time
from threading import Event, Timer

import undetected_chromedriver as uc

import env

timer = None


def quit_curses(stdscr: curses.window) -> None:
    curses.nocbreak()
    stdscr.refresh()
    curses.endwin()


def quit_chromedriver(driver: uc.Chrome, stdscr: curses.window) -> None:
    try:
        driver.quit()
    except Exception as e:
        curse_print(stdscr, f"Error while quitting driver: {e}\n")
        stdscr.refresh()


def quit_mitmproxy(mitmproxy_process: subprocess.Popen[str]) -> None:
    mitmproxy_process.terminate()
    mitmproxy_process.wait()


def quit_vlc(vlc_process: subprocess.Popen[str]):
    vlc_process.terminate()
    vlc_process.wait()


def print_dot(stdscr: curses.window) -> None:
    global timer
    curse_print(stdscr, ".")
    timer = Timer(1, lambda: print_dot(stdscr))
    timer.start()


def stop_dots() -> None:
    global timer
    if timer:
        timer.cancel()


def process_input(input: str) -> str:
    """
    Strips a stream name from a url
    """
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


def record_stream(m3u8_url: str, output_file: str) -> None:
    """
    Starts the recording and playback processes detached from the main program.
    """
    subprocess.Popen(
        [
            "vlc",
            m3u8_url,
            "--sout",
            f"#standard{{access=file,mux=ts,dst={output_file}}}",
            "--no-sout-all",
            "--sout-keep",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    time.sleep(1)
    subprocess.Popen(
        ["vlc", output_file],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )


def curse_print(stdscr: curses.window, input: str) -> None:
    try:
        stdscr.addstr(input)
        stdscr.refresh()
    except curses.error:
        pass


def main(stdscr: curses.window) -> None:
    curses.cbreak()
    stdscr.keypad(True)

    history = []
    history_index = -1
    try:
        while True:
            raw_input = ""
            if env.favorites and len(env.favorites) > 0:
                history.extend(env.favorites)
            history_index = len(history)
            prompt = "Enter the stream you want to watch or record: "
            curse_print(stdscr, "\n" + prompt)

            while True:
                key = stdscr.getch()

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

            raw_input = raw_input.strip()
            if raw_input:
                history.append(raw_input)

            stream_name = process_input(raw_input)
            video_url = f"{env.base_url}/{stream_name}"
            output_file = get_unique_file_name(f"{stream_name}.mp4")

            try:
                mitmproxy_command = [
                    "mitmdump",
                    "-s",
                    "capture_video_requests.py",
                ]
                mitmproxy_process = subprocess.Popen(
                    mitmproxy_command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                printed_urls = set()
                m3u8_url_to_play = None
                options = uc.ChromeOptions()
                options.add_argument("--proxy-server=http://127.0.0.1:8080")

                driver = uc.Chrome(
                    headless=True, use_subprocess=False, options=options
                )
                print_dot(stdscr)
                driver.get(video_url)

                m3u8_detected = Event()

                def timeout_handler():
                    if not m3u8_detected.is_set():
                        stop_dots()
                        curse_print(
                            stdscr,
                            "\nNo .m3u8 URL detected within 5 seconds. Restarting...\n",
                        )
                        quit_mitmproxy(mitmproxy_process)
                        quit_chromedriver(driver, stdscr)
                        return

                timeout_timer = Timer(5, timeout_handler)
                timeout_timer.start()

                for line in mitmproxy_process.stdout:
                    if ".m3u8" in line:
                        stop_dots()
                        timeout_timer.cancel()
                        m3u8_detected.set()
                        m3u8_url = line.strip()
                        if m3u8_url in printed_urls:
                            m3u8_url_to_play = m3u8_url
                            break
                        else:
                            printed_urls.add(m3u8_url)

            except Exception as e:
                curse_print(stdscr, f"Error occurred: {e}\n")
                quit_mitmproxy(mitmproxy_process)
                raise e

            finally:
                if "driver" in locals():
                    quit_chromedriver(driver, stdscr)
                quit_mitmproxy(mitmproxy_process)

            if m3u8_url_to_play:
                vlc_process = subprocess.Popen(
                    ["vlc", m3u8_url_to_play],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                curse_print(
                    stdscr,
                    "\nPress RETURN to start recording or TAB to switch streams: ",
                )

                while True:
                    key = stdscr.getch()

                    if key == curses.KEY_ENTER or key in [10, 13]:
                        curse_print(stdscr, "\nStarting recording...\n")
                        quit_vlc(vlc_process)
                        record_stream(m3u8_url_to_play, output_file)
                        curse_print(
                            stdscr, "\nRecording and playback started.\n"
                        )
                        break

                    elif key == 9:
                        curse_print(
                            stdscr, "\nRestarting for a new stream...\n"
                        )
                        quit_vlc(vlc_process)
                        break

    except KeyboardInterrupt:
        stop_dots()
        quit_curses(stdscr)
        exit()
    finally:
        try:
            time.sleep(0.01)
        except KeyboardInterrupt:
            stop_dots()
            quit_curses(stdscr)
            exit()
        finally:
            pass


if __name__ == "__main__":
    curses.wrapper(main)
