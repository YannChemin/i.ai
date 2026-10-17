"""
MODULE:    i.ai
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Agents that route, clarify, answer, plan and write report pipelines.
SPDX-License-Identifier: Unlicense

The design follows GeoCopilot (https://github.com/yosgi/GeoCopilot): an
intent agent classifies each request; unclear requests go to a
clarification agent; questions go to a query agent; tasks go to an
execution agent that returns a plan of GRASS commands and shell lines;
end-to-end requests go to a pipeline agent that writes a scripted report
workspace. All agents work in a loop of JSON steps: each step either
calls one read-only tool or delivers a result (answer, plan, outline,
script, report). Every result is validated, and validation errors go back
to the agent until the result is valid or the step budget is spent.
"""

import datetime
import difflib
import json
import os
import re

import grass.script as gs

from iai_llm import BackendError
from iai_shell import plan_item, validate_steps

INTENTS = ["execute", "query", "pipeline", "clarify", "help", "chat"]

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["intent", "confidence"],
}

CLARIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {"type": "array", "items": {"type": "string"}},
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["questions"],
}

STEP_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "module": {"type": "string"},
        "params": {"type": "object"},
        "flags": {"type": "string"},
        "shell": {"type": "string"},
        "purpose": {"type": "string"},
    },
}

OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "short_title": {"type": "string"},
        "main": {"type": "string"},
        "docref": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "meta": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
        },
        "env": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "comment": {"type": "string"},
                },
            },
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "lang": {"type": "string", "enum": ["bash", "python"]},
                    "grass": {"type": "boolean"},
                    "keep": {"type": "boolean"},
                    "purpose": {"type": "string"},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "outputs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "brief": {"type": "string"},
                },
            },
        },
    },
}


def step_schema(actions):
    return {
        "type": "object",
        "properties": {
            "thought": {"type": "string"},
            "action": {"type": "string", "enum": list(actions)},
            "tool": {"type": "string"},
            "args": {"type": "object"},
            "answer": {"type": "string"},
            "plan": {"type": "array", "items": STEP_ITEM_SCHEMA},
            "questions": {"type": "array", "items": {"type": "string"}},
            "outline": OUTLINE_SCHEMA,
            "code": {"type": "string"},
            "body": {"type": "string"},
            "bibtex": {"type": "string"},
        },
        "required": ["action"],
    }


ROLE = """You are i.ai, an expert assistant for GRASS GIS running inside the \
user's live GRASS session on Linux. You know remote sensing, hydrology, terrain \
analysis, vector analysis, temporal data, the Linux command line and LaTeX."""

GRASS_RULES = """GRASS rules:
- Every GRASS tool installed in this session is available to you, core tools and \
addons alike. Find tools with search_modules or list_modules; never invent a tool.
- Before you use a tool, read its interface with module_help unless you already \
did in this conversation, and use only parameters, flags and option values it lists.
- Use existing map names exactly as they exist (list_maps, match_map). If the user's \
words do not identify a map, ask.
- Name new maps in lowercase snake_case, descriptive, never an existing name.
- Do not change the computational region or the mask unless the user asks for it \
or the task cannot be done otherwise; say why.
- For r.mapcalc use the expression parameter, e.g. "ndvi = float(b8 - b4) / (b8 + b4)".
- Every command-line tool on PATH is available too (search_commands, command_help). \
Use GRASS for GIS processing in the session; use command-line tools for what GRASS \
does not do (downloads, files outside GRASS, PDF and image handling, LaTeX)."""

PROTOCOL = """Work in steps. Reply to every message with one JSON object:
{{"thought": "<one short sentence>", "action": "<one of: {actions}>", ...}}
- action "tool": also give "tool" (a tool name below) and "args" (object). You \
will get the result in the next message. One tool per step.
{extra}- action "clarify": also give "questions" (list) when you cannot go on \
without the user's answer.

Tools:
{tools}"""

ANSWER_LINE = (
    '- action "answer": also give "answer", plain text for a terminal, concise, '
    "citing commands where useful.\n"
)
PLAN_LINE = (
    '- action "plan": also give "plan", an ordered list of steps. A GRASS step is '
    '{"module": "r.tool", "params": {"key": "value"}, "flags": "letters", "purpose": '
    '"why"}; a command-line step is {"shell": "gdalwarp -t_srs EPSG:32633 a.tif '
    'b.tif", "purpose": "why"}. Never add --overwrite to GRASS steps. Later steps may '
    "use outputs of earlier steps. The plan is validated; you get any errors back to "
    'fix. Put a short explanation for the user in "answer".\n'
)

