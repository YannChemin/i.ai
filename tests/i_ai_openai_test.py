"""Tests of the OpenAI-compatible backends against a local fake server.

The fake server speaks the OpenAI API (/v1/models, /v1/chat/completions,
/v1/embeddings) with configurable behaviour: rejected keys, unsupported
output modes, rate limits, reasoning output. No network is used.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from iai_llm import BackendError, BlabladorBackend, OpenAIBackend, make_backend

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


class Fake:
    """Behaviour of the fake server, changed per test."""

    key = "secret"
    models = ["alias-large", "alias-fast", "alias-embeddings"]
    supported = {"json_schema", "json_object", "prompt"}
    content = '{"ok": true}'
    rate_limited = 0  # Number of calls answered with 429 first.
    retry_after = "1"
    forbidden = False
    seen = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, data, headers=None):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if self.headers.get("Authorization") != "Bearer " + Fake.key:
            self.reply(401, {"error": {"message": "You must provide a valid API key.",
                                       "code": "invalid_api_key"}})  # fmt: skip
            return False
        if Fake.forbidden:
            self.reply(403, {"error": {"message": "User is banned"}})
            return False
        return True

    def do_GET(self):
        if self.path == "/v1/models" and self.authorized():
            self.reply(200, {"data": [{"id": m} for m in Fake.models]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self.authorized():
            return
        if self.path == "/v1/embeddings":
            self.reply(
                200, {"data": [{"embedding": [1.0, 0.0]} for _ in payload["input"]]}
            )
            return
        mode = (payload.get("response_format") or {}).get("type", "prompt")
        Fake.seen.append(mode)
        if Fake.rate_limited:
            Fake.rate_limited -= 1
            self.reply(429, {"error": {"message": "Rate limit exceeded"}},
                       {"Retry-After": Fake.retry_after})  # fmt: skip
            return
        if mode not in Fake.supported:
            self.reply(400, {"error": {"message": "response_format not supported"}})
            return
        self.reply(200, {"choices": [{"message": {"content": Fake.content}}]})


@pytest.fixture
def server():
    for key, value in vars(Fake).copy().items():
        if not key.startswith("_"):
            setattr(Fake, key, value if not isinstance(value, list) else list(value))
    Fake.key, Fake.supported = "secret", {"json_schema", "json_object", "prompt"}
    Fake.content, Fake.rate_limited, Fake.retry_after = '{"ok": true}', 0, "1"
    Fake.forbidden, Fake.seen = False, []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:{}/v1".format(httpd.server_address[1])
    httpd.shutdown()


def backend(url, **kwargs):
    kwargs.setdefault("key", "secret")
    return BlabladorBackend(url=url, **kwargs)


def ask(b):
    return b.complete_json("system", [{"role": "user", "content": "q"}], SCHEMA)


def test_blablador_defaults(monkeypatch):
    monkeypatch.setenv("BLABLADOR_API_KEY", "from-env")
    b = make_backend("blablador")
    assert b.url == "https://api.blablador.fz-juelich.de/v1"
    assert b.model == "alias-large" and b.embed_model == "alias-embeddings"
    assert b.key == "from-env"


def test_key_and_models_check(server):
    b = backend(server)
    b.check()
    assert ask(b) == {"ok": True}
    with pytest.raises(BackendError, match="not offered.*alias-fast"):
        backend(server, model="alias-nope").check()


def test_rejected_key_and_forbidden(server):
    with pytest.raises(BackendError, match="rejected the API key.*BLABLADOR_API_KEY"):
        backend(server, key="wrong").check()
    Fake.forbidden = True
    with pytest.raises(BackendError, match="refused access"):
        backend(server).check()


def test_missing_key_is_reported(monkeypatch):
    for name in ("BLABLADOR_API_KEY", "IAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(BackendError, match="No API key"):
        BlabladorBackend().check()


def test_output_mode_fallback_is_remembered(server):
    Fake.supported = {"prompt"}
    b = backend(server)
    assert ask(b) == {"ok": True}
    assert Fake.seen == ["json_schema", "json_object", "prompt"]
    ask(b)
    assert Fake.seen[-1] == "prompt" and len(Fake.seen) == 4  # No new probing.


def test_reasoning_is_stripped(server):
    Fake.content = '<think>The {schema} says {"ok": false}...</think>\n{"ok": true}'
    assert ask(backend(server)) == {"ok": True}


def test_rate_limit_is_waited_out(server):
    Fake.rate_limited, Fake.retry_after = 2, "0.2"
    notes = []
    b = backend(server)
    b.notify = notes.append
    start = time.time()
    assert ask(b) == {"ok": True}
    assert time.time() - start >= 0.4 and len(notes) == 2
    assert "waiting" in notes[0]


def test_rate_limit_gives_up_after_budget(server):
    Fake.rate_limited, Fake.retry_after = 99, "0.3"
    b = backend(server, max_wait=0.5)
    with pytest.raises(BackendError, match="rate limit"):
        ask(b)


def test_generic_openai_backend_with_key(server):
    b = OpenAIBackend(model="alias-fast", url=server, key="secret")
    b.check()
    assert ask(b) == {"ok": True}
    assert backend(server).embed(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]
