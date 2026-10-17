"""
MODULE:    i.ai
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Voice input for i.ai: microphone recording, GPU transcription.
SPDX-License-Identifier: Unlicense

Speech is recorded from the PC microphone with PipeWire (pw-record, the
Debian 13 default; arecord as a fallback) as 16 kHz mono WAV, and
transcribed by whisper.cpp large-v3 on a GPU, never on a CPU.

The GPU machine is this one by default, or a GPU server given by its
address and login name (voice_host, voice_user). A GPU search runs there
first: GPU hardware (lspci, /dev/dri, nvidia-smi), a whisper-server built
with a GPU backend (Vulkan, CUDA, HIP, OpenCL, SYCL...), and the model.
Then whisper-server starts with the model kept in GPU memory (through an
ssh tunnel for a server) and must report its GPU backend
("whisper_backend_init_gpu: using ...") or voice input stops. Each
utterance is sent to it and nothing is written on the server. The server
process is stopped when i.ai exits.

Settings (environment), used by the GPU search when set:
    IAI_WHISPER_BIN    whisper-server path on the GPU machine
    IAI_WHISPER_MODEL  model path on the GPU machine
"""

import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

SSH_OPTS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=30",
]
GPU_LINE = re.compile(r"whisper_backend_init_gpu: using (.+)")