SCRIPT_RULES = """Script rules:
- bash: start with "#!/usr/bin/env bash", "set -euo pipefail" and \
'source "$(dirname "$0")/00_env.sh"'. Run GRASS tools only through grass_run \
(grass_run r.slope.aspect elevation=dem slope=slope --overwrite), or for many \
tools in one session: grass_run bash -s <<EOF ... EOF (escape \\$ for variables of \
the inner shell).
- python: start with "#!/usr/bin/env python3" and a docstring; read paths from \
os.environ["REPORT_DIR"]. Steps with grass=true run inside the GRASS session: \
import grass.script as gs and use gs.run_command, gs.read_command, gs.parse_command.
- Paths: write only inside $REPORT_DIR (figures/, maps/, tables/, data/) and the \
GRASS mapset. Never use absolute paths into the user's home except data inputs.
- GRASS outputs: new map names, with --overwrite (bash) or overwrite=True (Python) \
so the step can run again. Never write over maps the pipeline did not create.
- Region: never leave the region changed. Python: with gs.RegionManager(...). \
bash: g.region save=iai_before --overwrite at the start, trap 'g.region \
region=iai_before' EXIT, and restore it.
- Maps for the report: GRASS display tools with the cairo driver: \
export GRASS_RENDER_IMMEDIATE=cairo GRASS_RENDER_FILE=$REPORT_DIR/maps/x.png \
GRASS_RENDER_WIDTH=1400 GRASS_RENDER_HEIGHT=1000 GRASS_RENDER_TRANSPARENT=FALSE, \
then d.rast, d.vect, d.legend, d.barscale (projected CRS) or d.grid, d.northarrow.
- Figures: Python matplotlib to figures/*.pdf, no title in the figure (the caption \
is in LaTeX), readable fonts, dpi 200 for rasters.
- Diagrams: Graphviz dot sources in diagrams/*.dot, rendered with dot -Tpdf to \
figures/*.pdf.
- Tables: write LaTeX tabular files (booktabs: \\toprule \\midrule \\bottomrule) to \
tables/*.tex, and the numbers also as CSV in data/.
- Idempotent: skip downloads already present; make each step safe to run again.
- Progress: in long bash loops call mm set "$SLOT" "$i" --of "$n" -n "name"; in \
GRASS Python loops call gs.percent(i, n, 2).
- Use only installed commands and Python modules (check with search_commands, \
command_help, run_shell_readonly). No sudo, no package installs; if something \
essential is missing, say so in a comment and fail with a clear message."""

REPORT_RULES = """Report rules:
- Write only the LaTeX body: sections, text, figures, tables. i.ai adds the \
preamble, the title block, the table of contents, the run book and the bibliography.
- Start with \\section*{Executive summary} and \\addcontentsline{toc}{section}{Executive summary}.
- Include every pipeline figure or map with \\figorpending{path}{width}{what it shows} \
inside a figure environment with \\caption and \\label, and every pipeline table \
with \\taborpending{tables/x.tex}{what it shows} inside a table environment. Only \
include paths that a step declares as output.
- Name GRASS tools with \\module{r.slope.aspect}. Use \\SI{}{} for units.
- Results that do not exist yet go in \\begin{pending}[Pending] ... \\end{pending} \
boxes; never invent numbers.
- Cite with \\cite{key}. Give the BibTeX in "bibtex": only peer-reviewed works or \
official documentation you are sure exist, each with its doi field when it has one. \
Check each DOI with check_doi first; i.ai verifies every DOI at Crossref and \
rejects the report if one is unknown or its title does not match."""


class Agent:
    """Base agent: a backend, the shared context and the tool registry."""

    def __init__(self, backend, context, tools):
        self.backend = backend
        self.context = context
        self.tools = tools

    def ask(self, system, messages, schema):
        return self.backend.complete_json(system, messages, schema)


