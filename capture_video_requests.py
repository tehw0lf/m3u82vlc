from mitmproxy import http
import env


def request(flow: http.HTTPFlow) -> None:
    if ".m3u8" in flow.request.pretty_url:
        print(flow.request.pretty_url)
    if env.log_file:
        with open(env.log_file, "a") as log:
            log.write(flow.request.pretty_url + "\n")
