#!/usr/bin/env python3
##############################################################################
# MODULE:    i.ai
# AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
# PURPOSE:   AI assistant that answers questions and plans validated GRASS
#            commands from the live session, with Ollama, OpenAI-compatible
#            servers, Claude Code or OpenCode as the language model.
# SPDX-License-Identifier: Unlicense
##############################################################################

# %module
# % description: AI assistant for GRASS: answers questions and plans validated commands with every installed tool.
# % keyword: imagery
# % keyword: AI
# % keyword: LLM
# % keyword: assistant
# % keyword: automation
# %end

# %option
# % key: query
# % type: string
# % required: no
# % description: Question or task for the assistant
# % guisection: Request
# %end

# %option
# % key: operation
# % type: string
# % required: no
# % options: ask,info,index
# % answer: ask
# % description: What to do
# % descriptions: ask;Answer the query or plan commands for it;info;Print the session context the assistant sees;index;Cache every tool interface and index the manual pages
# % guisection: Request
# %end

# %option
# % key: backend
# % type: string
# % required: no
# % options: ollama,openai,claude,opencode
# % answer: ollama
# % description: Language model backend
# % descriptions: ollama;Local Ollama server;openai;OpenAI-compatible server (key in IAI_API_KEY or OPENAI_API_KEY);claude;Claude Code CLI on the logged-in account;opencode;OpenCode CLI (default model opencode/big-pickle, guest access)
# % guisection: Model
# %end

# %option
# % key: model
# % type: string
# % required: no
# % description: Model name (default: first Ollama model, Claude Code default, or opencode/big-pickle)
# % guisection: Model
# %end

# %option
# % key: url
# % type: string
# % required: no
# % description: Server URL (Ollama, OpenAI-compatible base URL, or a running OpenCode server)
# % guisection: Model
# %end

# %option
# % key: embed_model
# % type: string
# % required: no
# % description: Embedding model for operation=index (ollama and openai backends)
# % guisection: Model
# %end

# %option
# % key: session
# % type: string
# % required: no
# % description: Name of a dialog session to continue and save
# % guisection: Request
# %end

# %option
# % key: format
# % type: string
# % required: no
# % options: plain,json,shell
# % answer: plain
# % description: Output format
# % descriptions: plain;Answer and plan as text;json;Full result as JSON;shell;Plan as a shell script
# % guisection: Output
# %end

# %option
# % key: max_steps
# % type: integer
# % required: no
# % answer: 10
# % description: Maximum number of reasoning steps per request
# % guisection: Model
# %end

# %option
# % key: timeout
# % type: integer
# % required: no
# % answer: 300
# % description: Timeout of one language model call in seconds
# % guisection: Model
# %end

# %flag
# % key: e
# % description: Execute the validated plan
# % guisection: Execution
# %end

# %flag
# % key: y
# % description: Run plan steps that change existing data or session state without asking
# % guisection: Execution
# %end

# %flag
# % key: i
# % description: Interactive dialog
# % guisection: Request
# %end

# %rules
# % requires: -y, -e
# %end

import json
import os
import sys

import grass.script as gs
from grass.exceptions import CalledModuleError


def _load_library():
    """Make the i.ai helper modules importable, installed or in the source tree."""
    here = os.path.dirname(os.path.realpath(__file__))
    candidates = [here]
    path = gs.utils.get_lib_path(modname="i.ai")
    if path:
        candidates.insert(0, path)
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "iai_agents.py")):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return
    gs.fatal(_("Cannot find the i.ai library files (iai_*.py)"))


