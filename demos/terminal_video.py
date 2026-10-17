"""
MODULE:    i.ai demos
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Render terminal sessions to video, with an audio track.
SPDX-License-Identifier: Unlicense

A demo is a list of timed events (typed text, printed lines, a recording
indicator driven by a WAV file, pauses, inserted video clips). The events
are rendered frame by frame with PIL as a GRASS-style terminal, and ffmpeg
encodes the frames to H.264 with the WAV placed at the moment the recording
starts.
"""

import os
import shutil
import subprocess
import textwrap
import wave

from PIL import Image, ImageDraw, ImageFont

FPS = 25
WIDTH, HEIGHT = 1280, 720
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_SIZE = 17
BG = (24, 26, 33)
TITLE_BG = (44, 47, 58)
COLORS = {
    "text": (220, 223, 228),
    "prompt": (110, 200, 120),
    "ai": (100, 170, 240),
    "message": (170, 175, 185),
    "warning": (230, 180, 80),
    "rec": (235, 70, 70),
    "dim": (120, 125, 135),
}


def wav_levels(path, fps=FPS):
    """Return the RMS level (0-1) of a 16-bit mono WAV for each video frame."""
    with wave.open(path, "rb") as handle:
        rate = handle.getframerate()
        width = handle.getsampwidth()
        data = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError("Expected 16-bit samples")
    samples = memoryview(data).cast("h")
    step = rate // fps
    levels = []
    for start in range(0, len(samples), step):
        chunk = samples[start : start + step]
        if not len(chunk):
            break
        rms = (sum(s * s for s in chunk) / len(chunk)) ** 0.5
        levels.append(min(1.0, rms / 8000.0))
    return levels, len(samples) / rate


class Terminal:
    """A terminal screen: scrolling lines and a current input line."""

    def __init__(self, title):
        self.title = title
        self.font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
        self.bold = ImageFont.truetype(BOLD_PATH, FONT_SIZE)
        self.char_w = self.font.getlength("M")  # Advance width, monospace.
        self.line_h = FONT_SIZE + 6
        self.cols = int((WIDTH - 40) // self.char_w)
        self.rows = (HEIGHT - 60) // self.line_h
        self.lines = []  # (segments) with segments = [(text, color)]
        self.current = []  # Segments of the line being typed.
        self.cursor = True
        self.rec_level = None  # Recording indicator level, or None.

    def println(self, text="", color="text"):
        # Wrap at word boundaries; render() still cuts anything longer.
        for part in textwrap.wrap(text, self.cols) or [""]:
            self.lines.append([(part, color)])

    def commit(self):
        """End the line being typed."""
        self.lines.append(self.current)
        self.current = []

    def render(self, blink_on=True):
        image = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 0, WIDTH, 34], fill=TITLE_BG)
        for i, color in enumerate(((237, 106, 94), (245, 191, 79), (98, 197, 84))):
            draw.ellipse([14 + i * 22, 11, 26 + i * 22, 23], fill=color)
        draw.text((90, 7), self.title, font=self.bold, fill=COLORS["text"])
        rows = []
        for segments in self.lines + [self.current]:
            rows.extend(self._wrap(segments))
        visible = rows[-self.rows :]
        y = 46
        for segments in visible:
            x = 20
            for text, color in segments:
                draw.text((x, y), text, font=self.font, fill=COLORS[color])
                x += self.char_w * len(text)
            y += self.line_h
        if self.cursor and blink_on:
            last = visible[-1] if visible else []
            x = 20 + self.char_w * sum(len(t) for t, _ in last)
            y = 46 + self.line_h * (len(visible) - 1)
            draw.rectangle(
                [x, y + 2, x + self.char_w - 2, y + FONT_SIZE + 2], fill=COLORS["text"]
            )
        if self.rec_level is not None:
            self._draw_rec(draw)
        return image

    def _wrap(self, segments):
        """Split one logical line into screen rows of at most cols characters."""
        rows, row, used = [], [], 0
        for text, color in segments:
            while text:
                room = self.cols - used
                if room <= 0:
                    rows.append(row)
                    row, used = [], 0
                    continue
                row.append((text[:room], color))
                used += len(text[:room])
                text = text[room:]
        rows.append(row)
        return rows

    def _draw_rec(self, draw):
        x0, y0 = WIDTH - 330, HEIGHT - 60
        draw.rounded_rectangle([x0, y0, WIDTH - 20, y0 + 40], 8, fill=(48, 30, 34))
        draw.ellipse([x0 + 12, y0 + 12, x0 + 28, y0 + 28], fill=COLORS["rec"])
        draw.text(
            (x0 + 38, y0 + 9), "REC  microphone", font=self.bold, fill=COLORS["rec"]
        )
        bar = int(110 * self.rec_level)
        draw.rectangle([x0 + 195, y0 + 14, x0 + 305, y0 + 26], outline=COLORS["dim"])
        if bar:
            draw.rectangle(
                [x0 + 196, y0 + 15, x0 + 195 + bar, y0 + 25], fill=COLORS["prompt"]
            )


