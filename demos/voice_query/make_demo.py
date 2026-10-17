#!/usr/bin/env python3
"""
MODULE:    i.ai demos
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Demo video: a spoken question answered by i.ai in the terminal.
SPDX-License-Identifier: Unlicense

The video shows a GRASS terminal: i.ai starts with voice input, the WAV
recording plays while the recording indicator follows its level, the
transcription appears on the i.ai prompt, then i.ai's answer.

Everything shown is real: the WAV is transcribed by whisper.cpp on the GPU
server, and the answer comes from i.ai run on the transcription. Both are
saved to capture.json; --reuse renders the video again from it without new
calls.

Run inside a GRASS session of the NC sample dataset, e.g.:

    grass --tmp-mapset ~/grassdata/nc_spm_08_grass7 --exec \\
        python3 demos/voice_query/make_demo.py

Output: demos/voice_query/voice_query.mp4
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(HERE))

from terminal_video import Demo  # noqa: E402

WAV = os.path.join(ROOT, "tests", "data", "voice_mean_elevation.wav")
CAPTURE = os.path.join(HERE, "capture.json")
OUTPUT = os.path.join(HERE, "voice_query.mp4")


def capture(args):
    """Transcribe the WAV on the GPU server and ask i.ai the question."""
    from iai_voice import WhisperServer, vocabulary

    server = WhisperServer(args.language, args.host, args.user)
    try:
        gpu = server.start()
        text = server.transcribe(args.wav, vocabulary(["elevation", "elevation_shade"]))
    finally:
        server.stop()
    print("Transcription: " + text, file=sys.stderr)
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "i.ai.py"),
            "query=" + text,
            "backend=" + args.backend,
            "model=" + args.model,
            "format=json",
            "--quiet",
        ],  # fmt: skip
        capture_output=True,
        text=True,
        check=True,
    )
    answer = json.loads(result.stdout)
    data = {
        "date": datetime.datetime.now().isoformat(timespec="seconds"),
        "server": "{}@{}".format(args.user, args.show_host),
        "gpu": gpu,
        "transcription": text,
        "intent": answer.get("intent", {}).get("intent"),
        "answer": answer.get("text", ""),
    }
    with open(CAPTURE, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)
    return data


def render(data, wav):
    server = data["server"]
    host, user = server.split("@")[1], server.split("@")[0]
    with Demo("GRASS 8.6 - nc_spm_08_grass7 - terminal", workdir=HERE) as demo:
        demo.prompt("GRASS nc_spm_08_grass7/PERMANENT:~ > ")
        demo.pause(0.8)
        demo.type(
            "i.ai backend=claude -i input=voice voice_host={} voice_user={}".format(
                host, user
            )
        )
        demo.enter(0.5)
        demo.say("Searching for a GPU on {}...".format(server), pause=1.2)
        demo.say(
            "Voice input ready: {}, recording with pw-record.".format(data["gpu"]),
            pause=0.4,
        )
        demo.say(
            "i.ai interactive dialog. Commands: /run (execute the last plan), /plan, "
            "/context, /reset, /exit.",
            pause=0.8,
        )
        demo.prompt("i.ai> ", "ai")
        demo.prompt("[Enter: speak] ", "dim")
        demo.pause(1.0)
        demo.enter(0.2)
        demo.say("Recording... press Enter to stop.", "warning", pause=0.1)
        demo.record(wav)
        demo.say("", pause=0.6)
        demo.prompt("i.ai> ", "ai")
        demo.type(data["transcription"], cps=40)
        demo.pause(1.0)
        demo.enter(0.3)
        for n in range(6):
            demo.term.current = [("thinking" + "." * (n % 4), "dim")]
            demo.pause(0.35)
        demo.term.current = []
        for line in data["answer"].splitlines() or [""]:
            demo.say(line, "ai", pause=0.12)
        demo.pause(1.0)
        demo.prompt("i.ai> ", "ai")
        demo.pause(1.0)
        demo.type("/exit")
        demo.enter(0.3)
        demo.prompt("GRASS nc_spm_08_grass7/PERMANENT:~ > ")
        demo.pause(2.0)
        return demo.encode(OUTPUT)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--wav", default=WAV)
    parser.add_argument(
        "--host",
        default=os.environ.get("IAI_VOICE_HOST"),
        help="GPU server (or IAI_VOICE_HOST)",
    )
    parser.add_argument("--user", default=os.environ.get("IAI_VOICE_USER", "yann"))
    parser.add_argument(
        "--show-host",
        default="192.0.2.10",
        help="address shown in the video and capture.json instead of the real one "
        "(default: a documentation address, RFC 5737)",
    )
    parser.add_argument("--language", default="en")
    parser.add_argument("--backend", default="claude")
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--reuse", action="store_true", help="render from capture.json")
    args = parser.parse_args()
    if not args.reuse and not args.host:
        parser.error("give the GPU server with --host or IAI_VOICE_HOST")
    if args.reuse:
        with open(CAPTURE, encoding="utf-8") as handle:
            data = json.load(handle)
    else:
        data = capture(args)
    print("Video: " + render(data, args.wav), file=sys.stderr)


if __name__ == "__main__":
    main()
