import json

from mitmproxy import http

import env


def request(flow: http.HTTPFlow) -> None:
    if ".m3u8" in flow.request.pretty_url:
        print(flow.request.pretty_url)
    if flow.response and flow.response.content:
        json_data = json.loads(flow.response.content).get("url")
        if json_data and json_data.endswith(".m3u8"):
            print(json_data)
    if env.log_file:
        with open(env.log_file, "a") as log:
            log.write(flow.request.pretty_url + "\n")