class Session:
    """Everything one i.ai run needs, wired together."""

    def __init__(self, options, need_backend=True):
        from iai_catalog import ModuleCatalog
        from iai_context import ContextManager
        from iai_llm import BackendError, make_backend
        from iai_rag import ManualIndex
        from iai_registry import EntityMatcher, EntityRegistry
        from iai_tools import ToolRegistry
        from iai_validate import ValidationLayer

        self.options = options
        self.catalog = ModuleCatalog()
        self.registry = EntityRegistry()
        self.matcher = EntityMatcher(self.registry)
        self.validator = ValidationLayer(
            self.catalog, self.registry, self.matcher, overwrite=gs.overwrite()
        )
        self.context = ContextManager(self.catalog, self.registry)
        self.backend = None
        if need_backend:
            self.backend = make_backend(
                options["backend"],
                model=options["model"] or None,
                url=options["url"] or None,
                timeout=int(options["timeout"]),
                embed_model=options["embed_model"] or None,
            )
            try:
                self.backend.check()
            except BackendError as error:
                gs.fatal(str(error))
            gs.verbose(_("Language model: {}").format(self.backend.describe()))
        self.manual = ManualIndex(self.backend)
        self.tools = ToolRegistry(
            self.catalog, self.registry, self.matcher, self.validator, self.manual
        )
        self.copilot = None
        if need_backend:
            from iai_agents import Copilot

            self.copilot = Copilot(
                self.backend,
                self.context,
                self.tools,
                self.validator,
                max_steps=int(options["max_steps"]),
            )

    def refresh(self):
        self.registry.refresh()
        self.context.refresh()


def print_result(result, output_format):
    """Print a copilot result on stdout in the requested format."""
    try:
        _print_result(result, output_format)
    finally:
        sys.stdout.flush()


def _print_result(result, output_format):
    if output_format == "json":
        data = {k: v for k, v in result.items() if k != "plan"}
        if result.get("plan"):
            data["plan"] = [
                dict(r.command.to_dict(), risk=r.risk, warnings=r.warnings)
                for r in result["plan"]
            ]
        print(json.dumps(data, indent=2, default=str))
        return
    if output_format == "shell":
        print("#!/bin/sh")
        print("# Generated by i.ai; review before running.")
        if result.get("text"):
            for line in result["text"].splitlines():
                print("# " + line)
        for r in result.get("plan") or []:
            if r.command.purpose:
                print("# " + r.command.purpose)
            print(r.command.shell())
        for q in result.get("questions") or []:
            print("# Question: " + q)
        return
    if result["kind"] == "error":
        gs.warning(result["text"])
        return
    if result.get("text"):
        print(result["text"])
    if result["kind"] == "clarify":
        for q in result["questions"]:
            print("? " + q)
        if result.get("suggestions"):
            print("\nYou could ask instead:")
            for s in result["suggestions"]:
                print("  - " + s)
    if result.get("plan"):
        print("\nPlan:")
        for n, r in enumerate(result["plan"], 1):
            note = "" if r.risk != "modify" else "  [changes existing data or state]"
            if r.command.purpose:
                print("  # {}".format(r.command.purpose))
            print("  {}. {}{}".format(n, r.command.shell(), note))
            for w in r.warnings:
                print("     warning: " + w)


def confirm(question):
    """Ask a yes/no question on the terminal; no terminal means no."""
    sys.stdout.flush()
    if not sys.stdin.isatty():
        return False
    try:
        answer = input("{} [y/N] ".format(question))
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def execute_plan(session, results, assume_yes, interactive):
    """Run a validated plan step by step, re-validating each step first.

    :return: True when every step ran
    """
    from iai_validate import Command

    sys.stdout.flush()  # Show the plan before the tools' own messages.
    commands = [r.command for r in results]
    total = len(commands)
    for n, command in enumerate(commands, 1):
        gs.percent(n - 1, total, 1)
        check = session.validator.validate(Command.from_any(command.to_dict()))
        if not check.valid:
            gs.warning(
                _("Step {n} <{cmd}> is no longer valid: {err}").format(
                    n=n, cmd=command.shell(), err="; ".join(check.errors)
                )
            )
            return False
        if check.risk == "modify" and not assume_yes:
            if not confirm(
                _(
                    "Step {n} changes existing data or session state:\n  {cmd}\nRun it?"
                ).format(n=n, cmd=command.shell())
            ):
                gs.warning(
                    _(
                        "Stopped before step {n} <{cmd}>; run i.ai with -y to allow "
                        "steps that change existing data or state."
                    ).format(n=n, cmd=command.shell())
                )
                return False
        gs.message(
            _("Step {n}/{total}: {cmd}...").format(
                n=n, total=total, cmd=command.shell()
            )
        )
        kwargs = dict(command.params)
        if command.flags:
            kwargs["flags"] = command.flags
        try:
            gs.run_command(command.module, overwrite=gs.overwrite(), **kwargs)
        except CalledModuleError as error:
            message = _("Step {n} <{cmd}> failed: {err}").format(
                n=n, cmd=command.shell(), err=error
            )
            if interactive:
                gs.warning(message)
                return False
            gs.fatal(message)
        session.registry.refresh()
    gs.percent(1, 1, 1)
    session.context.refresh()
    return True