class IntentAgent(Agent):
    """Classify a request: execute, query, pipeline, clarify, help or chat."""

    def run(self, text):
        system = (
            ROLE + "\n\nClassify the user's latest message.\n"
            "- execute: the user wants something done to data now (import, compute, "
            "classify, export, convert...).\n"
            "- query: the user asks for information, about the data, the session, "
            "a method or a tool.\n"
            "- pipeline: the user wants an end-to-end workflow written as scripts, "
            "a report, or a document produced from data (several steps, maps, "
            "figures, tables, LaTeX).\n"
            "- clarify: the request is too ambiguous to act on or answer, even "
            "knowing the session below.\n"
            "- help: the user asks how to use i.ai or GRASS in general.\n"
            "- chat: greetings, thanks, feedback.\n"
            "Give your confidence from 0 to 1 and a short reason.\n\n"
            "Session:\n" + self.context.for_prompt()
        )
        messages = self.context.history[-6:] + [{"role": "user", "content": text}]
        result = self.ask(system, messages, INTENT_SCHEMA)
        if result.get("intent") not in INTENTS:
            result["intent"] = "query"
        # A model that gives no confidence has not signalled doubt.
        try:
            result["confidence"] = float(result.get("confidence", 1.0))
        except (TypeError, ValueError):
            result["confidence"] = 1.0
        return result


class ClarificationAgent(Agent):
    """Ask what is missing, and suggest requests that could run as they are."""

    def run(self, text, reason=""):
        system = (
            ROLE
            + "\n\nThe user's request is ambiguous or incomplete for this session. "
            "Ask the fewest questions (one or two) needed to act, and give three "
            "complete requests the user could send instead, using real map names "
            "from the session.\n\nSession:\n" + self.context.for_prompt()
        )
        prompt = (
            text if not reason else "{}\n\n(Why it is unclear: {})".format(text, reason)
        )
        messages = self.context.history[-6:] + [{"role": "user", "content": prompt}]
        result = self.ask(system, messages, CLARIFY_SCHEMA)
        return {
            "kind": "clarify",
            "questions": [q for q in result.get("questions", []) if q][:3],
            "suggestions": [s for s in result.get("suggestions", []) if s][:3],
        }


class LoopAgent(Agent):
    """Agent that works in tool-calling steps until it delivers a result."""

    actions = ("tool", "answer", "clarify")
    task = ""

    def __init__(self, backend, context, tools, validator, max_steps=10):
        super().__init__(backend, context, tools)
        self.validator = validator
        self.max_steps = max_steps
        self.trace = []

    def system_prompt(self, extra_rules=""):
        extra = ""
        if "answer" in self.actions:
            extra += ANSWER_LINE
        if "plan" in self.actions:
            extra += PLAN_LINE
        extra += self.result_lines()
        parts = [
            ROLE,
            self.task,
            GRASS_RULES,
            extra_rules,
            PROTOCOL.format(
                actions=", ".join(self.actions),
                extra=extra,
                tools=self.tools.describe(),
            ),
            "Session:\n" + self.context.for_prompt(),
        ]
        return "\n\n".join(p for p in parts if p)

    def result_lines(self):
        """Protocol lines for the results specific to an agent."""
        return ""

    def loop(self, system, messages, handle, max_steps=None):
        """Run the step loop.

        :param handle: function(reply) returning ("done", result) or
            ("retry", feedback) for a non-tool reply, or None if the action is
            not one it handles
        :return: the result, or an error dict
        """
        schema = step_schema(self.actions)
        max_steps = max_steps or self.max_steps
        for step in range(1, max_steps + 1):
            reply = self.ask(system, messages, schema)
            action = reply.get("action")
            messages.append({"role": "assistant", "content": json.dumps(reply)})
            if reply.get("thought"):
                gs.verbose(_("Step {}: {}").format(step, reply["thought"]))
            if action == "tool":
                name = reply.get("tool", "")
                args = reply.get("args") or {}
                gs.verbose(_("Tool {} {}").format(name, json.dumps(args)[:300]))
                output = self.tools.call(name, args)
                self.trace.append({"tool": name, "args": args})
                messages.append(
                    {
                        "role": "user",
                        "content": "Result of {}:\n{}".format(name, output),
                    }
                )
                continue
            if action == "clarify" and reply.get("questions"):
                return {
                    "kind": "clarify",
                    "questions": reply["questions"][:3],
                    "suggestions": [],
                }
            outcome = handle(reply)
            if outcome is not None:
                status, value = outcome
                if status == "done":
                    return value
                if step == max_steps:
                    return {"kind": "error", "text": value}
                messages.append({"role": "user", "content": value})
                continue
            messages.append(
                {
                    "role": "user",
                    "content": "That step was incomplete. Reply with one JSON object "
                    "using one of the actions: {}.".format(", ".join(self.actions)),
                }
            )
        return {
            "kind": "error",
            "text": _("No result after {} steps; try a more specific request.").format(
                max_steps
            ),
        }

    def run(self, text):
        self.trace = []
        messages = list(self.context.history) + [{"role": "user", "content": text}]
        return self.loop(self.system_prompt(), messages, self.handle)

    def handle(self, reply):
        action = reply.get("action")
        if action == "plan" and "plan" in self.actions:
            outcome = self._check_plan(reply)
            if outcome["kind"] == "plan":
                return "done", outcome
            return "retry", outcome["feedback"]
        if action == "answer" and reply.get("answer"):
            return "done", {"kind": "answer", "text": reply["answer"]}
        return None

    def _check_plan(self, reply):
        items, errors = [], []
        for i, item in enumerate(reply.get("plan") or [], 1):
            try:
                items.append(plan_item(item))
            except ValueError as error:
                errors.append("Step {}: {}".format(i, error))
        if not items and not errors:
            errors.append("The plan is empty.")
        results = (
            validate_steps(items, self.validator, self.tools.shell_validator)
            if not errors
            else []
        )
        for i, result in enumerate(results, 1):
            for error in result.errors:
                errors.append(
                    "Step {} ({}): {}".format(i, result.command.module, error)
                )
            for suggestion in result.suggestions:
                errors.append("  suggestion: " + suggestion)
        if errors:
            feedback = (
                "The plan is not valid; fix it and send the whole plan again:\n"
                + "\n".join(errors)
            )
            return {"kind": "invalid", "feedback": feedback, "text": feedback}
        return {"kind": "plan", "text": reply.get("answer", ""), "plan": results}


