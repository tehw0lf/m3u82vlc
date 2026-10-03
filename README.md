# m3u82vlc

## usage

this is a curses based CLI to watch and record m3u8 streams.

streams with separate audio and video playlists (e.g. LLHLS) are detected
automatically: the largest video variant is played and recorded together
with its audio track.

recordings run in the background and keep going when the player or the
terminal is closed. the panel at the top lists the running recordings.
press TAB at the URL prompt to select one, x to stop it and y to confirm.

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
non_headless_mode_conditions  # sites to use with GUI mode
```