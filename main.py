import curses
import subprocess
import os
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
import time
import env
from threading import Timer, Event


def process_input(input_string):
    """
    Strips a stream name from a url
    """
    if "/" in input_string:
        stream_name = input_string.split("/")[-1]
    else:
        stream_name = input_string
    return stream_name


def get_unique_file_name(base_name):
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


def record_stream(m3u8_url, output_file):
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


def main(stdscr):
    curses.cbreak()
    curses.noecho()
    stdscr.keypad(True)

    history = []
    history_index = -1

    while True:
        raw_input = ""
        if env.favorites and len(env.favorites) > 0:
            history.extend(env.favorites)
        history_index = len(history)
        prompt = "Enter the stream you want to watch or record: "
        stdscr.addstr("\n" + prompt)
        stdscr.refresh()

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
                stdscr.addstr(raw_input)
                stdscr.refresh()

            elif key == curses.KEY_DOWN:
                if history and history_index < len(history) - 1:
                    history_index += 1
                    raw_input = history[history_index]
                else:
                    history_index = len(history)
                    raw_input = ""
                stdscr.move(stdscr.getyx()[0], len(prompt))
                stdscr.clrtoeol()
                stdscr.addstr(raw_input)
                stdscr.refresh()

            elif key in [10, 13]:
                break

            elif key in [curses.KEY_BACKSPACE, 127, 8]:
                if len(raw_input) > 0:
                    raw_input = raw_input[:-1]
                    stdscr.move(stdscr.getyx()[0], len(prompt))
                    stdscr.clrtoeol()
                    stdscr.addstr(raw_input)
                    stdscr.refresh()

            else:
                raw_input += chr(key)
                stdscr.move(stdscr.getyx()[0], len(prompt))
                stdscr.clrtoeol()
                stdscr.addstr(raw_input)
                stdscr.refresh()

        raw_input = raw_input.strip()
        if raw_input:
            history.append(raw_input)

        stream_name = process_input(raw_input)
        video_url = f"{env.base_url}/{stream_name}"
        output_file = get_unique_file_name(f"{stream_name}.mp4")

        try:
            mitmproxy_command = ["mitmdump", "-s", "capture_video_requests.py"]
            mitmproxy_process = subprocess.Popen(
                mitmproxy_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            printed_urls = set()
            m3u8_url_to_play = None
            options = webdriver.ChromeOptions()
            options.add_argument("--proxy-server=http://127.0.0.1:8080")
            service = Service(env.chromedriver_path)

            driver = webdriver.Chrome(service=service, options=options)
            driver.get(video_url)
            time.sleep(1)

            m3u8_detected = Event()

            def timeout_handler():
                if not m3u8_detected.is_set():
                    stdscr.addstr(
                        "\nNo .m3u8 URL detected within 10 seconds. Restarting...\n"
                    )
                    stdscr.refresh()
                    mitmproxy_process.terminate()
                    mitmproxy_process.wait()
                    try:
                        driver.quit()
                    except Exception as e:
                        stdscr.addstr(f"Error while quitting driver: {e}\n")
                        stdscr.refresh()
                    return

            timeout_timer = Timer(10, timeout_handler)
            timeout_timer.start()

            for line in mitmproxy_process.stdout:
                if ".m3u8" in line:
                    timeout_timer.cancel()
                    m3u8_detected.set()
                    m3u8_url = line.strip()
                    if m3u8_url in printed_urls:
                        m3u8_url_to_play = m3u8_url
                        break
                    else:
                        printed_urls.add(m3u8_url)

        except Exception as e:
            stdscr.addstr(f"Error occurred: {e}\n")
            stdscr.refresh()
            mitmproxy_process.terminate()
            mitmproxy_process.wait()
            raise e

        finally:
            if "driver" in locals():
                try:
                    driver.quit()
                except Exception as e:
                    stdscr.addstr(f"Error while quitting driver: {e}\n")
                    stdscr.refresh()
            mitmproxy_process.terminate()
            mitmproxy_process.wait()

        if m3u8_url_to_play:
            vlc_process = subprocess.Popen(
                ["vlc", m3u8_url_to_play],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            stdscr.addstr(
                "\nPress RETURN to start recording or TAB to switch streams: "
            )
            stdscr.refresh()

            while True:
                key = stdscr.getch()

                if key == curses.KEY_ENTER or key in [10, 13]:
                    stdscr.addstr("\nStarting recording...\n")
                    stdscr.refresh()
                    vlc_process.terminate()
                    vlc_process.wait()

                    record_stream(m3u8_url_to_play, output_file)
                    stdscr.addstr("\nRecording and playback started.\n")
                    break

                elif key == 9:
                    stdscr.addstr("\nRestarting for a new stream...\n")
                    stdscr.refresh()
                    vlc_process.terminate()
                    vlc_process.wait()
                    break


if __name__ == "__main__":
    curses.wrapper(main)