class QueryAgent(LoopAgent):
    """Answer questions about the data, the session, methods and tools."""

    actions = ("tool", "answer", "clarify")
    task = (
        "Answer the user's question. Look things up with the tools instead of "
        "guessing: map metadata, statistics through run_readonly, files through "
        "run_shell_readonly, tool interfaces and manuals. When the answer involves "
        "commands, give exact, valid commands for the installed tools and real names."
    )


class ExecutionAgent(LoopAgent):
    """Turn a task into a validated plan of GRASS commands and shell lines."""

    actions = ("tool", "plan", "answer", "clarify")
    task = (
        "The user wants a task done. Inspect the data and the tools you need, then "
        "propose a plan of GRASS commands and command-line steps that does the task. "
        "The plan only runs if the user allows it. If no command is needed, answer "
        "instead."
    )


class PipelineAgent(LoopAgent):
    """Write a scripted report workspace: outline, step scripts, LaTeX body."""

    actions = ("tool", "outline", "clarify")
    task = (
        "The user wants an end-to-end pipeline from data to a report. First inspect "
        "the data and the tools you need, then give the outline of the pipeline: "
        "numbered steps (each one script, bash or python) from data preparation to "
        "the figures, maps and tables of the report, and the report sections."
    )

    def __init__(
        self, backend, context, tools, validator, workspace, meta, max_steps=12
    ):
        super().__init__(backend, context, tools, validator, max_steps)
        self.workspace = workspace
        self.meta = meta

    def result_lines(self):
        if self.actions == ("tool", "outline", "clarify"):
            return (
                '- action "outline": also give "outline": {"title", "subtitle", '
                '"short_title", "main" (file stem of the .tex, e.g. SF_Terrain), '
                '"docref", "keywords": [...], "meta": [{"label": "Study area", '
                '"value": "..."}, {"label": "Data", ...}], "env": [{"name": "AOI_N", '
                '"value": "...", "comment": "..."}] (study settings shared by all '
                'steps), "steps": [{"id": "01", "name": "setup_region", "lang": '
                '"bash"|"python", "grass": true|false (python steps that call GRASS), '
                '"keep": true to keep an existing step unchanged, "purpose", "inputs": '
                '[...], "outputs": ["figures/x.pdf", "maps/y.png", "tables/z.tex", '
                '"GRASS map names"]}], "sections": [{"title", "brief"}]}.\n'
            )
        if self.actions == ("tool", "script"):
            return '- action "script": also give "code", the full script text.\n'
        return (
            '- action "report": also give "body" (LaTeX body) and "bibtex" (BibTeX '
            "entries for every \\cite).\n"
        )

    def run(self, text):
        from iai_report import validate_outline

        self.trace = []
        self.actions = ("tool", "outline", "clarify")
        existing = ""
        if self.workspace.exists:
            existing = (
                "\n\nThe workspace already has this pipeline; revise it as asked "
                "and mark unchanged steps with keep=true:\n"
                + json.dumps(
                    {
                        k: self.workspace.manifest.get(k)
                        for k in ("title", "main", "steps", "sections")
                    },
                    indent=1,
                )
            )
        messages = list(self.context.history) + [
            {"role": "user", "content": text + existing}
        ]

        def handle_outline(reply):
            outline = reply.get("outline") if reply.get("action") == "outline" else None
            if not outline:
                return None
            errors = validate_outline(outline)
            if errors:
                return "retry", "The outline is not valid; fix it:\n" + "\n".join(
                    errors
                )
            return "done", {"kind": "outline", "outline": outline}

        result = self.loop(self.system_prompt(), messages, handle_outline)
        if result["kind"] != "outline":
            return result
        return self.build(text, result["outline"])

    def build(self, request, outline):
        """Write every step script and the report into the workspace."""
        from iai_report import (
            RUN_STEP,
            render_env,
            render_makefile,
            render_readme,
            render_runbook,
            render_tex,
        )

        ws = self.workspace
        ws.create()
        previous = {s["id"]: s for s in ws.steps()}
        render_env(ws, outline, self.context)
        ws.write("scripts/run_step.sh", RUN_STEP, executable=True)
        ws.manifest.update(
            {
                "title": outline["title"],
                "main": outline["main"],
                "outline": outline,
                "request": request,
                "steps": [],
            }
        )
        ws.manifest.setdefault(
            "created", datetime.datetime.now().isoformat(timespec="seconds")
        )
        planned = set(ws.known_outputs())
        written = []
        total = len(outline["steps"])
        for n, spec in enumerate(outline["steps"], 1):
            step = {
                "id": str(spec["id"]),
                "name": spec["name"],
                "lang": spec["lang"],
                "grass": bool(spec.get("grass")),
                "purpose": spec.get("purpose", ""),
                "inputs": spec.get("inputs") or [],
                "outputs": spec.get("outputs") or [],
            }
            path = ws.step_file(step)
            old = previous.get(step["id"])
            if spec.get("keep") and old and ws.read(path):
                gs.message(_("Keeping step {}: {}").format(step["id"], path))
                ws.manifest["steps"].append(step)
                written.append((step, ws.read(path)))
                continue
            gs.message(
                _("Writing step {n}/{t}: {path}...").format(n=n, t=total, path=path)
            )
            code, check = self.write_step(request, outline, step, written, planned)
            if code is None:
                return {"kind": "error", "text": check}
            ws.write(path, code, executable=True)
            planned.update(check.grass_outputs)
            step["risk"] = check.risk
            ws.manifest["steps"].append(step)
            written.append((step, code))
        ws.save_manifest()
        render_makefile(ws)
        render_runbook(ws)
        gs.message(_("Writing the report..."))
        report = self.write_report(request, outline)
        if report["kind"] == "error":
            ws.save_manifest()
            return report
        render_tex(ws, outline, report["body"], self.meta)
        ws.write("references.bib", report["bibtex"])
        from iai_report import bib_entries, compile_report

        ok, errors = compile_report(ws, outline["main"])
        unverified = [
            (key, title)
            for key, title in bib_entries(report["bibtex"])
            if key not in report.get("verified", set())
        ]
        render_readme(ws, self.meta.get("session"), unverified)
        ws.manifest["report"] = {"compiled": ok, "errors": errors}
        ws.save_manifest()
        return {
            "kind": "pipeline",
            "text": self.summary(outline, ok, errors),
            "workspace": ws.root,
            "compiled": ok,
        }

    def summary(self, outline, ok, errors):
        ws = self.workspace
        lines = [
            'Pipeline "{}" written to {}'.format(outline["title"], ws.root),
            "",
            "Steps:",
        ]
        for step in ws.steps():
            lines.append(
                "  {}  {}  [{}]  {}".format(
                    step["id"],
                    ws.step_file(step),
                    step.get("risk", "create"),
                    step["purpose"],
                )
            )
        lines += [
            "",
            "Report: {}.tex ({})".format(
                outline["main"],
                "compiles, pipeline outputs shown as pending boxes"
                if ok
                else "does not compile yet: " + "; ".join(errors[:3]),
            ),
            "Run it: cd {} && make   (or i.ai operation=run -e)".format(ws.root),
        ]
        return "\n".join(lines)

    def _step_context(self, request, outline, written, extra=""):
        ws = self.workspace
        steps = "\n".join(
            "- {} {} (lang {}, grass {}): {} -> outputs {}".format(
                s["id"],
                ws.step_file({"id": s["id"], "name": s["name"], "lang": s["lang"]}),
                s["lang"],
                bool(s.get("grass")),
                s.get("purpose", ""),
                ", ".join(s.get("outputs") or []),
            )
            for s in outline["steps"]
        )
        budget, shown = 24000, []
        for step, code in reversed(written):
            if budget <= 0:
                break
            chunk = code[:budget]
            budget -= len(chunk)
            shown.append("### {}\n{}".format(ws.step_file(step), chunk))
        return (
            "User request:\n{}\n\nPipeline: {}\n\nAll steps:\n{}\n\nscripts/00_env.sh:\n{}"
            "\n\nScripts already written:\n{}{}".format(
                request,
                outline["title"],
                steps,
                ws.read("scripts/00_env.sh"),
                "\n\n".join(reversed(shown)) or "(none)",
                extra,
            )
        )

    def write_step(self, request, outline, step, written, planned):
        """Have the model write one step until it validates.

        :return: (code, ScriptCheck) or (None, error text)
        """
        self.actions = ("tool", "script")
        ws = self.workspace
        local = [ws.step_file(s) for s in outline["steps"] if s.get("name")] + [
            "scripts/run_step.sh",
            "scripts/00_env.sh",
        ]
        prompt = self._step_context(request, outline, written) + (
            "\n\nWrite {} now: {}\nlang {}, grass {}, inputs {}, outputs {}.".format(
                ws.step_file(step),
                step["purpose"],
                step["lang"],
                step["grass"],
                ", ".join(step["inputs"]) or "-",
                ", ".join(step["outputs"]) or "-",
            )
        )
        task = "Write one step script of a report pipeline."
        system = "\n\n".join(
            [ROLE, task, GRASS_RULES, SCRIPT_RULES, self._protocol(), self._session()]
        )
        holder = {}

        def handle_script(reply):
            code = reply.get("code") if reply.get("action") == "script" else None
            if not code:
                return None
            check = self.tools.shell_validator.check(
                code,
                step["lang"],
                functions=("grass_run", "mm"),
                planned=set(planned),
                local_files=local,
                known_outputs=ws.known_outputs() | planned,
            )
            if not check.valid:
                return (
                    "retry",
                    "The script is not valid; fix it and send it whole:\n"
                    + "\n".join(check.errors),
                )
            holder["check"] = check
            return "done", code

        result = self.loop(system, [{"role": "user", "content": prompt}], handle_script)
        if isinstance(result, dict):
            return None, "Step {}: {}".format(step["id"], result.get("text") or result)
        return result, holder["check"]

    def write_report(
        self, request, outline, current=None, findings="", instruction=None
    ):
        """Have the model write (or rewrite) the report body until it is valid."""
        from iai_report import bib_entries, check_report_body

        self.actions = ("tool", "report")
        ws = self.workspace
        outputs = set()
        for step in ws.steps():
            outputs.update(o for o in step.get("outputs") or [] if "/" in o)
        sections = "\n".join(
            "- {}: {}".format(s.get("title", ""), s.get("brief", ""))
            for s in outline["sections"]
        )
        steps = "\n".join(
            "- {} {}: {} -> {}".format(
                s["id"],
                ws.step_file(s),
                s["purpose"],
                ", ".join(s.get("outputs") or []),
            )
            for s in ws.steps()
        )
        prompt = (
            "User request:\n{}\n\nReport: {} - {}\nSections:\n{}\n\nPipeline steps:\n{}\n\n"
            "Study settings (scripts/00_env.sh):\n{}".format(
                request,
                outline["title"],
                outline.get("subtitle", ""),
                sections,
                steps,
                ws.read("scripts/00_env.sh"),
            )
        )
        if current and instruction:
            prompt += "\n\nCurrent body:\n{}\n\nCurrent BibTeX:\n{}\n\n{}".format(
                current["body"], current["bibtex"], instruction
            )
        elif current:
            prompt += (
                "\n\nCurrent body:\n{}\n\nCurrent BibTeX:\n{}\n\nThe pipeline has run. "
                "Its outputs:\n{}\n\nRewrite the body with the real results: replace the "
                "pending boxes of results by the numbers and findings above, update the "
                "Executive summary, keep everything else.".format(
                    current["body"], current["bibtex"], findings
                )
            )
        else:
            prompt += "\n\nWrite the report body now."
        system = "\n\n".join(
            [
                ROLE,
                "Write the body of a scientific report in LaTeX.",
                REPORT_RULES,
                self._protocol(),
                self._session(),
            ]
        )
        holder = {}

        def handle_report(reply):
            body = reply.get("body") if reply.get("action") == "report" else None
            if not body:
                return None
            bibtex = reply.get("bibtex") or ""
            errors = check_report_body(body, bibtex, ws, outputs)
            verified, doi_errors = verify_dois(bibtex, self.tools)
            errors += doi_errors
            if errors:
                return (
                    "retry",
                    "The report is not valid; fix it and send body and bibtex again:\n"
                    + "\n".join(errors),
                )
            holder["verified"] = verified
            return "done", {"kind": "report", "body": body, "bibtex": bibtex}

        result = self.loop(
            system,
            [{"role": "user", "content": prompt}],
            handle_report,
            max_steps=self.max_steps + 4,
        )
        if result.get("kind") == "report":
            result["verified"] = holder.get("verified", set())
            keys = {k for k, _ in bib_entries(result["bibtex"])}
            result["verified"] &= keys
        return result

    def _protocol(self):
        return PROTOCOL.format(
            actions=", ".join(self.actions),
            extra=self.result_lines(),
            tools=self.tools.describe(),
        )

    def _session(self):
        return "Session:\n" + self.context.for_prompt()