class VoiceError(RuntimeError):
    """Voice input cannot work; the message says what to fix."""


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Recorder:
    """Record the microphone to a 16 kHz mono WAV file until stopped."""

    def __init__(self, device=None):
        self.device = device
        if shutil.which("pw-record"):
            self.tool = "pw-record"
        elif shutil.which("arecord"):
            self.tool = "arecord"
        else:
            raise VoiceError(
                "No recorder found: install pipewire-bin (pw-record) or alsa-utils (arecord)"
            )
        self.process = None
        self.path = None

    def command(self, path):
        if self.tool == "pw-record":
            cmd = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16"]
            if self.device:
                cmd += ["--target", self.device]
            return cmd + [path]
        cmd = ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "wav"]
        if self.device:
            cmd += ["-D", self.device]
        return cmd + [path]

    def start(self, directory):
        self.path = os.path.join(
            directory, "utterance-{}.wav".format(uuid.uuid4().hex[:8])
        )
        self.process = subprocess.Popen(
            self.command(self.path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.2)
        if self.process.poll() is not None:
            error = self.process.stderr.read().decode("utf-8", "replace").strip()
            raise VoiceError(
                "{} could not record: {}".format(self.tool, error or "no detail")
            )

    def stop(self):
        """Stop recording; return the WAV path and its duration in seconds."""
        if self.process is None:
            return None, 0.0
        # SIGINT lets both recorders finish the WAV header.
        self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process = None
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return self.path, max(0, size - 44) / 32000.0  # 16 kHz, 16-bit, mono.


GPU_PROBE = r"""
lspci 2>/dev/null | grep -iE 'vga|3d controller|display' | sed 's/^/GPU_PCI /'
for d in /dev/dri/renderD* /dev/nvidia0 /dev/kfd; do [ -e "$d" ] && echo "GPU_DEV $d"; done
command -v nvidia-smi >/dev/null && nvidia-smi -L 2>/dev/null | sed 's/^/GPU_NV /'
for p in "${IAI_WHISPER_BIN:-}" "$(command -v whisper-server 2>/dev/null)" \
         ~/dev/whisper.cpp/build/bin/whisper-server ~/whisper.cpp/build/bin/whisper-server \
         $(find ~ -maxdepth 6 -type f -name whisper-server -perm -u+x 2>/dev/null | head -5); do
    [ -n "$p" ] && [ -x "$p" ] || continue
    backends=$( { ldd "$p" 2>/dev/null; ls "$(dirname "$p")" 2>/dev/null; } \
        | grep -oiE 'ggml-(vulkan|cuda|hip|opencl|sycl|metal|musa)' | sort -u | tr '\n' ' ')
    echo "WHISPER $p|$backends"
done
for m in "${IAI_WHISPER_MODEL:-}" ~/dev/whisper.cpp/models/ggml-large-v3.bin \
         $(find ~ -maxdepth 6 -type f -name 'ggml-large-v3*.bin' 2>/dev/null | head -5); do
    [ -n "$m" ] && [ -s "$m" ] && echo "MODEL $m"
done
"""


class Target:
    """The machine that runs whisper: this one, or a GPU server over ssh."""

    def __init__(self, host=None, user=None):
        self.host = host or None
        self.user = user or None
        self.remote = bool(self.host)

    def __str__(self):
        if not self.remote:
            return "this machine ({})".format(socket.gethostname())
        return "{}@{}".format(self.user, self.host) if self.user else self.host

    @property
    def ssh_target(self):
        return "{}@{}".format(self.user, self.host) if self.user else self.host

    def run(self, script, timeout=60):
        """Run a bash script on the target; return (exit code, output)."""
        if self.remote:
            cmd = ["ssh", *SSH_OPTS, self.ssh_target, "bash -s"]
        else:
            cmd = ["bash", "-s"]
        env = dict(os.environ)
        env.pop("LD_PRELOAD", None)
        try:
            result = subprocess.run(
                cmd, input=script, capture_output=True, text=True,
                timeout=timeout, env=env, check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        return result.returncode, result.stdout + result.stderr


def gpu_search(target):
    """Find GPUs and a GPU-enabled whisper.cpp build with a model on a target.

    :return: dict with gpus, binary, backends, model
    :raises VoiceError: when the target is unreachable, has no GPU, or no
        GPU-enabled whisper build or model; voice input never uses the CPU
    """
    code, output = target.run(GPU_PROBE)
    if target.remote and code == 255:
        raise VoiceError(
            "Cannot reach the GPU server {} over ssh (host up, link up, key-based login "
            "for this user?)".format(target)
        )
    gpus, devices, builds, models = [], [], [], []
    for line in output.splitlines():
        kind, _, value = line.partition(" ")
        if kind == "GPU_PCI":
            gpus.append(re.sub(r"^\S+\s+", "", value).strip())
        elif kind == "GPU_NV":
            gpus.append(value.strip())
        elif kind == "GPU_DEV":
            devices.append(value.strip())
        elif kind == "WHISPER":
            path, _, backends = value.partition("|")
            builds.append((path, backends.split()))
        elif kind == "MODEL" and value not in models:
            models.append(value.strip())
    found = {"target": str(target), "gpus": gpus, "devices": devices}
    if not gpus and not devices:
        raise VoiceError(
            "No GPU found on {}; voice input never runs on the CPU".format(target)
        )
    gpu_builds = [(p, b) for p, b in builds if b]
    if not gpu_builds:
        detail = (
            "only CPU builds of whisper-server ({})".format(
                ", ".join(p for p, _ in builds)
            )
            if builds
            else "no whisper-server"
        )
        raise VoiceError(
            "GPU found on {} ({}) but {}. Build whisper.cpp with a GPU backend there "
            "(e.g. cmake -B build -DGGML_VULKAN=ON), or give voice_host= of a GPU "
            "server.".format(target, "; ".join(gpus) or ", ".join(devices), detail)
        )
    if not models:
        raise VoiceError(
            "No ggml-large-v3 model on {} (models/download-ggml-model.sh large-v3, or set "
            "IAI_WHISPER_MODEL)".format(target)
        )
    found["binary"], found["backends"] = gpu_builds[0][0], gpu_builds[0][1]
    found["model"] = models[0]
    return found


class WhisperServer:
    """whisper-server on a GPU machine: this one, or a server over an ssh tunnel."""

    def __init__(self, language="auto", host=None, user=None, threads=4):
        self.language = language
        self.target = Target(host, user)
        self.threads = threads
        self.process = None
        self.remote_pid = None
        self.local_port = None
        self.gpu = None
        self.search = None
        self._lines = []

    @property
    def remote(self):
        return str(self.target)

    def start(self, timeout=180):
        """Find the GPU, start the server and wait until the model is loaded."""
        self.search = gpu_search(self.target)
        self.local_port = _free_port()
        port = random.randint(20000, 29999) if self.target.remote else self.local_port
        server_cmd = "exec $(command -v stdbuf >/dev/null && echo stdbuf -oL -eL) {bin} -m {model} -l {lang} -t {threads} --host 127.0.0.1 --port {port}".format(
            bin=self.search["binary"],
            model=self.search["model"],
            lang=self.language,
            threads=self.threads,
            port=port,
        )
        env = dict(os.environ)
        env.pop("LD_PRELOAD", None)
        if self.target.remote:
            cmd = [
                "ssh", *SSH_OPTS, "-o", "ExitOnForwardFailure=yes",
                "-L", "127.0.0.1:{}:127.0.0.1:{}".format(self.local_port, port),
                self.target.ssh_target, "echo IAI_PID $$; " + server_cmd,
            ]  # fmt: skip
        else:
            cmd = ["bash", "-c", server_cmd]
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        ended = threading.Event()
        reader = threading.Thread(target=self._read, args=(ended,), daemon=True)
        reader.start()
        # The server answers HTTP once the model is loaded; its own "listening"
        # line can sit in a stdout buffer, so the port is polled instead.
        deadline = time.time() + timeout
        while not self._answers():
            if ended.is_set() or self.process.poll() is not None:
                break
            if time.time() > deadline:
                self.stop()
                raise VoiceError(
                    "whisper-server did not start within {} s".format(timeout)
                )
            time.sleep(1)
        if self.process.poll() is not None:
            tail = "\n".join(self._lines[-8:])
            self.stop()
            raise VoiceError(
                "whisper-server stopped on {}:\n{}".format(self.target, tail)
            )
        if not self.gpu:
            self.stop()
            raise VoiceError(
                "whisper-server on {} did not start on the GPU (no "
                "'whisper_backend_init_gpu' line); voice input never runs on the CPU. "
                "Check the GPU driver there, or reboot it.".format(self.target)
            )
        return self.gpu

    def _answers(self):
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:{}/".format(self.local_port), timeout=2
            ) as response:
                return response.status < 500
        except urllib.error.HTTPError as error:
            return error.code < 500
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _read(self, ended):
        for line in self.process.stdout:
            line = line.rstrip()
            self._lines.append(line)
            self._lines = self._lines[-50:]
            if line.startswith("IAI_PID "):
                self.remote_pid = line.split()[1]
            match = GPU_LINE.search(line)
            if match:
                self.gpu = match.group(1).strip()
        ended.set()  # The process ended: start() reports why.

    def transcribe(self, wav_path, timeout=120):
        """Return the text of a WAV file."""
        if self.process is None or self.process.poll() is not None:
            raise VoiceError("whisper-server is not running")
        boundary = uuid.uuid4().hex
        with open(wav_path, "rb") as handle:
            audio = handle.read()
        fields = {"response_format": "json", "temperature": "0.0"}
        body = b""
        for key, value in fields.items():
            body += '--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.format(
                b=boundary, k=key, v=value
            ).encode()
        body += (
            '--{b}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n".format(b=boundary).encode()
            + audio
            + "\r\n--{b}--\r\n".format(b=boundary).encode()
        )
        request = urllib.request.Request(
            "http://127.0.0.1:{}/inference".format(self.local_port),
            data=body,
            method="POST",
        )
        request.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                reply = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise VoiceError("Transcription failed: {}".format(error))
        if "error" in reply:
            raise VoiceError("Transcription failed: {}".format(reply["error"]))
        return clean_text(reply.get("text", ""))

    def stop(self):
        """Stop the remote server and the tunnel."""
        if self.remote_pid:
            subprocess.run(
                [
                    "ssh",
                    *SSH_OPTS,
                    self.target.ssh_target,
                    "kill {} 2>/dev/null || true".format(self.remote_pid),
                ],  # fmt: skip
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.remote_pid = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process = None


# Whisper marks non-speech this way; such segments are not words the user said.
NON_SPEECH = re.compile(
    r"\[(BLANK_AUDIO|MUSIC|NOISE|SILENCE|INAUDIBLE)[^\]]*\]|\((silence|music|noise)\)",
    re.I,
)


def clean_text(text):
    text = NON_SPEECH.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


class VoiceInput:
    """Push-to-talk: Enter starts recording, Enter stops, text comes back."""

    def __init__(
        self, language="auto", host=None, user=None, device=None, message=print
    ):
        self.message = message
        self.recorder = Recorder(device)
        self.server = WhisperServer(language, host, user)
        self.tmp = tempfile.mkdtemp(prefix="i.ai-voice-")

    def start(self):
        self.message("Searching for a GPU on {}...".format(self.server.remote))
        gpu = self.server.start()
        self.message(
            "Voice input ready: {}, recording with {}.".format(gpu, self.recorder.tool)
        )

    def listen(self, wait_start=input, wait_stop=input):
        """Record one utterance and return its text ('' when nothing was said).

        :param wait_start: called before recording (default: wait for Enter)
        :param wait_stop: called while recording (default: wait for Enter)
        """
        if wait_start is not None:
            wait_start()
        self.recorder.start(self.tmp)
        try:
            wait_stop()
        finally:
            path, seconds = self.recorder.stop()
        try:
            if seconds < 0.3:
                return ""
            return self.server.transcribe(path)
        finally:
            if path and os.path.exists(path):
                os.remove(path)  # Speech is not kept.

    def close(self):
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
