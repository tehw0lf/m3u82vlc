# m3u82vlc

## usage

this is a curses based CLI to watch and record m3u8 streams.

streams with separate audio and video playlists (e.g. LLHLS) are detected
automatically: the largest video variant is played and recorded together
with its audio track.

recordings run in the background and keep going when the player or the
terminal is closed. the panel at the top lists the running recordings.
press TAB at the URL prompt to select one, x to stop it and y to confirm.

## remote control

`server.py` offers the same detection and recording without a terminal or
player, for example from a phone in the LAN:

```bash
uv run python server.py
```

it prints the address of the web page including the access token. send a
stream URL there and the page shows whether a stream was found (green) or
not (red). as there is no preview, a found stream is recorded right away,
and running recordings can be stopped after a confirmation. links are
checked one after another. a recording started elsewhere, for example in
the terminal, gets an entry in the link list as well, so the page shows
when it has ended.

the same is available as a REST API under `/api`, with the token sent as
`Authorization: Bearer <token>`:

| request | purpose |
| --- | --- |
| `GET /api/state` | links and running recordings |
| `POST /api/links` with `{"url": "..."}` | queue a link for detection and recording |
| `DELETE /api/links/<id>` | remove a link from the list |
| `DELETE /api/recordings/<pid>` | stop a recording |

the connection is plain HTTP, so only use it in a network you trust.

## requirements

- vlc for playback
- ffmpeg for recording
- linux, as the running recordings are read from /proc

## environment

the following options are available and will be read from env.py:

```python
base_path  # the path to save recordings
favorites  # shortcuts available via arrow up
elements_to_click_on_load  # ids of elements to click
server_host  # address the remote control listens on, default 127.0.0.1
server_port  # port of the remote control, default 8338
server_token  # access token, generated on every start if not set
```