def run_index(session, options):
    gs.message(_("Reading the interface of every installed tool..."))
    failed = session.catalog.describe_all()
    gs.message(
        _("{n} tools described, {f} unreadable.").format(
            n=len(session.catalog.modules) - len(failed), f=len(failed)
        )
    )
    if failed:
        gs.verbose(_("Unreadable: {}").format(", ".join(failed)))
    gs.message(_("Indexing the manual pages..."))
    session.manual.build_lexical(progress=True)
    gs.message(_("{n} manual sections indexed.").format(n=len(session.manual.chunks)))
    if session.backend is None:
        return
    from iai_llm import BackendError

    try:
        session.backend.embed(["test"])
    except BackendError as error:
        gs.message(
            _("No embeddings ({}); manual search will use keywords.").format(error)
        )
        return
    gs.message(_("Embedding the manual sections..."))
    session.manual.build_dense()


def interactive_loop(session, flags, output_format, store, session_name):
    gs.message(
        _(
            "i.ai interactive dialog. Commands: /run (execute the last plan), /plan, "
            "/context, /reset, /exit."
        )
    )
    last = None
    while True:
        try:
            text = input("i.ai> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/exit", "/quit", "exit", "quit", "i.ai close"):
            break
        if text == "/context":
            print(session.context.for_prompt())
            continue
        if text == "/reset":
            session.context.history = []
            session.context.last_plan = []
            last = None
            gs.message(_("Dialog history cleared."))
            continue
        if text == "/plan":
            if last and last.get("plan"):
                print_result({"kind": "plan", "plan": last["plan"]}, output_format)
            else:
                gs.message(_("No plan yet."))
            continue
        if text == "/run":
            if last and last.get("plan"):
                execute_plan(session, last["plan"], flags["y"], interactive=True)
            else:
                gs.message(_("No plan to run."))
            continue
        result = session.copilot.run(text)
        print_result(result, output_format)
        last = result if result.get("plan") else last
        if result.get("plan") and flags["e"] and confirm(_("Run this plan?")):
            execute_plan(session, result["plan"], flags["y"], interactive=True)
        if store:
            store.save(session_name, session.context)


def main():
    _load_library()
    from iai_context import SessionStore

    operation = options["operation"]
    output_format = options["format"]

    if operation == "info":
        session = Session(options, need_backend=False)
        if output_format == "json":
            print(json.dumps(session.context.summary(), indent=2, default=str))
        else:
            print(session.context.for_prompt())
        return

    if operation == "index":
        need = options["backend"] in ("ollama", "openai") and bool(
            options["embed_model"]
        )
        session = Session(options, need_backend=need)
        run_index(session, options)
        return

    if not options["query"] and not flags["i"]:
        gs.fatal(_("Give a query, or use -i for an interactive dialog"))

    session = Session(options)
    store = SessionStore() if options["session"] else None
    if store and store.load(options["session"], session.context):
        gs.verbose(_("Continuing session <{}>").format(options["session"]))

    if options["query"]:
        result = session.copilot.run(options["query"])
        print_result(result, output_format)
        if store:
            store.save(options["session"], session.context)
        if result["kind"] == "error" and not flags["i"]:
            sys.exit(1)
        if result.get("plan") and flags["e"]:
            execute_plan(session, result["plan"], flags["y"], interactive=flags["i"])
        elif result.get("plan") and not flags["i"]:
            gs.message(_("Nothing was run; add -e to execute the plan."))

    if flags["i"]:
        interactive_loop(session, flags, output_format, store, options["session"])
    session.catalog.save()


if __name__ == "__main__":
    options, flags = gs.parser()
    sys.exit(main())
