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
# % options: ask,report,run,compile,info,index
# % answer: ask
# % description: What to do
# % descriptions: ask;Answer the query, plan commands, or write a report pipeline, as the request needs;report;Write a report pipeline (scripts and LaTeX report) for the query;run;Run the stored report pipeline, repair failing steps, write the results into the report and compile it;compile;Compile the stored report;info;Print the session context the assistant sees;index;Cache every tool interface and index the manual pages
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

# %option G_OPT_M_DIR
# % key: workdir
# % required: no
# % description: Report workspace directory (default: the session's directory under ~/.local/share/i.ai/sessions)
# % guisection: Report
# %end

# %option
# % key: from_step
# % type: string
# % required: no
# % description: With operation=run, first pipeline step to run (e.g. 04)
# % guisection: Report
# %end

# %option
# % key: repairs
# % type: integer
# % required: no
# % answer: 2
# % description: Repair attempts per failing step or LaTeX compilation
# % guisection: Report
# %end

# %option
# % key: author
# % type: string
# % required: no
# % description: Report author (default: git user.name)
# % guisection: Report
# %end

# %option
# % key: contact
# % type: string
# % required: no
# % description: Report contact line (default: git user.email)
# % guisection: Report
# %end

# %option
# % key: organization
# % type: string
# % required: no
# % description: Organization shown in the report header
# % guisection: Report
# %end

# %option
# % key: classification
# % type: string
# % required: no
# % description: Classification banner of the report, e.g. RESTRICTED (none by default)
# % guisection: Report
# %end

# %option
# % key: input
# % type: string
# % required: no
# % options: typing,voice
# % answer: typing
# % description: How requests are entered
# % descriptions: typing;Typed in the terminal;voice;Spoken into the microphone, transcribed on a GPU (Enter starts and stops recording)
# % guisection: Voice
# %end

# %option
# % key: voice_host
# % type: string
# % required: no
# % description: IP address or name of a GPU server for transcription (default: IAI_VOICE_HOST, else this machine)
# % guisection: Voice
# %end

# %option
# % key: voice_user
# % type: string
# % required: no
# % description: Login name on the GPU server (default: IAI_VOICE_USER, else the ssh default)
# % guisection: Voice
# %end

# %option
# % key: voice_language
# % type: string
# % required: no
# % answer: en
# % description: Spoken language for transcription (en, fr, ..., or auto to detect it)
# % guisection: Voice
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
# % description: Execute the validated plan, or run the report pipeline
# % guisection: Execution
# %end

