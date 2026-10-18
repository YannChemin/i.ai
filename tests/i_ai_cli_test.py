"""Tests of the i.ai command line that need no language model."""

import json
import os
import subprocess
import sys

from conftest import ADDON_DIR

SCRIPT = os.path.join(ADDON_DIR, "i.ai.py")


def run(session, *args):
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True,
        text=True,
        env=session.env,
        check=False,
    )


def test_info_json(session):
    result = run(session, "operation=info", "format=json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["maps"]["raster"] >= 3
    assert data["tools"]["total"] > 500


def test_query_required(session):
    result = run(session)
    assert result.returncode != 0
    assert "query" in result.stderr


def test_yes_requires_execute(session):
    result = run(session, "query=x", "-y")
    assert result.returncode != 0


def test_backend_name_given_as_model(session):
    result = run(session, "query=x", "model=claude")
    assert result.returncode != 0
    assert "backend=claude" in result.stderr


def test_api_key_file(session, tmp_path):
    """The key file is read; a missing one fails loudly; open permissions warn."""
    result = run(
        session,
        "query=x",
        "backend=blablador",
        "api_key_file=" + str(tmp_path / "none"),
    )
    assert result.returncode != 0 and "Cannot read the API key file" in result.stderr
    key = tmp_path / "key"
    key.write_text("not-a-real-key\n")
    key.chmod(0o644)
    # A wrong key on a local port that nothing listens on: the run fails at the
    # connection, after the permission warning.
    result = run(
        session, "query=x", "backend=openai", "model=m", "url=http://127.0.0.1:9/v1",
        "api_key_file=" + str(key),
    )  # fmt: skip
    assert "can be read by other users" in result.stderr
    assert result.returncode != 0
