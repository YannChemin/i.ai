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
import threading
import time
import urllib.error
import urllib.request

BACKENDS = ("ollama", "openai", "blablador", "claude", "opencode")


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
    # Reasoning models (Qwen3, DeepSeek-R1...) may think aloud before the answer.
    text = re.sub(r"(?s)<think>.*?</think>", "", text).strip()
    if not text:
        raise BackendError("The language model replied with reasoning only")
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


class HTTPError(BackendError):
    """An HTTP error answer, with its status code and server message."""

    def __init__(self, code, url, message, retry_after=None):
        super().__init__("HTTP {} from {}: {}".format(code, url, message))
        self.code = code
        self.server_message = message
        self.retry_after = retry_after


def _request(url, payload=None, headers=None, timeout=300):
    """Send a GET (no payload) or JSON POST; return the decoded JSON reply."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")[:1000]
        try:
            message = json.loads(body).get("error", {})
            message = (
                message.get("message", body) if isinstance(message, dict) else message
            )
        except (ValueError, AttributeError):
            message = body
        retry = error.headers.get("Retry-After") if error.headers else None
        if retry is None:
            # Some servers give the wait only in the message ("retry in 37 s").
            found = re.search(r"(\d+(?:\.\d+)?)\s*(?:s\b|sec|second)", str(message))
            retry = found.group(1) if found else None
        try:
            retry = float(retry) if retry is not None else None
        except ValueError:
            retry = None
        raise HTTPError(error.code, url, str(message).strip(), retry)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise BackendError("Cannot reach {}: {}".format(url, error))
    except ValueError as error:
        raise BackendError("{} did not answer with JSON: {}".format(url, error))


class OpenAIBackend(Backend):
    """Any OpenAI-compatible chat completions server with an API key.

    This covers OpenAI itself, llama.cpp server, vLLM, LM Studio, academic
    services such as Blablador, and similar. The key comes from api_key_file
    (read by i.ai) or the environment (IAI_API_KEY, then OPENAI_API_KEY).

    Structured output is requested as a JSON schema; a server that rejects
    it gets JSON mode, then a plain request with the schema in the prompt.
    The level that works is kept for the rest of the session. Rate-limited
    calls (HTTP 429) are retried after the wait the server asks for, within
    max_wait seconds in total per call.
    """

    name = "openai"
    default_url = "https://api.openai.com/v1"
    default_model = None
    default_embed_model = "text-embedding-3-small"
    key_variables = ("IAI_API_KEY", "OPENAI_API_KEY")
    modes = ("json_schema", "json_object", "prompt")

    def __init__(
        self,
        model=None,
        url=None,
        timeout=300,
        embed_model=None,
        key=None,
        max_wait=180,
    ):
        super().__init__(model or self.default_model, url or self.default_url, timeout)
        self.url = self.url.rstrip("/")
        self.embed_model = embed_model or self.default_embed_model
        self.key = key or next(
            (os.environ[v] for v in self.key_variables if os.environ.get(v)), None
        )
        self.max_wait = max_wait
        self.mode = self.modes[0]
        self.notify = lambda message: None  # i.ai sets gs.message.
        self.verbose = lambda message: None  # i.ai sets gs.verbose.

    def _headers(self):
        return {"Authorization": "Bearer " + self.key} if self.key else {}

    def _call(self, path, payload=None, timeout=None):
        """Call the API, waiting out rate limits, with readable errors."""
        url = self.url + path
        waited = 0.0
        while True:
            try:
                return _request(url, payload, self._headers(), timeout or self.timeout)
            except HTTPError as error:
                if error.code == 429:
                    wait = error.retry_after if error.retry_after is not None else 10.0
                    if waited + wait > self.max_wait:
                        raise BackendError(
                            "{} keeps refusing calls for the rate limit ({}); waited {:.0f} s. "
                            "Try again later, use a faster model, or slow down.".format(
                                self.url, error.server_message, waited
                            )
                        )
                    self.notify(
                        "Rate limit of {}: waiting {:.0f} s before retrying...".format(
                            self.url, wait
                        )
                    )
                    time.sleep(wait)
                    waited += wait
                    continue
                if error.code == 401:
                    raise BackendError(
                        "{} rejected the API key ({}). Set it with api_key_file= or "
                        "{}.".format(
                            self.url,
                            error.server_message,
                            " or ".join(self.key_variables),
                        )
                    )
                if error.code == 403:
                    raise BackendError(
                        "{} refused access ({}): the key may lack rights to this model, or "
                        "the account may be blocked.".format(
                            self.url, error.server_message
                        )
                    )
                raise

    def check(self):
        if not self.model:
            raise BackendError("The {} backend needs model=<name>".format(self.name))
        # A local server (llama.cpp, vLLM...) may run without a key.
        local = re.match(r"https?://(localhost|127\.|\[::1\])", self.url)
        if not self.key and not local:
            raise BackendError(
                "No API key for {}: put it in a file given as api_key_file=, or in "
                "{}.".format(self.url, " or ".join(self.key_variables))
            )
        try:
            listing = self._call("/models", timeout=30)
        except HTTPError as error:
            if error.code == 404:
                return  # Servers without a model list: the first call will tell.
            raise
        names = [m.get("id") for m in listing.get("data", []) if isinstance(m, dict)]
        if names and self.model not in names:
            raise BackendError(
                "Model <{}> is not offered by {}. Available: {}".format(
                    self.model, self.url, ", ".join(sorted(n for n in names if n))
                )
            )

    def complete_json(self, system, messages, schema):
        while True:
            payload = {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, *messages],
                "temperature": 0,
            }
            if self.mode == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "step", "schema": schema},
                }
            elif self.mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
            if self.mode != "json_schema":
                payload["messages"][0] = {
                    "role": "system",
                    "content": system
                    + "\n\nReply with one JSON object, without prose or code fence, "
                    "following this JSON schema:\n" + json.dumps(schema),
                }
            try:
                reply = self._call("/chat/completions", payload)
            except HTTPError as error:
                # A server that does not support this output mode says 400 or 422.
                if error.code in (400, 422) and self.mode != self.modes[-1]:
                    old = self.mode
                    self.mode = self.modes[self.modes.index(self.mode) + 1]
                    self.verbose(
                        "{} refused {} output ({}); using {}".format(
                            self.url, old, error.server_message[:120], self.mode
                        )
                    )
                    continue
                raise
            choices = reply.get("choices") or [{}]
            content = choices[0].get("message", {}).get("content") or ""
            return extract_json(content)

    def embed(self, texts):
        reply = self._call("/embeddings", {"model": self.embed_model, "input": texts})
        return [item["embedding"] for item in reply.get("data", [])]


class BlabladorBackend(OpenAIBackend):
    """Blablador, the Helmholtz AI service at Jülich, open for research and teaching.

    The key is a Helmholtz Codebase personal access token with the read_user
    scope (https://sdlaml.pages.jsc.fz-juelich.de/ai/guides/blablador_api_access/).
    Models are named by alias: alias-large (default), alias-fast, alias-code,
    alias-huge; embeddings use alias-embeddings.
    """

    name = "blablador"
    default_url = "https://api.blablador.fz-juelich.de/v1"
    default_model = "alias-large"
    default_embed_model = "alias-embeddings"
    key_variables = ("BLABLADOR_API_KEY", "IAI_API_KEY")


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
                # Errors go to stderr as they happen: a refused request (such
                # as the guest rate limit) is logged, but opencode keeps running.
                "--print-logs",
                "--log-level",
                "ERROR",
            ]
            if self.url:
                cmd += ["--attach", self.url]
            output = _run_streaming(cmd, prompt, workdir, self.timeout, self.model)
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


OPENCODE_ERROR = re.compile(r'stream error.*?error\.error="([^"]+)"')


def _run_streaming(cmd, prompt, workdir, timeout, model):
    """Run opencode, failing as soon as it logs a model error.

    opencode logs a failed model call (rate limit, unknown model, network)
    and then waits instead of exiting, so its stderr is read live and the
    process is stopped at the first stream error.
    """
    env = os.environ.copy()
    env.pop("LD_PRELOAD", None)
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=workdir,
            env=env,
        )
    except OSError as error:
        raise BackendError("Cannot run opencode: {}".format(error))
    chunks, errors = [], []

    def read_stdout():
        chunks.append(process.stdout.read())

    def read_stderr():
        for line in process.stderr:
            match = OPENCODE_ERROR.search(line)
            if match:
                errors.append(match.group(1))
                process.terminate()

    readers = [
        threading.Thread(target=f, daemon=True) for f in (read_stdout, read_stderr)
    ]
    for reader in readers:
        reader.start()
    try:
        process.stdin.write(prompt)
        process.stdin.close()
    except BrokenPipeError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise BackendError("opencode did not answer within {} s".format(timeout))
    for reader in readers:
        reader.join(timeout=5)
    if errors:
        reason = errors[0]
        if "rate limit" in reason.lower():
            raise BackendError(
                "OpenCode refused the request to {}: rate limit exceeded (the free "
                "guest quota). Try again later, or use another backend such as "
                "backend=claude.".format(model)
            )
        raise BackendError("OpenCode error with {}: {}".format(model, reason))
    return "".join(chunks)


def make_backend(name, model=None, url=None, timeout=300, embed_model=None, key=None):
    """Return a backend instance by name.

    :param str name: One of BACKENDS
    :param str key: API key for the openai and blablador backends
    """
    if name == "ollama":
        return OllamaBackend(model, url, timeout, embed_model)
    if name == "openai":
        return OpenAIBackend(model, url, timeout, embed_model, key)
    if name == "blablador":
        return BlabladorBackend(model, url, timeout, embed_model, key)
    if name == "claude":
        return ClaudeCodeBackend(model, url, timeout)
    if name == "opencode":
        return OpenCodeBackend(model, url, timeout)
    raise BackendError("Unknown backend <{}>".format(name))