class RepairAgent(PipelineAgent):
    """Fix a step script from its error log, or the report from LaTeX errors."""

    def repair_step(self, step, code, log_tail, planned):
        """Return a fixed script that validates, or None."""
        self.actions = ("tool", "script")
        ws = self.workspace
        outline = ws.manifest.get(
            "outline", {"steps": ws.steps(), "title": ws.manifest.get("title", "")}
        )
        local = [ws.step_file(s) for s in ws.steps()] + [
            "scripts/run_step.sh",
            "scripts/00_env.sh",
        ]
        prompt = self._step_context(
            ws.manifest.get("request", ""),
            outline,
            [],
            "\n\nThe step {} failed. Its script:\n{}\n\nEnd of its log:\n{}\n\nFind the "
            "cause (inspect files and tools if needed) and send the whole fixed "
            "script.".format(ws.step_file(step), code, log_tail),
        )
        system = "\n\n".join(
            [
                ROLE,
                "Fix a failing step script of a report pipeline.",
                GRASS_RULES,
                SCRIPT_RULES,
                self._protocol(),
                self._session(),
            ]
        )

        def handle_script(reply):
            fixed = reply.get("code") if reply.get("action") == "script" else None
            if not fixed:
                return None
            check = self.tools.shell_validator.check(
                fixed,
                step["lang"],
                functions=("grass_run", "mm"),
                planned=set(planned),
                local_files=local,
                known_outputs=ws.known_outputs() | planned,
            )
            if not check.valid:
                return "retry", "The script is not valid:\n" + "\n".join(check.errors)
            return "done", fixed

        result = self.loop(system, [{"role": "user", "content": prompt}], handle_script)
        return None if isinstance(result, dict) else result


