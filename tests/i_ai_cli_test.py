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
