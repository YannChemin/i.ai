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
