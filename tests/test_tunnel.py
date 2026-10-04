import asyncio
import subprocess
import sys

import tunnel
from streams import TUNNEL


def test_connection_is_made_again_while_attempts_hang(monkeypatch):
    calls = []

    async def open_connection(host, port):
        calls.append((host, port))
        if len(calls) < 3:
            # A dropped connection is never answered
            await asyncio.sleep(3600)
        return "reader", "writer"

    monkeypatch.setattr(tunnel, "ATTEMPT_INTERVAL", 0.01)
    monkeypatch.setattr(tunnel.asyncio, "open_connection", open_connection)

    assert asyncio.run(tunnel.open_connection("a.example", 443)) == (
        "reader",
        "writer",
    )
    assert calls == [("a.example", 443)] * 3


def test_unreachable_server_is_given_up(monkeypatch):
    async def open_connection(host, port):
        raise OSError("refused")

    monkeypatch.setattr(tunnel, "ATTEMPT_INTERVAL", 0.01)
    monkeypatch.setattr(tunnel.asyncio, "open_connection", open_connection)

    assert asyncio.run(tunnel.open_connection("a.example", 443)) is None


def test_command_reaches_a_server_through_the_proxy():
    async def scenario():
        async def greet(reader, writer):
            writer.write(b"hello")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(greet, "127.0.0.1", 0)
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
            greeting = await reader.read()
            writer.close()
        return response, greeting

    response, greeting = asyncio.run(scenario())
    assert response.startswith(b"HTTP/1.1 200")
    assert greeting == b"hello"


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