def verify_dois(bibtex, tools):
    """Check every DOI of a BibTeX text at Crossref.

    :return: (keys whose DOI matched, list of errors)
    """
    verified, errors = set(), []
    for match in re.finditer(
        r"@\w+\s*\{\s*([^,\s]+)\s*,(.*?)(?=\n@|\Z)", bibtex, re.DOTALL
    ):
        key, fields = match.group(1), match.group(2)
        doi = re.search(r"doi\s*=\s*[{\"]([^}\"]+)[}\"]", fields, re.IGNORECASE)
        if not doi:
            continue
        title = re.search(r"\btitle\s*=\s*[{\"](.+?)[}\"]\s*,?\s*\n", fields, re.DOTALL)
        found = (
            tools.call("check_doi", {"doi": doi.group(1)})
            if "check_doi" in tools.tools
            else ""
        )
        if not found:
            continue
        try:
            record = json.loads(found)
        except ValueError:
            errors.append("{}: {}".format(key, found))
            continue
        if title:
            wanted = re.sub(
                r"[^a-z0-9 ]", "", re.sub(r"[{}\\]", "", title.group(1)).lower()
            )
            registered = re.sub(r"[^a-z0-9 ]", "", (record.get("title") or "").lower())
            ratio = difflib.SequenceMatcher(None, wanted, registered).ratio()
            if ratio < 0.8:
                errors.append(
                    '{}: DOI {} is registered as "{}", not "{}"; fix or remove the '
                    "entry".format(
                        key, doi.group(1), record.get("title"), title.group(1).strip()
                    )
                )
                continue
        verified.add(key)
    return verified, errors


