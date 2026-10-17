"""
MODULE:    i.ai
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Language model backends for i.ai.
SPDX-License-Identifier: Unlicense

Every backend answers one question: given a system prompt and a list of
chat messages, return one JSON object that matches a JSON schema. The
agents in iai_agents.py drive the tool loop themselves, so a backend
never has to support native tool calling. This keeps command-line agents
(Claude Code, OpenCode) and small local models on the same protocol.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

BACKENDS = ("ollama", "openai", "claude", "opencode")


class BackendError(RuntimeError):
    """Raised when a backend cannot be reached or returns no usable JSON."""


def extract_json(text):
    """Return the first JSON object found in a model reply.

    Models wrap JSON in prose or code fences despite instructions, so the
    reply is scanned for the first balanced object that parses.

    :param str text: Raw model reply
    :return: Parsed object
    :rtype: dict
    """
    if not text:
        raise BackendError("Empty reply from the language model")
    text = text.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except ValueError:
            pass
    decoder = json.JSONDecoder()
    for start in (m.start() for m in re.finditer(r"\{", text)):
        try:
            value, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise BackendError("No JSON object in the model reply: {}".format(text[:300]))


def _flatten(system, messages):
    """Render a chat as one prompt for command-line agents."""
    parts = ["# Instructions\n\n" + system] if system else []
    for message in messages:
        parts.append("# {}\n\n{}".format(message["role"].upper(), message["content"]))
    parts.append(
        "# Reply\n\nReply with one JSON object only, no prose and no code fence."
    )
    return "\n\n".join(parts)


def _http_json(url, payload, headers=None, timeout=300):
    """POST a JSON payload and return the decoded JSON reply."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")[:500]
        raise BackendError("HTTP {} from {}: {}".format(error.code, url, body))
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise BackendError("Cannot reach {}: {}".format(url, error))


def _http_get_json(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise BackendError("Cannot reach {}: {}".format(url, error))


class Backend:
    """Base class: one structured JSON completion per call."""

    name = "base"

    def __init__(self, model=None, url=None, timeout=300):
        self.model = model
        self.url = url
        self.timeout = timeout

    def describe(self):
        return "{} ({})".format(self.name, self.model or "default model")

    def check(self):
        """Fail with BackendError when the backend is not usable."""

    def complete_json(self, system, messages, schema):
        """Return one JSON object answering the chat.

        :param str system: System prompt
        :param list messages: Chat messages, dicts with role and content
        :param dict schema: JSON schema the reply must follow
        :rtype: dict
        """
        raise NotImplementedError

    def embed(self, texts):
        """Return one embedding vector per text, or raise BackendError."""
        raise BackendError("Backend {} has no embedding endpoint".format(self.name))


class OllamaBackend(Backend):
    """Local Ollama server, with JSON schema constrained output."""

    name = "ollama"

    def __init__(self, model=None, url=None, timeout=300, embed_model=None):
        super().__init__(model, url or "http://localhost:11434", timeout)
        self.url = self.url.rstrip("/")
        self.embed_model = embed_model or "nomic-embed-text"

    def installed_models(self):
        tags = _http_get_json(self.url + "/api/tags")
        return [m["name"] for m in tags.get("models", [])]

    def check(self):
        models = self.installed_models()
        if not models:
            raise BackendError(
                "No model installed in Ollama at {}; run 'ollama pull <model>'".format(
                    self.url
                )
            )
        if not self.model:
            self.model = models[0]
        elif self.model not in models and self.model + ":latest" not in models:
            raise BackendError(
                "Model <{}> is not installed in Ollama (installed: {})".format(
                    self.model, ", ".join(models)
                )
            )

    def complete_json(self, system, messages, schema):
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0},
        }
        reply = _http_json(self.url + "/api/chat", payload, timeout=self.timeout)
        return extract_json(reply.get("message", {}).get("content", ""))

    def embed(self, texts):
        reply = _http_json(
            self.url + "/api/embed",
            {"model": self.embed_model, "input": texts},
            timeout=self.timeout,
        )
        vectors = reply.get("embeddings")
        if not vectors:
            raise BackendError(
                "Ollama returned no embeddings; is <{}> pulled?".format(
                    self.embed_model
                )
            )
        return vectors


class OpenAIBackend(Backend):
    """Any OpenAI-compatible chat completions server.

    This covers OpenAI itself, llama.cpp server, vLLM, LM Studio and
    similar. The key is read from IAI_API_KEY, then OPENAI_API_KEY.
    """

    name = "openai"

    def __init__(self, model=None, url=None, timeout=300, embed_model=None):
        super().__init__(model, url or "https://api.openai.com/v1", timeout)
        self.url = self.url.rstrip("/")
        self.embed_model = embed_model or "text-embedding-3-small"
        self.key = os.environ.get("IAI_API_KEY") or os.environ.get("OPENAI_API_KEY")

    def _headers(self):
        return {"Authorization": "Bearer " + self.key} if self.key else {}

    def check(self):
        if not self.model:
            raise BackendError("The openai backend needs model=<name>")

    def complete_json(self, system, messages, schema):
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "step", "schema": schema},
            },
        }
        url = self.url + "/chat/completions"
        try:
            reply = _http_json(url, payload, self._headers(), self.timeout)
        except BackendError as error:
            # Older servers only know the plain JSON mode.
            if "HTTP 400" not in str(error):
                raise
            payload["response_format"] = {"type": "json_object"}
            reply = _http_json(url, payload, self._headers(), self.timeout)
        choices = reply.get("choices") or [{}]
        return extract_json(choices[0].get("message", {}).get("content", ""))

    def embed(self, texts):
        reply = _http_json(
            self.url + "/embeddings",
            {"model": self.embed_model, "input": texts},
            self._headers(),
            self.timeout,
        )
        return [item["embedding"] for item in reply.get("data", [])]


