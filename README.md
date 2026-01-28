# m3u82vlc

## usage

this is a curses based CLI to watch and record m3u8 streams.

## environment

the following options are available and will be read from env.py:

```python
base_path # the path to save recordings
favorites # shortcuts available via arrow up
proxy_log_file # optional log file for proxy debugging
elements_to_click_on_load # ids of elements to click
non_headless_mode_conditions # sites to use with GUI mode
```