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

# Seconds the command has to send its greeting after asking for a server
GREETING_TIMEOUT = 10

# First byte of a TLS record that carries a handshake message
TLS_HANDSHAKE = 0x16

Connection = tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]


async def attempt_connection(
    host: str, port: int, greeting: bytes
) -> Connection:
    """
    Connects to a server, sends the greeting and waits for the first data
    of its answer. A connection that is set up may still never be answered,
    so only the answer tells that it works.
    """
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(greeting)
        await writer.drain()
        answer = await reader.read(65536)
        if not answer:
            raise ConnectionError("Server closed the connection")
    except BaseException:
        writer.close()
        raise
    return reader, writer, answer


async def open_connection(
    host: str, port: int, greeting: bytes
) -> Connection | None:
    """
    Returns the first connection the server answers on, along with the
    start of its answer, or None if it answers on none of them.
    """
    attempts: set[asyncio.Task] = set()
    try:
        for _ in range(ATTEMPTS):
            attempts.add(
                asyncio.ensure_future(attempt_connection(host, port, greeting))
            )
            done, _ = await asyncio.wait(
                attempts,
                timeout=ATTEMPT_INTERVAL,
                return_when=asyncio.FIRST_COMPLETED,
            )
            attempts -= done
            answered = [
                attempt.result()
                for attempt in done
                if attempt.exception() is None
            ]
            # Only one of the connections answered at the same time is used
            for _, writer, _ in answered[1:]:
                writer.close()
            if answered:
                return answered[0]
        return None
    finally:
        for attempt in attempts:
            attempt.cancel()


async def read_greeting(reader: asyncio.StreamReader) -> bytes:
    """
    Reads what the command sends first, which for https is one TLS record.
    """
    header = await reader.readexactly(5)
    if header[0] != TLS_HANDSHAKE:
        return header
    return header + await reader.readexactly(int.from_bytes(header[3:]))


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
    passes the data on in both directions afterwards. The server is only
    connected once the greeting for it is known, as the greeting is sent
    with every attempt.
    """
    server = None
    try:
        request = await reader.readuntil(b"\r\n\r\n")
        method, target, _ = request.split(b"\r\n")[0].decode().split(" ")
        host, _, port = target.rpartition(":")
        if method != "CONNECT":
            raise ValueError("Not a CONNECT request")
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        greeting = await asyncio.wait_for(
            read_greeting(reader), GREETING_TIMEOUT
        )
        server = await open_connection(host.strip("[]"), int(port), greeting)
        if server is not None:
            writer.write(server[2])
            await writer.drain()
    except OSError, ValueError, TimeoutError, asyncio.IncompleteReadError:
        if server is not None:
            server[1].close()
        server = None
    if server is None:
        # The command takes the closed connection for a failed request
        writer.close()
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
