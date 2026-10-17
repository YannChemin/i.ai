"""Tests of voice input: recorder, GPU search, transcript cleaning.

No microphone is recorded. The GPU search is tested on canned probe
output. A live test against a GPU server runs only when IAI_TEST_VOICE_HOST
(and optionally IAI_TEST_VOICE_USER and IAI_TEST_VOICE_WAV) are set.
"""

import glob
import os
import re

import pytest

from iai_voice import (
    Recorder,
    Target,
    VoiceError,
    WhisperServer,
    clean_text,
    gpu_search,
    vocabulary,
)

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
# Spoken fixtures: tests/data/<name>.wav with the sentence read in <name>.txt
# (recorded with tests/record_voice_fixture.py).
FIXTURES = sorted(
    p for p in glob.glob(os.path.join(DATA, "*.wav")) if os.path.exists(p[:-4] + ".txt")
)

PROBE_GPU = """GPU_PCI 06:00.0 VGA compatible controller: AMD Radeon Pro WX 7100
GPU_DEV /dev/dri/renderD128
WHISPER /opt/w/whisper-server|ggml-vulkan
MODEL /opt/w/ggml-large-v3.bin
"""


class CannedTarget(Target):
    def __init__(self, output, code=0, remote=True):
        super().__init__("gpu.example" if remote else None, "me" if remote else None)
        self.output, self.code = output, code

    def run(self, script, timeout=60):
        return self.code, self.output


def test_recorder_commands():
    recorder = Recorder()
    recorder.tool = "pw-record"
    assert recorder.command("a.wav")[:7] == [
        "pw-record", "--rate", "16000", "--channels", "1", "--format", "s16",
    ]  # fmt: skip
    recorder.tool, recorder.device = "arecord", "hw:1"
    command = recorder.command("a.wav")
    assert "-D" in command and "S16_LE" in command and command[-1] == "a.wav"


def test_gpu_search_finds_vulkan_build():
    found = gpu_search(CannedTarget(PROBE_GPU))
    assert found["binary"] == "/opt/w/whisper-server"
    assert found["backends"] == ["ggml-vulkan"]
    assert found["model"].endswith("ggml-large-v3.bin")
    assert found["target"] == "me@gpu.example"


@pytest.mark.parametrize(
    "output, code, message",
    [
        ("", 255, "Cannot reach"),
        ("WHISPER /w/whisper-server|\nMODEL /m.bin\n", 0, "No GPU"),
        (
            "GPU_DEV /dev/dri/renderD128\nWHISPER /w/whisper-server|\n",
            0,
            "only CPU builds",
        ),
        (
            "GPU_DEV /dev/dri/renderD128\nWHISPER /w/s|ggml-cuda\n",
            0,
            "No ggml-large-v3",
        ),
    ],
)
def test_gpu_search_never_falls_back_to_cpu(output, code, message):
    with pytest.raises(VoiceError, match=message):
        gpu_search(CannedTarget(output, code))


def test_target_names():
    assert str(Target("192.0.2.10", "yann")) == "yann@192.0.2.10"
    assert Target("192.0.2.10").ssh_target == "192.0.2.10"
    assert str(Target()).startswith("this machine")


def test_clean_text():
    assert (
        clean_text(" [BLANK_AUDIO] compute  the slope [MUSIC] ") == "compute the slope"
    )


@pytest.mark.skipif(
    not os.environ.get("IAI_TEST_VOICE_HOST"), reason="no GPU server given"
)
def test_live_transcription():
    wav = os.environ.get("IAI_TEST_VOICE_WAV")
    if not wav:
        pytest.skip("IAI_TEST_VOICE_WAV not set")
    server = WhisperServer(
        "auto", os.environ["IAI_TEST_VOICE_HOST"], os.environ.get("IAI_TEST_VOICE_USER")
    )
    try:
        assert server.start()
        assert server.transcribe(wav)
    finally:
        server.stop()


def test_read_request_flow(monkeypatch):
    import builtins
    import importlib.util

    from conftest import ADDON_DIR

    spec = importlib.util.spec_from_file_location(
        "i_ai_main", os.path.join(ADDON_DIR, "i.ai.py")
    )
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)

    class FakeVoice:
        def listen(self, wait_start=None, wait_stop=input):
            wait_stop()  # Enter stops the recording.
            return "compute the slope of elevation"

    prompts = []
    answers = iter(["", "", "compute the slope of elevation"])

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(builtins, "input", fake_input)
    assert main.read_request(FakeVoice()) == "compute the slope of elevation"
    assert "[Enter: speak]" in prompts[0]
    # A typed line goes through without recording.
    answers = iter(["/plan"])
    assert main.read_request(FakeVoice()) == "/plan"


def test_vocabulary_prompt():
    prompt = vocabulary(["elevation", "lsat7_2002_40"])
    assert prompt.startswith("GRASS GIS") and "lsat7_2002_40" in prompt
    assert len(vocabulary(["x" * 50] * 100)) <= 801


def words(text):
    return re.findall(r"\w+", text.lower())


@pytest.fixture(scope="module")
def gpu_server():
    if not os.environ.get("IAI_TEST_VOICE_HOST"):
        pytest.skip("no GPU server given (IAI_TEST_VOICE_HOST)")
    server = WhisperServer(
        "auto", os.environ["IAI_TEST_VOICE_HOST"], os.environ.get("IAI_TEST_VOICE_USER")
    )
    server.start()
    yield server
    server.stop()


@pytest.mark.parametrize(
    "wav", FIXTURES, ids=[os.path.basename(p)[:-4] for p in FIXTURES]
)
def test_spoken_fixture(gpu_server, wav):
    """The recorded sentence comes back with at most one word in five wrong."""
    with open(wav[:-4] + ".txt", encoding="utf-8") as handle:
        expected = words(handle.read())
    # The spoken language: <name>.lang when present, else English. Automatic
    # detection is not reliable with an accent.
    language = "en"
    if os.path.exists(wav[:-4] + ".lang"):
        with open(wav[:-4] + ".lang", encoding="utf-8") as handle:
            language = handle.read().strip()
    heard = words(
        gpu_server.transcribe(
            wav, vocabulary(["elevation", "elevation_shade"]), language=language
        )
    )
    assert heard, "nothing transcribed: is the recording silent?"
    matched = sum(1 for w in expected if w in heard)
    assert matched >= 0.8 * len(expected), "heard: " + " ".join(heard)