class Copilot:
    """Route each request to the right agent and keep the dialog history."""

    def __init__(self, backend, context, tools, validator, max_steps=10, threshold=0.4):
        self.backend = backend
        self.context = context
        self.tools = tools
        self.validator = validator
        self.max_steps = max_steps
        self.threshold = threshold
        self.intent_agent = IntentAgent(backend, context, tools)
        self.clarification_agent = ClarificationAgent(backend, context, tools)
        self.query_agent = QueryAgent(backend, context, tools, validator, max_steps)
        self.execution_agent = ExecutionAgent(
            backend, context, tools, validator, max_steps
        )
        self.workspace_factory = None  # Set by i.ai: returns (Workspace, meta).

    def pipeline_agent(self, cls=PipelineAgent):
        if self.workspace_factory is None:
            raise BackendError(
                "Report pipelines need a place to live: give session=<name> or workdir=<dir>"
            )
        workspace, meta = self.workspace_factory()
        self.tools.enable_report_tools(workspace)
        return cls(
            self.backend, self.context, self.tools, self.validator, workspace, meta,
            max_steps=max(self.max_steps, 12),
        )  # fmt: skip

    def run(self, text, intent=None):
        """Handle one request.

        :param str text: The user's message
        :param str intent: Force a route (execute, query or pipeline)
        :return: dict with kind answer, plan, pipeline, clarify or error
        """
        try:
            if intent:
                route = {"intent": intent, "confidence": 1.0, "reason": "forced"}
            else:
                route = self.intent_agent.run(text)
            gs.verbose(
                _("Intent: {intent} ({confidence:.2f}) {reason}").format(
                    intent=route["intent"],
                    confidence=route["confidence"],
                    reason=route.get("reason", ""),
                )
            )
            if route["intent"] == "clarify" or route["confidence"] < self.threshold:
                result = self.clarification_agent.run(text, route.get("reason", ""))
            elif route["intent"] == "execute":
                result = self.execution_agent.run(text)
            elif route["intent"] == "pipeline":
                result = self.pipeline_agent().run(text)
            else:
                result = self.query_agent.run(text)
        except BackendError as error:
            return {"kind": "error", "text": str(error), "intent": None}
        result["intent"] = route
        self._record(text, result)
        return result

    def _record(self, text, result):
        self.context.add_turn("user", text)
        if result["kind"] == "plan":
            commands = [r.command for r in result["plan"]]
            self.context.last_plan = [c.to_dict() for c in commands]
            summary = "Proposed plan:\n" + "\n".join(c.shell() for c in commands)
            if result.get("text"):
                summary = result["text"] + "\n" + summary
            self.context.add_turn("assistant", summary)
        elif result["kind"] == "clarify":
            self.context.add_turn("assistant", "\n".join(result["questions"]))
        else:
            self.context.add_turn("assistant", result.get("text", ""))


def gather_findings(workspace, max_chars=20000):
    """Collect the pipeline outputs a text model can read, for the results pass."""
    parts, used = [], 0
    for folder, pattern in (("tables", ".tex"), ("data", ".csv")):
        directory = workspace.path(folder)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.endswith(pattern) or name == "runbook.tex":
                continue
            text = workspace.read(os.path.join(folder, name)) or ""
            chunk = "### {}/{}\n{}".format(folder, name, text[:3000])
            if used + len(chunk) > max_chars:
                break
            parts.append(chunk)
            used += len(chunk)
    figures = [
        f
        for f in workspace.files()
        if f.startswith(("figures/", "maps/")) and not f.endswith(".dot")
    ]
    parts.append("### Figures and maps produced\n" + "\n".join(figures))
    for step in workspace.steps():
        log = workspace.read("logs/{}_{}.log".format(step["id"], step["name"])) or ""
        tail = "\n".join(log.splitlines()[-15:])
        if tail and used < max_chars:
            parts.append("### log of step {}\n{}".format(step["id"], tail))
            used += len(tail)
    return "\n\n".join(parts)
