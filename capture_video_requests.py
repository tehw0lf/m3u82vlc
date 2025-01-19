from mitmproxy import http


def request(flow: http.HTTPFlow) -> None:
    if ".m3u8" in flow.request.pretty_url:
        print(flow.request.pretty_url)