class Demo:
    """Timed events rendered to frames, encoded with ffmpeg."""

    def __init__(self, title, workdir=None):
        self.term = Terminal(title)
        self.workdir = workdir or os.getcwd()
        self.frames_dir = None
        self.count = 0
        self.audio = []  # (path, start seconds)
        self.clips = []  # (frame index, path of an inserted video)

    def __enter__(self):
        self.frames_dir = os.path.join(self.workdir, ".frames")
        shutil.rmtree(self.frames_dir, ignore_errors=True)
        os.makedirs(self.frames_dir)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.frames_dir, ignore_errors=True)

    @property
    def now(self):
        return self.count / FPS

    def frame(self, n=1):
        for _ in range(n):
            blink = (self.count // (FPS // 2)) % 2 == 0
            self.term.render(blink).save(
                os.path.join(self.frames_dir, "f{:06d}.png".format(self.count))
            )
            self.count += 1

    def pause(self, seconds):
        self.frame(int(seconds * FPS))

    def type(self, text, color="text", cps=28):
        """Type text on the current line at cps characters per second."""
        per_char = max(1, int(FPS / cps))
        for char in text:
            if self.term.current and self.term.current[-1][1] == color:
                self.term.current[-1] = (self.term.current[-1][0] + char, color)
            else:
                self.term.current.append((char, color))
            self.frame(per_char)

    def prompt(self, text, color="prompt"):
        self.term.current.append((text, color))
        self.frame()

    def enter(self, pause=0.3):
        self.term.commit()
        self.pause(pause)

    def say(self, text, color="message", pause=0.15):
        """Print a full line at once (program output)."""
        self.term.println(text, color)
        self.pause(pause)

    def record(self, wav):
        """Show the recording indicator while the WAV plays."""
        levels, duration = wav_levels(wav)
        self.audio.append((wav, self.now))
        for level in levels:
            self.term.rec_level = level
            self.frame()
        self.term.rec_level = None
        return duration

    def clip(self, path):
        """Insert a video clip (e.g. a captured window) at this point."""
        self.clips.append((self.count, path))

    def encode(self, output):
        """Encode the frames, clips and audio to one H.264 MP4 in a single pass.

        Every part (terminal frames, inserted clips) is decoded, brought to
        the same size, aspect ratio and frame rate, and joined with the
        concat filter before one encoder runs: a file joined by stream copy
        changes its H.264 parameters mid-stream, and many players then keep
        showing the first part.
        """
        parts = []
        cut = 0
        for index, path in self.clips + [(self.count, None)]:
            if index > cut:
                parts.append(("frames", cut, index))
            if path:
                parts.append(("clip", path))
            cut = index
        cmd = ["ffmpeg", "-v", "error", "-y"]
        for part in parts:
            if part[0] == "frames":
                cmd += [
                    "-framerate", str(FPS), "-start_number", str(part[1]),
                    "-i", os.path.join(self.frames_dir, "f%06d.png"),
                ]  # fmt: skip
            else:
                cmd += ["-i", part[1]]
        normalize = (
            "scale={w}:{h}:force_original_aspect_ratio=decrease,"
            "pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x181a21,setsar=1,fps={f},"
            "format=yuv420p".format(w=WIDTH, h=HEIGHT, f=FPS)
        )
        filters = []
        for n, part in enumerate(parts):
            trim = ""
            if part[0] == "frames":
                trim = "trim=end_frame={},setpts=PTS-STARTPTS,".format(
                    part[2] - part[1]
                )
            filters.append("[{n}:v]{t}{f}[v{n}]".format(n=n, t=trim, f=normalize))
        filters.append(
            "{}concat=n={}:v=1:a=0[vout]".format(
                "".join("[v{}]".format(n) for n in range(len(parts))), len(parts)
            )
        )
        # Each clip shifts the audio after it by its duration.
        labels = []
        for n, (wav, start) in enumerate(self.audio):
            shift = sum(
                probe_duration(p) for i, p in self.clips if i <= int(start * FPS)
            )
            cmd += ["-i", wav]
            delay = int((start + shift) * 1000)
            filters.append(
                "[{}:a]adelay={}|{}[a{}]".format(len(parts) + n, delay, delay, n)
            )
            labels.append("[a{}]".format(n))
        maps = ["-map", "[vout]"]
        if labels:
            filters.append(
                "{}amix=inputs={}:normalize=0[aout]".format(
                    "".join(labels), len(labels)
                )
            )
            maps += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
        cmd += ["-filter_complex", ";".join(filters), *maps]
        cmd += [
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", output,
        ]  # fmt: skip
        subprocess.run(cmd, check=True)
        return output


def probe_duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())
