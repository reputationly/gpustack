"""语音产物扩展名契约：命名者（门面）与编码者（引擎）必须同源。

历史问题：``_output_ext`` 对音频固定返回 ``.wav``，而引擎按请求的 ``response_format``
编码。两边一旦分歧，产出就是"mp3 字节装进 .wav 文件"，网关按扩展名回 ``audio/wav``，
客户端按 WAV 解一份 MP3 —— 失败得又晚又难查。所以这里钉住三件事：

1. 语音产物扩展名跟着 ``response_format`` 走，且**只能**是引擎认得的容器；
2. 认不出的容器当场 400，不悄悄回落到 ``.wav``（回落等于把上面那个 bug 引回来）；
3. 其它 kind（image/video/music/audiogen）行为不变 —— 改语音不能连带改视频。
"""

import pytest

from gpustack.api.exceptions import BadRequestException
from gpustack.routes.videos import (
    _AUDIO_OUTPUT_EXTS,
    _output_content_type,
    _output_ext,
    _requested_output_format,
)


def test_tts_defaults_to_wav_when_format_absent():
    # 不传 response_format = 交给引擎默认（wav），门面不能自作主张换容器。
    assert _output_ext("tts") == ".wav"
    assert _output_ext("tts", "") == ".wav"


@pytest.mark.parametrize("fmt", sorted(_AUDIO_OUTPUT_EXTS))
def test_tts_honours_requested_container(fmt):
    assert _output_ext("tts", fmt) == _AUDIO_OUTPUT_EXTS[fmt]


def test_tts_rejects_container_the_engine_cannot_encode():
    # aac/m4a/ogg 曾是网关侧白名单里的项，但引擎（vLLM-Omni）不认。
    for bad in ("aac", "m4a", "ogg", "midi"):
        with pytest.raises(BadRequestException) as exc:
            _output_ext("tts", bad)
        assert bad in exc.value.message


def test_audiogen_is_wav_only():
    # t2a/v2m/tv2m/svs 走引擎的 chat 路径回 base64 WAV，没有容器开关。
    assert _output_ext("svs") == ".wav"
    assert _output_ext("svs", "wav") == ".wav"
    with pytest.raises(BadRequestException):
        _output_ext("svs", "mp3")


def test_non_audio_kinds_unchanged():
    assert _output_ext("t2i") == ".png"
    assert _output_ext("t2v") == ".mp4"
    assert _output_ext("i2v") == ".mp4"
    # music 固定 .mp3 是既有契约，本轮不动；请求里带 response_format 也不该改写它。
    assert _output_ext("t2m") == ".mp3"
    assert _output_ext("t2m", "wav") == ".mp3"


def test_requested_output_format_normalizes():
    assert _requested_output_format({"response_format": " MP3 "}) == "mp3"
    assert _requested_output_format({}) == ""
    assert _requested_output_format({"response_format": None}) == ""
    assert _requested_output_format({"response_format": 123}) == ""


@pytest.mark.parametrize("fmt,ext", sorted(_AUDIO_OUTPUT_EXTS.items()))
def test_every_audio_ext_has_content_type(fmt, ext):
    # /videos/{id}/content 直接回源时用这张表；Starlette 按扩展名猜的表里
    # 没有 .opus/.pcm/.flac，猜不出来会退成 text/plain，浏览器不会当音频播。
    assert _output_content_type("task" + ext) != "application/octet-stream"


def test_unknown_ext_degrades_to_octet_stream():
    assert _output_content_type("task.bin") == "application/octet-stream"


def test_normalised_format_is_what_reaches_the_engine():
    """The name we pick and the value we forward must be the same string.

    Forwarding the raw body value while naming the artifact from the normalised one
    is how " MP3 " produced a ``.mp3`` path and an engine-side 422, and how an empty
    string produced a ``.wav`` path with an invalid ``response_format``.
    """
    from gpustack.routes.videos import _apply_output_format, _requested_output_format

    for raw, expected in (
        ("  MP3 ", "mp3"),
        ("FLAC", "flac"),
        ("", ""),
        (None, ""),
        (123, ""),
    ):
        body = {"response_format": raw}
        normalised = _requested_output_format(body)
        assert normalised == expected
        _apply_output_format(body, normalised)
        if expected:
            assert body["response_format"] == expected
        else:
            # 空/非法值不能原样下发:引擎的 response_format 是 Literal
            assert "response_format" not in body
