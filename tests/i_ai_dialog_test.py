"""Tests of the interactive dialog: plan approval and direct commands."""

import builtins
import importlib.util
import os

import pytest

from conftest import ADDON_DIR


@pytest.fixture(scope="module")
def main():
    spec = importlib.util.spec_from_file_location(
        "i_ai_main", os.path.join(ADDON_DIR, "i.ai.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def iai(parts, tmp_path):
    from iai_context import ContextManager
    from iai_shell import CommandCatalog, ShellValidator

    catalog, registry, matcher, validator = parts

    class Session:
        pass

    session = Session()
    session.catalog, session.registry, session.validator = catalog, registry, validator
    session.commands = CommandCatalog()
    session.shell_validator = ShellValidator(session.commands, catalog, validator)
    session.context = ContextManager(catalog, registry, session.commands)
    session.context.workdir = str(tmp_path)
    return session


@pytest.mark.parametrize(
    "text", ["Do it.", "yes", "OK!", "Run it", "go ahead", "Vas-y", "oui"]
)
def test_approvals(main, text):
    assert main.is_approval(text)


@pytest.mark.parametrize(
    "text", ["Open monitor wx0", "do it with the slope map", "what is it?"]
)
def test_not_approvals(main, text):
    assert not main.is_approval(text)


def dialog(main, iai, monkeypatch, lines, plan_text=None):
    """Run the dialog on scripted input lines; return the requests the model got."""
    from iai_shell import plan_item, validate_steps

    session = iai
    asked = []

    class FakeCopilot:
        def run(self, text, intent=None):
            asked.append(text)
            item = plan_item(plan_text)
            results = validate_steps([item], session.validator, session.shell_validator)
            return {"kind": "plan", "text": "", "plan": results}

    session.copilot = FakeCopilot()
    feed = iter(lines)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(feed))
    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: True)
    main.interactive_loop(session, {"y": False, "e": False}, "plain", None, None)
    return asked


def test_plan_offered_then_approved_by_words(main, iai, monkeypatch, tmp_path):
    marker = tmp_path / "ran.txt"
    asked = dialog(
        main,
        iai,
        monkeypatch,
        ["make the marker", "n", "Do it.", "/exit"],  # Plan, decline, approve.
        plan_text={"shell": "echo ran > {}".format(marker)},
    )
    assert asked == ["make the marker"]  # "Do it." did not go to the model.
    assert marker.read_text().strip() == "ran"


def test_direct_grass_and_shell_commands(main, iai, monkeypatch, tmp_path, capfd):
    marker = tmp_path / "direct.txt"
    asked = dialog(
        main,
        iai,
        monkeypatch,
        ["r.info -g map=elevation", "!echo direct > {}".format(marker), "/exit"],
    )
    assert asked == []  # Neither line went to the model.
    assert "rows=10" in capfd.readouterr().out
    assert marker.read_text().strip() == "direct"


def test_direct_commands_are_validated(main, iai, monkeypatch, capfd):
    dialog(main, iai, monkeypatch, ["!sudo ls", "r.info mapp=elevation", "/exit"])
    err = capfd.readouterr().err
    assert "sudo is not allowed" in err and "no parameter 'mapp'" in err
