"""
Runs a command behind a local HTTP proxy that makes several attempts at
once to reach a server. Usage: tunnel.py <command> [argument ...]

Stream servers drop a part of the connections at times. ffmpeg opens a new
one for nearly every segment and gives up on a recording once one of them
times out, while a browser recovers from it unnoticed. The proxy belongs to
the command and ends with it, so a recording does not depend on the program
that started it.
"""

import asyncio
import os
import signal
import sys

# Seconds after which another attempt joins the ones still under way
ATTEMPT_INTERVAL = 0.3

# Attempts made before a connection counts as failed
ATTEMPTS = 20


async def open_connection(
    host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    """
    Connects to a server and returns the first attempt that gets through,
    or None if none of them does.
    """
    attempts: set[asyncio.Task] = set()
    try:
        for _ in range(ATTEMPTS):
            attempts.add(
                asyncio.ensure_future(asyncio.open_connection(host, port))
            )
            done, _ = await asyncio.wait(
                attempts,
                timeout=ATTEMPT_INTERVAL,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for attempt in done:
                attempts.discard(attempt)
                if attempt.exception() is None:
                    return attempt.result()
        return None
    finally:
        for attempt in attempts:
            attempt.cancel()


async def forward(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """
    Serves one CONNECT request, which is what ffmpeg sends for https, and
    passes the data on in both directions afterwards.
    """
    server = None
    try:
        request = await reader.readuntil(b"\r\n\r\n")
        method, target, _ = request.split(b"\r\n")[0].decode().split(" ")
        host, _, port = target.rpartition(":")
        if method == "CONNECT":
            server = await open_connection(host.strip("[]"), int(port))
    except OSError, ValueError, asyncio.IncompleteReadError:
        pass
    try:
        if server is None:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
    except OSError:
        writer.close()
        if server is not None:
            server[1].close()
        return
    await asyncio.gather(
        forward(reader, server[1]), forward(server[0], writer)
    )


async def run(command: list[str]) -> int:
    """
    Runs the command with the proxy in its environment and returns its exit
    code.
    """
    proxy = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    port = proxy.sockets[0].getsockname()[1]
    environment = dict(os.environ, http_proxy=f"http://127.0.0.1:{port}")
    # Only requests to the stream server may go through the proxy
    environment.pop("no_proxy", None)
    process = await asyncio.create_subprocess_exec(*command, env=environment)

    def stop() -> None:
        if process.returncode is None:
            process.terminate()

    # Stopping the tunnel stops the command, which then ends the tunnel
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop)
    async with proxy:
        return await process.wait()


if __name__ == "__main__":
    exit_code = asyncio.run(run(sys.argv[1:]))
    # A command ended by a signal reports it as a negative number
    sys.exit(exit_code if exit_code >= 0 else 128 - exit_code)