class ClaudeCodeBackend(Backend):
    """Claude Code in headless mode, on the user's own logged-in account.

    Each call runs 'claude -p' with its built-in tools disabled and the
    reply constrained by --json-schema, so Claude only plans; i.ai keeps
    control of every GRASS command. No API key is needed when the CLI is
    logged in.
    """

    name = "claude"

    def __init__(self, model=None, url=None, timeout=300):
        super().__init__(model, url, timeout)
        self.executable = shutil.which("claude")

    def check(self):
        if not self.executable:
            raise BackendError(
                "The claude command was not found; install Claude Code and log in"
            )

    def complete_json(self, system, messages, schema):
        # The system prompt goes through --system-prompt.
        prompt = _flatten(None, messages)
        cmd = [
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--system-prompt",
            system,
            "--json-schema",
            json.dumps(schema),
        ]
        if self.model:
            cmd += ["--model", self.model]
        with tempfile.TemporaryDirectory(prefix="i.ai-claude-") as workdir:
            result = _run_cli(cmd, prompt, workdir, self.timeout, "claude")
        try:
            envelope = json.loads(result)
        except ValueError:
            return extract_json(result)
        if envelope.get("is_error"):
            raise BackendError(
                "Claude Code error: {}".format(
                    envelope.get("result") or envelope.get("subtype")
                )
            )
        if isinstance(envelope.get("structured_output"), dict):
            return envelope["structured_output"]
        return extract_json(envelope.get("result", ""))


class OpenCodeBackend(Backend):
    """OpenCode, by default with the free Big Pickle model as a guest.

    Each call runs 'opencode run' with the read-only plan agent, in an
    empty temporary directory, without auto-approved permissions, and
    reads the text events of its JSON event stream.
    """

    name = "opencode"

    def __init__(self, model=None, url=None, timeout=300):
        super().__init__(model or "opencode/big-pickle", url, timeout)
        self.executable = shutil.which("opencode") or _home_opencode()

    def check(self):
        if not self.executable:
            raise BackendError(
                "The opencode command was not found; install it from opencode.ai"
            )

    def complete_json(self, system, messages, schema):
        instructions = (
            system + "\n\nDo not use any of your own tools: no file reads, no edits, no"
            " shell. Answer from this prompt only.\n\nThe reply must follow this"
            " JSON schema:\n" + json.dumps(schema)
        )
        prompt = _flatten(instructions, messages)
        with tempfile.TemporaryDirectory(prefix="i.ai-opencode-") as workdir:
            cmd = [
                self.executable,
                "run",
                "--pure",
                "-m",
                self.model,
                "--agent",
                "plan",
                "--dir",
                workdir,
                "--format",
                "json",
            ]
            if self.url:
                cmd += ["--attach", self.url]
            output = _run_cli(cmd, prompt, workdir, self.timeout, "opencode")
        texts = []
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "text":
                texts.append(event.get("part", {}).get("text", ""))
            elif event.get("type") == "error":
                raise BackendError("OpenCode error: {}".format(event.get("error")))
        if not texts:
            raise BackendError("OpenCode returned no text: {}".format(output[-500:]))
        return extract_json(texts[-1])


def _home_opencode():
    path = os.path.expanduser("~/.opencode/bin/opencode")
    return path if os.access(path, os.X_OK) else None


def _run_cli(cmd, prompt, workdir, timeout, label):
    """Run a command-line agent with the prompt on stdin."""
    env = os.environ.copy()
    # A GRASS session exports variables that agent CLIs do not need.
    env.pop("LD_PRELOAD", None)
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=workdir,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise BackendError("{} did not answer within {} s".format(label, timeout))
    except OSError as error:
        raise BackendError("Cannot run {}: {}".format(label, error))
    if result.returncode != 0 and not result.stdout.strip():
        raise BackendError(
            "{} failed (exit {}): {}".format(
                label, result.returncode, result.stderr.strip()[-500:]
            )
        )
    return result.stdout


def make_backend(name, model=None, url=None, timeout=300, embed_model=None):
    """Return a backend instance by name.

    :param str name: One of BACKENDS
    """
    if name == "ollama":
        return OllamaBackend(model, url, timeout, embed_model)
    if name == "openai":
        return OpenAIBackend(model, url, timeout, embed_model)
    if name == "claude":
        return ClaudeCodeBackend(model, url, timeout)
    if name == "opencode":
        return OpenCodeBackend(model, url, timeout)
    raise BackendError("Unknown backend <{}>".format(name))
