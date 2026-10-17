"""Tests of the JSON extraction shared by all backends (no model is called)."""

import pytest

from iai_llm import BackendError, extract_json, make_backend


def test_plain_json():
    assert extract_json('{"action": "answer"}') == {"action": "answer"}


def test_fenced_json():
    text = 'Here it is:\n```json\n{"a": 1}\n```\nDone.'
    assert extract_json(text) == {"a": 1}


def test_json_inside_prose():
    assert extract_json('Sure. {"a": {"b": [1, 2]}} trailing') == {"a": {"b": [1, 2]}}


def test_no_json_fails_loudly():
    with pytest.raises(BackendError):
        extract_json("no braces here")


def test_unknown_backend():
    with pytest.raises(BackendError):
        make_backend("nonexistent")


def test_opencode_default_model():
    assert make_backend("opencode").model == "opencode/big-pickle"


def fake_opencode(tmp_path, body):
    """An executable standing in for opencode, reading the prompt on stdin."""
    path = tmp_path / "opencode"
    path.write_text("#!/usr/bin/env bash\ncat >/dev/null\n" + body)
    path.chmod(0o755)
    return str(path)


def test_opencode_answer(tmp_path):
    import json

    from iai_llm import OpenCodeBackend

    event = tmp_path / "event.jsonl"
    event.write_text(
        json.dumps({"type": "text", "part": {"text": '{"ok": true}'}}) + "\n"
    )
    backend = OpenCodeBackend(timeout=30)
    backend.executable = fake_opencode(tmp_path, "cat {}\n".format(event))
    assert backend.complete_json("s", [{"role": "user", "content": "q"}], {}) == {
        "ok": True
    }


def test_opencode_rate_limit_fails_fast(tmp_path):
    """opencode logs the refusal and then hangs: i.ai must not wait for it."""
    import time

    from iai_llm import OpenCodeBackend

    backend = OpenCodeBackend(timeout=60)
    backend.executable = fake_opencode(
        tmp_path,
        'echo \'timestamp=x level=ERROR message="stream error" modelID=big-pickle '
        'error.error="AI_APICallError: Rate limit exceeded. Please try again later."\' >&2\n'
        "sleep 50\n",
    )
    start = time.time()
    with pytest.raises(BackendError, match="rate limit exceeded"):
        backend.complete_json("s", [{"role": "user", "content": "q"}], {})
    assert time.time() - start < 10
