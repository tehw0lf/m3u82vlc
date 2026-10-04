import asyncio
import subprocess
import sys

import tunnel
from streams import TUNNEL


class Writer:
    def __init__(self):
        self.sent = b""
        self.closed = False

    def write(self, data):
        self.sent += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class Reader:
    def __init__(self, answers):
        self.answers = answers

    async def read(self, size):
        if not self.answers:
            # A server that never answers
            await asyncio.sleep(3600)
        return self.answers.pop(0)


def test_connection_is_made_again_until_the_server_answers(monkeypatch):
    writers = []

    async def open_connection(host, port):
        assert (host, port) == ("a.example", 443)
        writers.append(Writer())
        # The first connections are set up, but never answered
        answers = [b"answer"] if len(writers) == 3 else []
        return Reader(answers), writers[-1]

    monkeypatch.setattr(tunnel, "ATTEMPT_INTERVAL", 0.01)
    monkeypatch.setattr(tunnel.asyncio, "open_connection", open_connection)

    _, writer, answer = asyncio.run(
        tunnel.open_connection("a.example", 443, b"greeting")
    )

    assert answer == b"answer" and writer is writers[2]
    assert [writer.sent for writer in writers] == [b"greeting"] * 3
    assert [writer.closed for writer in writers] == [True, True, False]


def test_unreachable_server_is_given_up(monkeypatch):
    async def open_connection(host, port):
        raise OSError("refused")

    monkeypatch.setattr(tunnel, "ATTEMPT_INTERVAL", 0.01)
    monkeypatch.setattr(tunnel.asyncio, "open_connection", open_connection)

    assert asyncio.run(tunnel.open_connection("a.example", 443, b"")) is None


def test_command_reaches_a_server_through_the_proxy():
    # One TLS handshake record, as the proxy waits for a complete one
    greeting = bytes([tunnel.TLS_HANDSHAKE, 3, 1, 0, 5]) + b"hello"

    async def scenario():
        async def answer(reader, writer):
            writer.write((await reader.readexactly(len(greeting)))[::-1])
            await writer.drain()
            writer.write(await reader.read(100))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(answer, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        proxy = await asyncio.start_server(
            tunnel.handle_client, "127.0.0.1", 0
        )
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", proxy.sockets[0].getsockname()[1]
        )
        async with server, proxy:
            writer.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
            response = await reader.readuntil(b"\r\n\r\n")
            writer.write(greeting)
            first = await reader.readexactly(len(greeting))
            writer.write(b"more")
            rest = await reader.read()
            writer.close()
        return response, first, rest

    response, first, rest = asyncio.run(scenario())
    assert response.startswith(b"HTTP/1.1 200")
    assert (first, rest) == (greeting[::-1], b"more")


def test_plain_request_is_passed_on_to_its_server():
    async def scenario():
        requests = []

        async def answer(reader, writer):
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\n\r\nplaylist")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(answer, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        proxy = await asyncio.start_server(
            tunnel.handle_client, "127.0.0.1", 0
        )
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", proxy.sockets[0].getsockname()[1]
        )
        async with server, proxy:
            writer.write(
                f"GET http://127.0.0.1:{port}/m.m3u8?a=1 HTTP/1.1\r\n"
                "Connection: keep-alive\r\nUser-Agent: UA\r\n\r\n".encode()
            )
            response = await reader.read()
            writer.close()
        return requests, response

    requests, response = asyncio.run(scenario())
    assert response.endswith(b"playlist")
    assert requests[0].startswith(b"GET /m.m3u8?a=1 HTTP/1.1\r\n")
    assert b"User-Agent: UA\r\n" in requests[0]
    assert b"keep-alive" not in requests[0]
    assert requests[0].endswith(b"Connection: close\r\n\r\n")


def test_tunnel_hands_on_the_proxy_and_the_exit_code():
    script = (
        "import os, sys;"
        "sys.exit(7 if os.environ['http_proxy']"
        ".startswith('http://127.0.0.1:') else 1)"
    )
    process = subprocess.run(
        [sys.executable, TUNNEL, sys.executable, "-c", script], check=False
    )
    assert process.returncode == 7
