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
| `POST /api/links/<id>/retry` | detect and record a failed link again |
| `DELETE /api/links/<id>` | remove a link from the list |
| `DELETE /api/recordings/<pid>` | stop a recording |

the connection is plain HTTP by default, so anyone in the network can read
the token. to serve HTTPS instead, create a self-signed certificate for the
address the server listens on and point `server_cert` and `server_key` in
env.py to it:

```bash
mkdir -p ~/.config/m3u82vlc && cd ~/.config/m3u82vlc
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -days 825 -keyout key.pem -out cert.pem -subj "/CN=m3u82vlc" \
  -addext "subjectAltName=IP:192.168.1.10"
chmod 600 key.pem
```

a browser warns about such a certificate once, as nobody vouches for it.
accepting the warning keeps the token from being read along, but does not
tell the server apart from someone posing as it. for that, install
cert.pem as a trusted certificate on the device.

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
server_cert  # certificate to serve HTTPS with, HTTP if not set
server_key  # private key of the certificate
server_token  # access token, generated on every start if not set
```