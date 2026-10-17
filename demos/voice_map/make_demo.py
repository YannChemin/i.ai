#!/usr/bin/env python3
"""
MODULE:    i.ai demos
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Demo video: a spoken request drawn by i.ai in a GRASS map display.
SPDX-License-Identifier: Unlicense

The video shows a GRASS terminal: i.ai starts with voice input, the WAV
recording plays while the recording indicator follows its level, the
transcription appears on the i.ai prompt, i.ai proposes and runs a plan
(d.mon start=wx0, d.rast, d.legend, d.northarrow, d.barscale, sleep 3,
d.mon stop=wx0), and the real wx0 map display, recorded while the plan ran,
is shown in place.

Everything shown is real: the WAV is transcribed by whisper.cpp on the GPU
server, i.ai plans and runs the commands, and the wx0 window is captured
from the screen with ffmpeg (x11grab, the window forced onto X11 through
GDK_BACKEND=x11, as GNOME on Wayland does not let scripts record the whole
screen). The transcription, plan and step log are saved to capture.json and
the window to wx0.mp4; --reuse renders the video again without new calls.

Run inside a throwaway mapset of the NC sample dataset (the plan draws in
it and the demo sets its region to the elevation map), e.g.:

    grass --tmp-mapset ~/grassdata/nc_spm_08_grass7 --exec \\
        python3 demos/voice_map/make_demo.py

Output: demos/voice_map/voice_map.mp4
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(HERE))

from terminal_video import Demo  # noqa: E402

WAV = os.path.join(ROOT, "tests", "data", "voice_elevation_map.wav")
CAPTURE = os.path.join(HERE, "capture.json")
WINDOW_CLIP = os.path.join(HERE, "wx0.mp4")
OUTPUT = os.path.join(HERE, "voice_map.mp4")


class WindowRecorder(threading.Thread):
    """Wait for the wx0 window, then record it until it closes."""

    def __init__(self, name="^wx0$", output=WINDOW_CLIP, wait=90):
        super().__init__(daemon=True)
        self.name, self.output, self.wait = name, output, wait
        self.started = None
        self.error = None

    def run(self):
        window = None
        deadline = time.time() + self.wait
        while time.time() < deadline and not window:
            found = subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--name", self.name],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.split()
            window = found[0] if found else None
            time.sleep(0.2)
        if not window:
            self.error = "the {} window never appeared".format(self.name)
            return
        time.sleep(0.5)  # Let the window draw its first frame.
        self.started = time.time()
        # ffmpeg stops with an error when the window closes; that ends the clip.
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "x11grab",
                "-draw_mouse",
                "0",
                "-window_id",
                window,
                "-framerate",
                "25",
                "-i",
                os.environ.get("DISPLAY", ":0"),
                "-t",
                "60",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                self.output,
            ],  # fmt: skip
            capture_output=True,
            check=False,
        )


def capture(args):
    """Transcribe the WAV on the GPU server, then let i.ai plan and run it."""
    import grass.script as gs
    from iai_voice import WhisperServer, vocabulary

    # The demo's own throwaway mapset works on the area of the elevation map.
    gs.run_command("g.region", raster="elevation")
    server = WhisperServer(args.language, args.host, args.user)
    try:
        gpu = server.start()
        text = server.transcribe(args.wav, vocabulary(["elevation", "elevation_shade"]))
    finally:
        server.stop()
    print("Transcription: " + text, file=sys.stderr)
    recorder = WindowRecorder()
    recorder.start()
    env = dict(os.environ, GDK_BACKEND="x11")
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "i.ai.py"),
            "query=" + text,
            "backend=" + args.backend,
            "model=" + args.model,
            "format=json",
            "-e",
        ],  # fmt: skip
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    recorder.join(timeout=70)
    if recorder.error or not os.path.exists(WINDOW_CLIP):
        sys.exit(
            "No window recorded: {}\n{}".format(recorder.error, result.stderr[-2000:])
        )
    answer = json.loads(result.stdout[result.stdout.index("{") :])
    # GRASS prints its progress on the same line, e.g. "   0%Step 1/7: ...".
    steps = re.findall(r"Step \d+/\d+: .*", result.stderr)
    data = {
        "date": datetime.datetime.now().isoformat(timespec="seconds"),
        "server": "{}@{}".format(args.user, args.show_host),
        "gpu": gpu,
        "transcription": text,
        "intent": answer.get("intent", {}).get("intent"),
        "answer": answer.get("text", ""),
        "plan": answer.get("plan", []),
        "steps": steps,
        "exit": result.returncode,
    }
    with open(CAPTURE, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)
    return data


def shell_line(item):
    if item.get("shell"):
        return item["shell"]
    parts = [item["module"]]
    if item.get("flags"):
        parts.append("-" + item["flags"])
    parts += ["{}={}".format(k, v) for k, v in item.get("params", {}).items()]
    return " ".join(parts)


def render(data, wav):
    server = data["server"]
    host, user = server.split("@")[1], server.split("@")[0]
    prompt = "GRASS nc_spm_08_grass7/demo:~ > "
    with Demo("GRASS 8.6 - nc_spm_08_grass7 - terminal", workdir=HERE) as demo:
        demo.prompt(prompt)
        demo.pause(0.6)
        demo.type("g.region raster=elevation")
        demo.enter(0.4)
        demo.prompt(prompt)
        demo.pause(0.5)
        demo.type(
            "i.ai backend=claude -i -e input=voice voice_host={} voice_user={}".format(
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
        for line in data["answer"].splitlines():
            demo.say(line, "ai", pause=0.1)
        demo.say("", pause=0.1)
        demo.say("Plan:", "text", pause=0.2)
        for n, item in enumerate(data["plan"], 1):
            if item.get("purpose"):
                demo.say("  # " + item["purpose"], "dim", pause=0.05)
            demo.say("  {}. {}".format(n, shell_line(item)), "text", pause=0.25)
        demo.pause(0.8)
        demo.prompt("Run this plan? [y/N] ", "warning")
        demo.pause(0.6)
        demo.type("y")
        demo.enter(0.3)
        # The messages i.ai printed, or the same lines rebuilt from the plan.
        steps = data["steps"] or [
            "Step {}/{}: {}...".format(n, len(data["plan"]), shell_line(item))
            for n, item in enumerate(data["plan"], 1)
        ]
        # The window is shown after the steps that draw it, before the last one.
        for line in steps[:-1]:
            demo.say(line, "message", pause=0.35)
        demo.pause(0.5)
        demo.clip(WINDOW_CLIP)
        for line in steps[-1:]:
            demo.say(line, "message", pause=0.4)
        demo.prompt("i.ai> ", "ai")
        demo.pause(1.0)
        demo.type("/exit")
        demo.enter(0.3)
        demo.prompt(prompt)
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