# %flag
# % key: y
# % description: Run steps that change existing data, session state or use the network without asking
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
import re
import subprocess
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
        from iai_llm import BACKENDS, BackendError, make_backend
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
        from iai_shell import CommandCatalog, ShellValidator

        self.commands = CommandCatalog()
        self.shell_validator = ShellValidator(
            self.commands, self.catalog, self.validator
        )
        self.context = ContextManager(self.catalog, self.registry, self.commands)
        self.backend = None
        if need_backend:
            model = (options["model"] or "").lower()
            if model in BACKENDS and model != options["backend"]:
                # model= names a model of the backend, not the backend itself.
                gs.fatal(
                    _(
                        "model={model} names a backend, not a model: use "
                        "backend={model} (model= then picks one of its models, "
                        "e.g. backend=claude model=sonnet)"
                    ).format(model=model)
                )
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
            self.catalog,
            self.registry,
            self.matcher,
            self.validator,
            self.manual,
            commands=self.commands,
            shell_validator=self.shell_validator,
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
    from iai_shell import ShellStep, plan_item, validate_steps

    sys.stdout.flush()  # Show the plan before the tools' own messages.
    commands = [r.command for r in results]
    total = len(commands)
    for n, command in enumerate(commands, 1):
        gs.percent(n - 1, total, 1)
        check = validate_steps(
            [plan_item(command.to_dict())], session.validator, session.shell_validator
        )[0]
        if not check.valid:
            gs.warning(
                _("Step {n} <{cmd}> is no longer valid: {err}").format(
                    n=n, cmd=command.shell(), err="; ".join(check.errors)
                )
            )
            return False
        if check.risk in ("modify", "network") and not assume_yes:
            what = (
                _("uses the network")
                if check.risk == "network"
                else _("changes existing data or session state")
            )
            if not confirm(
                _("Step {n} {what}:\n  {cmd}\nRun it?").format(
                    n=n, what=what, cmd=command.shell()
                )
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
        if isinstance(command, ShellStep):
            cwd = session.context.workdir or os.getcwd()
            returncode = subprocess.run(
                ["bash", "-c", command.line], cwd=cwd, check=False
            ).returncode
            if returncode != 0:
                message = _("Step {n} <{cmd}> failed with exit code {rc}").format(
                    n=n, cmd=command.shell(), rc=returncode
                )
                if interactive:
                    gs.warning(message)
                    return False
                gs.fatal(message)
            session.registry.refresh()
            continue
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


def open_workspace(session, options, store, create):
    """Return the report workspace of this run, or fail with guidance."""
    from iai_report import Workspace

    workdir = options["workdir"] or session.context.workdir
    if not workdir and options["session"]:
        workdir = store.default_workdir(options["session"])
    if not workdir:
        gs.fatal(
            _(
                "Report pipelines are stored per session: give session=<name> or workdir=<dir>"
            )
        )
    workspace = Workspace(workdir)
    if not create and not workspace.exists:
        gs.fatal(_("No report pipeline in <{}>").format(workspace.root))
    session.context.workdir = workspace.root
    return workspace


def report_meta(options):
    from iai_report import default_author

    name, email = default_author()
    return {
        "author": options["author"] or name,
        "contact": options["contact"] or email,
        "organization": options["organization"],
        "classification": options["classification"],
        "session": options["session"],
    }


def run_pipeline(session, workspace, options, flags):
    """Run the stored pipeline step by step, repairing failing steps.

    :return: True when every step succeeded
    """
    from iai_agents import RepairAgent
    from iai_report import check_step_ownership, run_step

    steps = workspace.steps()
    start = options["from_step"]
    if start:
        ids = [s["id"] for s in steps]
        if start not in ids:
            gs.fatal(_("No step <{}>; steps: {}").format(start, ", ".join(ids)))
        steps = steps[ids.index(start) :]
    repairs = int(options["repairs"])
    repairer = None
    if session.copilot is not None and repairs > 0:
        repairer = session.copilot.pipeline_agent(RepairAgent)
    local = [workspace.step_file(s) for s in workspace.steps()] + [
        "scripts/run_step.sh",
        "scripts/00_env.sh",
    ]
    planned = set(workspace.known_outputs())
    total = len(steps)
    sys.stdout.flush()
    for n, step in enumerate(steps, 1):
        gs.percent(n - 1, total, 1)
        path = workspace.step_file(step)
        attempt = 0
        while True:
            code = workspace.read(path)
            if code is None:
                gs.fatal(_("Step script <{}> is missing").format(path))
            check = session.shell_validator.check(
                code,
                step["lang"],
                functions=("grass_run", "mm"),
                planned=set(planned),
                local_files=local,
                known_outputs=workspace.known_outputs() | planned,
            )
            check.errors += check_step_ownership(code, workspace.manifest["main"])
            if check.valid and not confirm_risk(check.risk, path, flags):
                workspace.save_manifest()
                return False
            if check.valid:
                gs.message(_("Step {n}/{t}: {path}...").format(n=n, t=total, path=path))
                before = mapset_maps(session)
                returncode, tail = run_step(workspace, step)
            else:
                returncode, tail = -1, "Validation failed:\n" + "\n".join(check.errors)
            session.registry.refresh()
            if returncode == 0:
                # What the step really created, whatever names its code builds.
                created = mapset_maps(session) - before
                planned.update(check.grass_outputs | created)
                workspace.record_outputs(check.grass_outputs | created)
                step["status"] = "done"
                break
            step["status"] = "failed"
            gs.warning(
                _("Step {id} failed:\n{tail}").format(
                    id=step["id"], tail="\n".join(tail.splitlines()[-12:])
                )
            )
            attempt += 1
            if repairer is None or attempt > repairs:
                workspace.save_manifest()
                gs.warning(
                    _("Stopped at step {id}; log in {log}").format(
                        id=step["id"],
                        log=workspace.path(
                            "logs/{}_{}.log".format(step["id"], step["name"])
                        ),
                    )
                )
                return False
            gs.message(
                _("Repairing step {id} (attempt {a} of {r})...").format(
                    id=step["id"], a=attempt, r=repairs
                )
            )
            fixed = repairer.repair_step(step, code, tail, planned)
            if fixed is None:
                workspace.save_manifest()
                gs.warning(_("No valid repair for step {}").format(step["id"]))
                return False
            if attempt == 1:
                workspace.write(path + ".orig", code)  # The script before repairs.
            workspace.write(path, fixed, executable=True)
        workspace.save_manifest()
    gs.percent(1, 1, 1)
    session.context.refresh()
    return True


def mapset_maps(session):
    """(type, name) of every map in the current mapset."""
    mapset = gs.gisenv()["MAPSET"]
    return {
        (e["type"], e["name"])
        for e in session.registry.entities
        if e["mapset"] == mapset
    }


def confirm_risk(risk, path, flags):
    """Ask before a step that modifies existing data or uses the network."""
    if risk not in ("modify", "network") or flags["y"]:
        return True
    what = (
        _("uses the network")
        if risk == "network"
        else _("removes or changes existing data")
    )
    if confirm(_("{path} {what}. Run it?").format(path=path, what=what)):
        return True
    gs.warning(
        _("Stopped before {path}; run i.ai with -y to allow steps that {what}.").format(
            path=path, what=what
        )
    )
    return False


def finalize_report(session, workspace, options, results_pass=True):
    """Write the results into the report and compile it, repairing LaTeX errors."""
    import re

    from iai_agents import gather_findings
    from iai_report import compile_report, render_tex

    manifest = workspace.manifest
    main = manifest["main"]
    agent = None
    if session.copilot is not None:
        agent = session.copilot.pipeline_agent()
    tex = workspace.read(main + ".tex") or ""
    match = re.search(r"% BODY START\n(.*)\n% BODY END", tex, re.DOTALL)
    current = {
        "body": match.group(1) if match else "",
        "bibtex": workspace.read("references.bib") or "",
    }
    meta = report_meta(options)
    outline = manifest.get(
        "outline", {"title": manifest["title"], "main": main, "sections": []}
    )
    if agent is not None and results_pass and match:
        gs.message(_("Writing the results into the report..."))
        report = agent.write_report(
            manifest.get("request", ""), outline, current, gather_findings(workspace)
        )
        if report.get("kind") == "report":
            current = {"body": report["body"], "bibtex": report["bibtex"]}
            render_tex(workspace, outline, current["body"], meta)
            workspace.write("references.bib", current["bibtex"])
        else:
            gs.warning(_("Results not written: {}").format(report.get("text")))
    gs.message(_("Compiling {}.tex...").format(main))
    ok, errors = compile_report(workspace, main)
    attempt = 0
    while not ok and agent is not None and match and attempt < int(options["repairs"]):
        attempt += 1
        gs.message(_("Repairing LaTeX errors (attempt {})...").format(attempt))
        report = agent.write_report(
            manifest.get("request", ""),
            outline,
            current,
            instruction="LaTeX fails with these errors; fix the body and BibTeX, change "
            "nothing else:\n" + "\n".join(errors),
        )
        if report.get("kind") != "report":
            break
        current = {"body": report["body"], "bibtex": report["bibtex"]}
        render_tex(workspace, outline, current["body"], meta)
        workspace.write("references.bib", current["bibtex"])
        ok, errors = compile_report(workspace, main)
    manifest["report"] = {"compiled": ok, "errors": errors}
    workspace.save_manifest()
    if ok:
        gs.message(_("Report: {}").format(workspace.path(main + ".pdf")))
    else:
        gs.warning(_("The report does not compile:\n{}").format("\n".join(errors)))
    return ok


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


def start_voice(options, session=None):
    """Start voice input on the GPU machine, or fail with what to fix."""
    from iai_voice import VoiceError, VoiceInput

    host = options["voice_host"] or os.environ.get("IAI_VOICE_HOST", "")
    user = options["voice_user"] or os.environ.get("IAI_VOICE_USER", "")
    if not sys.stdin.isatty():
        gs.fatal(_("Voice input needs a terminal (Enter starts and stops recording)"))
    try:
        voice = VoiceInput(
            language=options["voice_language"] or "en",
            host=host or None,
            user=user or None,
            message=gs.message,
            # Map names of the session, so they are heard as such.
            names=[e["name"] for e in session.registry.entities] if session else (),
        )
        voice.start()
    except VoiceError as error:
        gs.fatal(_("Voice input unavailable: {}").format(error))
    return voice


def read_request(voice, prompt="i.ai> "):
    """Read one request: typed, or spoken when voice input is on.

    With voice, an empty line starts recording and Enter stops it; the
    transcription is then put on the prompt line, to edit or send.
    """
    if voice is None:
        return input(prompt).strip()
    from iai_voice import VoiceError

    typed = input(prompt + "[Enter: speak] ").strip()
    if typed:
        return typed
    print(_("Recording... press Enter to stop."), flush=True)
    try:
        text = voice.listen(wait_start=None, wait_stop=input)
    except VoiceError as error:
        gs.warning(str(error))
        return ""
    if not text:
        gs.message(_("Nothing was heard."))
        return ""
    try:
        import readline

        readline.set_startup_hook(lambda: readline.insert_text(text))
        try:
            return input(prompt).strip()
        finally:
            readline.set_startup_hook()
    except ImportError:
        print(prompt + text)
        return text if confirm(_("Send this?")) else ""


# Short spoken or typed approvals of the pending plan (English and French).
AFFIRM = re.compile(
    r"^(yes|yeah|yep|ok|okay|sure|go|go ahead|do it|do that|run it|run that|"
    r"run the plan|run this plan|execute|execute it|execute the plan|proceed|"
    r"start it|open it|please do|oui|vas[ -]y|allez|lance|lance[ -]le|"
    r"ex[eé]cute|ex[eé]cute[ -]le|fais[ -]le|d'accord)( please| now| it)?$"
)


def is_approval(text):
    """True when a request only approves the pending plan ("Do it.")."""
    return bool(AFFIRM.match(re.sub(r"[^\w' -]", "", text.lower()).strip()))


def run_direct(session, text, flags):
    """Run a GRASS command or a !shell line typed in the dialog, validated."""
    from iai_shell import ShellStep, plan_item, validate_steps

    try:
        item = ShellStep(text[1:]) if text.startswith("!") else plan_item(text)
    except ValueError as error:
        gs.warning(str(error))
        return
    result = validate_steps([item], session.validator, session.shell_validator)[0]
    if not result.valid:
        gs.warning(
            _("Not run: {}").format("; ".join(result.errors + result.suggestions))
        )
        return
    execute_plan(session, [result], flags["y"], interactive=True)


def interactive_loop(session, flags, output_format, store, session_name, voice=None):
    gs.message(
        _(
            "i.ai interactive dialog. Ask in plain words; each plan is offered to run. "
            "Say or type 'do it' to run the last plan. A line starting with a GRASS "
            "tool (d.mon start=wx0) runs it directly, a line starting with ! runs a "
            "shell command. Commands: /run, /plan, /context, /reset, /exit."
        )
    )
    last = None
    pending = False  # The last plan has not run yet.
    while True:
        try:
            text = read_request(voice)
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
            last, pending = None, False
            gs.message(_("Dialog history cleared."))
            continue
        if text == "/plan":
            if last and last.get("plan"):
                print_result({"kind": "plan", "plan": last["plan"]}, output_format)
            else:
                gs.message(_("No plan yet."))
            continue
        if text == "/run" or (pending and is_approval(text)):
            if last and last.get("plan"):
                execute_plan(session, last["plan"], flags["y"], interactive=True)
                pending = False
            else:
                gs.message(_("No plan to run."))
            continue
        first = text.split()[0]
        if text.startswith("!") or session.catalog.exists(first):
            run_direct(session, text, flags)
            continue
        result = session.copilot.run(text)
        print_result(result, output_format)
        if result.get("plan"):
            last, pending = result, True
            # The user is at the terminal: every plan is offered to run.
            if confirm(_("Run this plan?")):
                execute_plan(session, result["plan"], flags["y"], interactive=True)
                pending = False
        if session_name:
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

    store = SessionStore()

    if operation in ("run", "compile"):
        session = Session(
            options, need_backend=operation == "run" and int(options["repairs"]) > 0
        )
        if options["session"]:
            store.load(options["session"], session.context)
        workspace = open_workspace(session, options, store, create=False)
        if session.copilot is not None:
            session.copilot.workspace_factory = lambda: (
                workspace,
                report_meta(options),
            )
        ok = True
        if operation == "run":
            if not flags["e"]:
                gs.fatal(_("operation=run executes the pipeline scripts: add -e"))
            ok = run_pipeline(session, workspace, options, flags)
        if ok:
            ok = finalize_report(
                session, workspace, options, results_pass=operation == "run"
            )
        if options["session"]:
            store.save(options["session"], session.context)
        session.catalog.save()
        return 0 if ok else 1

    voice_mode = options["input"] == "voice"
    if not options["query"] and not flags["i"] and not voice_mode:
        gs.fatal(_("Give a query, use -i for an interactive dialog, or input=voice"))

    session = Session(options)
    voice = start_voice(options, session) if voice_mode else None
    try:
        return ask(session, options, flags, store, voice)
    finally:
        if voice is not None:
            voice.close()


def ask(session, options, flags, store, voice):
    """Answer the query or run the dialog; the ask and report operations."""
    operation = options["operation"]
    output_format = options["format"]
    if voice is not None and not options["query"] and not flags["i"]:
        # One spoken request, edited or sent from the prompt line.
        while not options["query"]:
            try:
                options["query"] = read_request(voice)
            except (EOFError, KeyboardInterrupt):
                print()
                return 1
    if options["session"] and store.load(options["session"], session.context):
        gs.verbose(_("Continuing session <{}>").format(options["session"]))
    if options["session"] or options["workdir"]:
        workspace_cache = {}

        def factory():
            if "ws" not in workspace_cache:
                workspace_cache["ws"] = open_workspace(
                    session, options, store, create=True
                )
            return workspace_cache["ws"], report_meta(options)

        session.copilot.workspace_factory = factory

    if options["query"]:
        intent = "pipeline" if operation == "report" else None
        result = session.copilot.run(options["query"], intent=intent)
        print_result(result, output_format)
        if options["session"]:
            store.save(options["session"], session.context)
        if result["kind"] == "error" and not flags["i"]:
            sys.exit(1)
        if result.get("plan") and flags["e"]:
            execute_plan(session, result["plan"], flags["y"], interactive=flags["i"])
        elif result.get("plan") and not flags["i"]:
            gs.message(_("Nothing was run; add -e to execute the plan."))
        if result["kind"] == "pipeline" and flags["e"]:
            workspace = session.copilot.workspace_factory()[0]
            if run_pipeline(session, workspace, options, flags):
                finalize_report(session, workspace, options)
            if options["session"]:
                store.save(options["session"], session.context)

    if flags["i"]:
        interactive_loop(
            session, flags, output_format, store, options["session"], voice=voice
        )
    session.catalog.save()
    return 0


if __name__ == "__main__":
    options, flags = gs.parser()
    sys.exit(main())
