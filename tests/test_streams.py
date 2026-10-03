import streams
from streams import SplitStream

BASE = "https://cdn.example/app/stream/"

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="und",DEFAULT=YES,URI="chunklist_4_audio_AbC_llhls.m3u8?session=9"
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360,AUDIO="a1"
chunklist_0_video_AbC_llhls.m3u8?session=9
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,AUDIO="a1"
chunklist_3_video_AbC_llhls.m3u8?session=9
"""

MUXED_MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
high.m3u8
"""


def test_playlist_url_maps_init_segment_to_chunklist():
    url = BASE + "init_7_audio_AbC_llhls.m4s?session=9"
    expected = BASE + "chunklist_7_audio_AbC_llhls.m3u8?session=9"
    assert streams.playlist_url(url) == expected


def test_playlist_url_drops_blocking_reload_parameters():
    url = BASE + "chunklist_2_video_AbC_llhls.m3u8?session=9&_HLS_msn=10"
    expected = BASE + "chunklist_2_video_AbC_llhls.m3u8?session=9"
    assert streams.playlist_url(url) == expected


def test_playlist_url_ignores_other_requests():
    assert (
        streams.playlist_url(BASE + "part_2_10_1_video_AbC_llhls.m4s") is None
    )
    assert streams.playlist_url("https://cdn.example/app.js") is None


def test_select_stream_prefers_master_playlist():
    urls = [
        BASE + "audio.m3u8",
        BASE + "llhls.m3u8?token=a",
        BASE + "video.m3u8",
    ]
    assert streams.stream_complete(urls)
    assert streams.select_stream(urls) == BASE + "llhls.m3u8?token=a"


def test_select_stream_pairs_audio_and_video_without_master():
    urls = [
        BASE + "audio.m3u8",
        BASE + "video_720.m3u8",
        BASE + "video_1080.m3u8",
    ]
    assert streams.stream_complete(urls)
    assert streams.select_stream(urls) == SplitStream(
        video_url=BASE + "video_720.m3u8", audio_url=BASE + "audio.m3u8"
    )


def test_select_stream_falls_back_to_first_url():
    urls = [BASE + "audio.m3u8"]
    assert not streams.stream_complete(urls)
    assert streams.select_stream(urls) == BASE + "audio.m3u8"
    assert streams.select_stream([]) is None


def test_find_split_stream_takes_largest_variant():
    stream = streams.find_split_stream(BASE + "llhls.m3u8", {}, MASTER)
    assert stream == SplitStream(
        video_url=BASE + "chunklist_3_video_AbC_llhls.m3u8?session=9",
        audio_url=BASE + "chunklist_4_audio_AbC_llhls.m3u8?session=9",
        resolution="1920x1080",
    )


def test_find_split_stream_ignores_muxed_playlist():
    assert streams.find_split_stream(BASE + "x.m3u8", {}, MUXED_MASTER) is None


def test_start_recording_command(monkeypatch):
    commands = []
    monkeypatch.setattr(
        streams.subprocess,
        "Popen",
        lambda command, **_: commands.append(command),
    )
    headers = {"User-Agent": "UA", "Referer": "https://site.example/"}
    streams.start_recording(
        SplitStream("v.m3u8", "a.m3u8"), "/out.ts", headers
    )
    streams.start_recording("master.m3u8", "/out.ts", {})

    split, single = commands
    assert split[:2] == ["nohup", "ffmpeg"]
    assert "-copyts" in split
    assert split.count("-i") == 2 and split.count("-user_agent") == 2
    assert "Referer: https://site.example/\r\n" in split
    assert split[-3:] == ["-f", "mpegts", "/out.ts"]
    assert single.count("-i") == 1 and "-copyts" not in single
