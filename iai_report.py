"""
MODULE:    i.ai
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Report workspaces: scripted pipelines from data to a LaTeX report.
SPDX-License-Identifier: Unlicense

A workspace is a directory in the layout of a scripted report:

    00_env.sh, run_step.sh, NN_<step>.sh|py   in scripts/
    figures/ maps/ tables/ diagrams/ data/ logs/
    <main>.tex, references.bib, Makefile, README.md
    .i.ai/pipeline.json                       the pipeline manifest

i.ai writes the shared environment, the step runner, the Makefile, the
README run book and the LaTeX preamble itself; the language model writes
the step scripts and the report body. Every step runs the same way from
make and from i.ai: scripts/run_step.sh sources 00_env.sh, whose
grass_run() calls GRASS tools directly inside a session and through
'grass ... --exec' outside one. The report includes pipeline outputs
through \\figorpending and \\taborpending, so it compiles before the data
exist, with a placeholder box for each missing output.
"""

import datetime
import json
import os
import re
import shutil
import subprocess

import grass.script as gs

LAYOUT = ("scripts", "figures", "maps", "tables", "diagrams", "data", "logs", ".i.ai")
OUTPUT_ROOTS = ("figures/", "maps/", "tables/", "diagrams/", "data/")
STEP_ID = re.compile(r"^\d{2}[a-z]?$")
STEP_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
MAIN_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
RESERVED_ENV = {
    "REPORT_DIR", "SCRIPTS_DIR", "GISDBASE", "PROJECT", "MAPSET", "PATH", "HOME",
    "GISRC", "GISBASE", "LD_PRELOAD", "PYTHONPATH",
}  # fmt: skip
MM_URL = "https://github.com/YannChemin/MetaMonitor"


def latex_escape(text):
    """Escape LaTeX special characters in plain text."""
    replacements = {
        "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }  # fmt: skip
    return "".join(replacements.get(c, c) for c in str(text))


def default_author():
    """Return (name, email) from git config, else from the account."""
    name = email = ""
    if shutil.which("git"):
        for key in ("user.name", "user.email"):
            try:
                value = subprocess.run(
                    ["git", "config", "--global", key],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                ).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                value = ""
            if key == "user.name":
                name = value
            else:
                email = value
    if not name:
        try:
            import pwd

            name = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0]
        except (ImportError, KeyError):
            name = os.environ.get("USER", "")
    return name, email


class Workspace:
    """A report directory with its pipeline manifest."""

    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.manifest_path = os.path.join(self.root, ".i.ai", "pipeline.json")
        self.manifest = self._load()

    def _load(self):
        try:
            with open(self.manifest_path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    @property
    def exists(self):
        return bool(self.manifest.get("steps"))

    def create(self):
        for name in LAYOUT:
            os.makedirs(os.path.join(self.root, name), exist_ok=True)

    def path(self, relative):
        """Return the absolute path of a workspace file; refuse escapes."""
        target = os.path.realpath(os.path.join(self.root, relative))
        if os.path.commonpath([target, os.path.realpath(self.root)]) != os.path.realpath(
            self.root
        ):
            raise ValueError("Path <{}> is outside the workspace".format(relative))
        return target

    def write(self, relative, content, executable=False):
        target = self.path(relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(content if content.endswith("\n") else content + "\n")
        os.chmod(tmp, 0o755 if executable else 0o644)
        os.replace(tmp, target)
        return target

    def read(self, relative):
        try:
            with open(self.path(relative), encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return None

    def save_manifest(self):
        self.manifest["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        self.write(".i.ai/pipeline.json", json.dumps(self.manifest, indent=1))

    def steps(self):
        return self.manifest.get("steps", [])

    def step_file(self, step):
        extension = "py" if step["lang"] == "python" else "sh"
        return "scripts/{}_{}.{}".format(step["id"], step["name"], extension)

    def files(self, limit=300):
        """List the workspace files (not logs or the manifest)."""
        found = []
        for base, dirs, names in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "logs"]
            for name in sorted(names):
                found.append(os.path.relpath(os.path.join(base, name), self.root))
                if len(found) >= limit:
                    return found
        return found

    def known_outputs(self):
        """GRASS maps that earlier runs of this pipeline created."""
        return {tuple(item) for item in self.manifest.get("grass_outputs", [])}

    def record_outputs(self, outputs):
        known = self.known_outputs() | set(outputs)
        self.manifest["grass_outputs"] = sorted(list(o) for o in known)


def validate_outline(outline):
    """Return the list of problems in a pipeline outline from the model."""
    errors = []
    for key in ("title", "main", "steps", "sections"):
        if not outline.get(key):
            errors.append("Missing '{}'".format(key))
    main = outline.get("main", "")
    if main and not MAIN_NAME.match(main):
        errors.append("main must be a file stem such as SF_Terrain, not <{}>".format(main))
    ids = set()
    for step in outline.get("steps") or []:
        sid, name = str(step.get("id", "")), step.get("name", "")
        if not STEP_ID.match(sid):
            errors.append("Step id <{}> must be two digits, e.g. 01 or 04b".format(sid))
        if sid in ids:
            errors.append("Step id <{}> is used twice".format(sid))
        ids.add(sid)
        if not STEP_NAME.match(name or ""):
            errors.append("Step name <{}> must be lowercase snake_case".format(name))
        if step.get("lang") not in ("bash", "python"):
            errors.append("Step {} lang must be bash or python".format(sid))
        for output in step.get("outputs") or []:
            if "/" in output and not output.startswith(OUTPUT_ROOTS):
                errors.append(
                    "Step {} output <{}> must be under {} (or a GRASS map name)".format(
                        sid, output, ", ".join(OUTPUT_ROOTS)
                    )
                )
    for item in outline.get("env") or []:
        name = item.get("name", "")
        if not ENV_NAME.match(name) or name in RESERVED_ENV:
            errors.append("Environment variable <{}> is invalid or reserved".format(name))
    return errors


def render_env(workspace, outline, context):
    """Write the shared environment of every step."""
    env = context.environment
    lines = [
        "#!/usr/bin/env bash",
        "# Shared environment of the pipeline \"{}\", written by i.ai.".format(
            outline["title"].replace('"', "'")
        ),
        '# Source it from every step: source "$(dirname "$0")/00_env.sh"',
        "# Every value can be overridden from the environment.",
        "set -euo pipefail",
        "",
        "# Paths and GRASS session.",
        'export REPORT_DIR="${{REPORT_DIR:-{}}}"'.format(workspace.root),
        'export SCRIPTS_DIR="${SCRIPTS_DIR:-$REPORT_DIR/scripts}"',
        'export GISDBASE="${{GISDBASE:-{}}}"'.format(env["gisdbase"]),
        'export PROJECT="${{PROJECT:-{}}}"'.format(env["project"]),
        'export MAPSET="${{MAPSET:-{}}}"'.format(env["mapset"]),
    ]
    extra = outline.get("env") or []
    if extra:
        lines += ["", "# Study settings."]
        for item in extra:
            value = str(item.get("value", "")).replace('"', '\\"')
            comment = item.get("comment", "")
            line = 'export {name}="${{{name}:-{value}}}"'.format(name=item["name"], value=value)
            lines.append(line + ("  # " + comment if comment else ""))
    lines += [
        "",
        "# Run a GRASS tool: directly inside a session, else through grass --exec.",
        "grass_run() {",
        '    if [ -n "${GISRC:-}" ]; then',
        '        "$@"',
        "    else",
        '        grass "$GISDBASE/$PROJECT/$MAPSET" --exec "$@"',
        "    fi",
        "}",
        "",
        "# Progress for MetaMonitor ({}); a no-op without it.".format(MM_URL),
        "# Usage: mm set <slot> <done> --of <total> -n <name>; mm done <slot>",
        "mm() {",
        '    command -v mmctl >/dev/null 2>&1 && mmctl "$@" >/dev/null 2>&1 || true',
        "}",
    ]
    workspace.write("scripts/00_env.sh", "\n".join(lines), executable=True)


RUN_STEP = """#!/usr/bin/env bash
# Run one pipeline step with the shared environment (written by i.ai).
# Usage: scripts/run_step.sh [--grass] scripts/NN_name.sh|py
# --grass runs a Python step inside the GRASS session (grass_run).
set -euo pipefail
source "$(dirname "$0")/00_env.sh"
cd "$REPORT_DIR"
grass=0
if [ "${1:-}" = --grass ]; then
    grass=1
    shift
fi
case "$1" in
    *.py)
        if [ "$grass" = 1 ]; then
            grass_run python3 "$1"
        else
            python3 "$1"
        fi
        ;;
    *) bash "$1" ;;
esac
"""


def step_command(workspace, step):
    prefix = "--grass " if step["lang"] == "python" and step.get("grass") else ""
    return "scripts/run_step.sh {}{}".format(prefix, workspace.step_file(step))


def render_makefile(workspace):
    manifest = workspace.manifest
    main = manifest["main"]
    steps = workspace.steps()
    latex = (
        "latexmk -pdf -interaction=nonstopmode -halt-on-error $(MAIN)"
        if shutil.which("latexmk")
        else "pdflatex -interaction=nonstopmode -halt-on-error $(MAIN) && "
        "bibtex $(MAIN) && pdflatex -interaction=nonstopmode $(MAIN) && "
        "pdflatex -interaction=nonstopmode $(MAIN)"
    )
    lines = [
        "# Written by i.ai from .i.ai/pipeline.json; edit the scripts, not this file.",
        "# make analysis: every step in order; make step-NN: one step; make: all.",
        "MAIN = {}".format(main),
        "",
        ".PHONY: all analysis report clean " + " ".join("step-" + s["id"] for s in steps),
        "",
        "all: analysis report",
        "",
        "analysis:",
    ]
    lines += ["\t" + step_command(workspace, s) for s in steps]
    lines.append("")
    for step in steps:
        lines += ["step-{}:".format(step["id"]), "\t" + step_command(workspace, step), ""]
    lines += [
        "report: $(MAIN).pdf",
        "",
        "$(MAIN).pdf: $(MAIN).tex references.bib $(wildcard tables/*.tex figures/* maps/*)",
        "\t" + latex,
        "",
        "clean:",
        "\trm -f $(MAIN).aux $(MAIN).bbl $(MAIN).blg $(MAIN).log $(MAIN).out "
        "$(MAIN).toc $(MAIN).fls $(MAIN).fdb_latexmk",
    ]
    workspace.write("Makefile", "\n".join(lines))


def render_runbook(workspace):
    """Write tables/runbook.tex, the run book table included by the report."""
    rows = []
    for step in workspace.steps():
        rows.append(
            "\\texttt{{{}}} & {} \\\\".format(
                latex_escape(step_command(workspace, step)), latex_escape(step["purpose"])
            )
        )
    rows.append("\\texttt{make report} & PDF of this report \\\\")
    table = "\n".join(
        [
            "% Written by i.ai from .i.ai/pipeline.json.",
            "\\begin{tabular}{p{0.46\\linewidth}p{0.46\\linewidth}}",
            "\\toprule",
            "Command & Effect \\\\",
            "\\midrule",
            *rows,
            "\\bottomrule",
            "\\end{tabular}",
        ]
    )
    workspace.write("tables/runbook.tex", table)


def render_readme(workspace, session_name, references):
    manifest = workspace.manifest
    lines = [
        "# {}".format(manifest["title"]),
        "",
        "Report: `{}.tex` (build with `make`). Pipeline written by *i.ai*{} on {}.".format(
            manifest["main"],
            " (session `{}`)".format(session_name) if session_name else "",
            manifest.get("created", "")[:10],
        ),
        "",
        "## Run",
        "",
        "```sh",
        "cd {}".format(workspace.root),
        "make analysis   # every step in order (outside or inside a GRASS session)",
        "make report     # the PDF",
        "```",
        "",
        "Or from GRASS, with automatic repair of failing steps:",
        "",
        "```sh",
        "i.ai {}operation=run -e".format(
            "session={} ".format(session_name) if session_name else "workdir={} ".format(workspace.root)
        ),
        "```",
        "",
        "## Steps",
        "",
        "| Step | Script | Purpose | Outputs |",
        "|---|---|---|---|",
    ]
    for step in workspace.steps():
        lines.append(
            "| {} | `{}` | {} | {} |".format(
                step["id"],
                workspace.step_file(step),
                step["purpose"].replace("|", "/"),
                ", ".join("`{}`".format(o) for o in step.get("outputs") or []),
            )
        )
    lines += [
        "",
        "## Layout",
        "",
        "| Path | Content |",
        "|---|---|",
        "| `scripts/00_env.sh` | Paths, GRASS project, study settings, `grass_run`, `mm` |",
        "| `scripts/run_step.sh` | Runs one step with the shared environment |",
        "| `figures/`, `maps/`, `tables/` | Outputs included in the report |",
        "| `data/` | Downloaded and intermediate data |",
        "| `logs/` | One log per step run by i.ai |",
        "| `.i.ai/pipeline.json` | Pipeline manifest (steps, outputs, state) |",
    ]
    if references:
        lines += [
            "",
            "## References to verify",
            "",
            "The language model wrote these entries. Check every one against its DOI "
            "before the report leaves your hands:",
            "",
        ]
        lines += ["- `{}`: {}".format(key, title) for key, title in references]
    workspace.write("README.md", "\n".join(lines))


TEMPLATE = r"""\documentclass[11pt,a4paper]{article}
% Written by i.ai. The preamble and title block are generated; the body
% between the BODY markers is the report text.

\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage[margin=2.5cm]{geometry}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{booktabs}
\usepackage{array}
\usepackage{longtable}
\usepackage{xcolor}
\usepackage{titlesec}
\usepackage{fancyhdr}
\usepackage{hyperref}
\usepackage{microtype}
\usepackage{parskip}
\usepackage{enumitem}
\usepackage{caption}
\usepackage{graphicx}
\usepackage{siunitx}
\usepackage[numbers,sort&compress]{natbib}
\usepackage{url}
\usepackage{tcolorbox}
\sisetup{detect-all, per-mode=symbol}
\emergencystretch=2em

\definecolor{darkblue}{RGB}{15,50,90}
\definecolor{midblue}{RGB}{30,100,160}
\definecolor{lightgray}{RGB}{240,242,245}
\definecolor{warnorange}{RGB}{200,90,20}
\definecolor{milgreen}{RGB}{60,90,40}

\titleformat{\section}
  {\color{darkblue}\large\bfseries}
  {\thesection.}{0.6em}{}[\color{midblue}\titlerule]
\titleformat{\subsection}
  {\color{midblue}\normalsize\bfseries}
  {\thesubsection.}{0.5em}{}
\titleformat{\subsubsection}
  {\color{milgreen}\normalsize\bfseries\itshape}
  {\thesubsubsection.}{0.5em}{}

\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0.6pt}
\renewcommand{\footrulewidth}{0.4pt}
\fancyhead[L]{\small\color{darkblue}<<HEADLEFT>>}
\fancyhead[R]{\small\color{darkblue}<<SHORTTITLE>>}
\fancyfoot[L]{\tiny\color{gray}<<FOOTLEFT>>}
\fancyfoot[C]{\small\thepage}
\fancyfoot[R]{\small\color{warnorange}\textbf{<<CLASSIFICATION>>}}

\hypersetup{
  colorlinks=true, linkcolor=midblue, urlcolor=midblue, citecolor=midblue,
  pdftitle={<<PDFTITLE>>},
  pdfauthor={<<AUTHOR>>},
  pdfkeywords={<<KEYWORDS>>},
}

\newcommand{\nm}[1]{\SI{#1}{\nano\metre}}
\newcommand{\important}[1]{\textcolor{warnorange}{\textbf{#1}}}
\newcommand{\module}[1]{\textit{#1}}
\newtcolorbox{pending}[1][]{colback=lightgray,colframe=warnorange,boxrule=0.6pt,
  arc=2pt,left=6pt,right=6pt,top=4pt,bottom=4pt,fontupper=\small,title={#1},
  coltitle=white,fonttitle=\bfseries\small}
% Include a pipeline output when it exists, else a placeholder.
\newcommand{\figorpending}[3]{%
  \IfFileExists{#1}{\includegraphics[width=#2]{#1}}%
  {\begin{pending}[Pending]#3 (\texttt{\detokenize{#1}})\end{pending}}}
\newcommand{\taborpending}[2]{%
  \IfFileExists{#1}{\input{#1}}%
  {\begin{pending}[Pending]#2 (\texttt{\detokenize{#1}})\end{pending}}}

\begin{document}

<<BANNER>>
\begin{center}
{\color{darkblue}\rule{\linewidth}{1.5pt}}\\[8pt]
{\LARGE\bfseries\color{darkblue}<<TITLE>>\\[5pt]
  \large\bfseries <<SUBTITLE>>}\\[8pt]
{\color{midblue}\rule{\linewidth}{0.8pt}}\\[6pt]
\small
\begin{tabular}{ll}
<<METAROWS>>
\end{tabular}\\[8pt]
{\color{darkblue}\rule{\linewidth}{1.5pt}}
\end{center}

\tableofcontents
\newpage

% BODY START
<<BODY>>
% BODY END

\section{Run book}
\label{sec:runbook}
Every figure, map and table above comes from the scripts below, run in
this order (\texttt{make analysis}, then \texttt{make report}).

\medskip
\input{tables/runbook.tex}

\bibliographystyle{plainnat}
\bibliography{references}

\end{document}
"""


def render_tex(workspace, outline, body, meta):
    """Assemble the report from the template, the outline and the body."""
    classification = meta.get("classification", "")
    author = meta.get("author", "")
    contact = meta.get("contact", "")
    organization = meta.get("organization", "")
    rows = [
        ("Document ref.", outline.get("docref", "")),
        ("Date", datetime.date.today().strftime("%-d %B %Y")),
    ]
    if classification:
        rows.append(("Classification", classification))
    rows += [(m.get("label", ""), m.get("value", "")) for m in outline.get("meta") or []]
    prepared = author + (", " + organization if organization else "")
    if prepared:
        rows.append(("Prepared by", prepared))
    if contact:
        rows.append(("Contact", contact))
    meta_rows = "\n".join(
        "\\textbf{{{}:}} & {} \\\\[2pt]".format(latex_escape(k), latex_escape(v))
        for k, v in rows
        if k and v
    )
    banner = ""
    if classification:
        banner = (
            "\\noindent{\\color{warnorange}\\rule{\\linewidth}{3pt}}\n"
            "\\begin{center}{\\large\\bfseries\\color{warnorange}"
            + latex_escape(classification.upper())
            + "}\\end{center}\n{\\color{warnorange}\\rule{\\linewidth}{3pt}}\n\\vspace{1.2em}\n"
        )
    head_left = "\\textbf{{{}}}".format(latex_escape(classification)) if classification else ""
    if organization:
        head_left += (
            " \\textcolor{warnorange}{$\\bullet$} " if head_left else ""
        ) + latex_escape(organization)
    foot = " \\textbar\\ ".join(latex_escape(x) for x in (author, contact) if x)
    values = {
        "<<HEADLEFT>>": head_left,
        "<<SHORTTITLE>>": latex_escape(outline.get("short_title") or outline["title"]),
        "<<FOOTLEFT>>": foot,
        "<<CLASSIFICATION>>": latex_escape(classification.upper()),
        "<<PDFTITLE>>": latex_escape(outline["title"]),
        "<<AUTHOR>>": latex_escape(author),
        "<<KEYWORDS>>": latex_escape(", ".join(outline.get("keywords") or [])),
        "<<BANNER>>": banner,
        "<<TITLE>>": latex_escape(outline["title"]),
        "<<SUBTITLE>>": latex_escape(outline.get("subtitle", "")),
        "<<METAROWS>>": meta_rows,
        "<<BODY>>": body.strip(),
    }
    text = TEMPLATE
    for marker, value in values.items():
        text = text.replace(marker, value)
    workspace.write(outline["main"] + ".tex", text)


def bib_entries(bibtex):
    """Return (key, title) for every entry of a BibTeX text."""
    entries = []
    for match in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,(.*?)(?=\n@|\Z)", bibtex, re.DOTALL):
        title = re.search(r"title\s*=\s*[{\"](.+?)[}\"]\s*,?\s*\n", match.group(2), re.DOTALL)
        entries.append((match.group(1), re.sub(r"\s+", " ", title.group(1)) if title else ""))
    return entries


def check_report_body(body, bibtex, workspace, outputs):
    """Static checks of the report body before compiling.

    :param set outputs: Files the pipeline steps declare
    """
    errors = []
    if re.search(r"\\(write18|immediate\s*\\write|openout)|\\input\s*\|", body):
        errors.append("Shell escapes and file writes are not allowed in the report")
    if re.search(r"\\(documentclass|begin\{document\}|end\{document\}|bibliography\{)", body):
        errors.append(
            "Give only the body: no \\documentclass, \\begin{document}, "
            "\\end{document} or \\bibliography (i.ai adds them)"
        )
    keys = {key for key, _ in bib_entries(bibtex)}
    for group in re.findall(r"\\cite[pt]?\*?(?:\[[^\]]*\])*\{([^}]*)\}", body):
        for key in group.split(","):
            if key.strip() and key.strip() not in keys:
                errors.append("\\cite{{{}}} has no entry in the BibTeX".format(key.strip()))
    for command, path in re.findall(r"\\(includegraphics|input)(?:\[[^\]]*\])?\{([^}]+)\}", body):
        if path == "tables/runbook.tex":
            continue
        exists = os.path.exists(workspace.path(path)) or any(
            os.path.exists(workspace.path(path + ext)) for ext in (".pdf", ".png", ".tex")
        )
        if not exists:
            hint = "\\figorpending" if command == "includegraphics" else "\\taborpending"
            errors.append(
                "\\{}{{{}}}: the file does not exist yet; use {} so the report compiles "
                "before the pipeline runs".format(command, path, hint)
            )
    for path in re.findall(r"\\(?:fig|tab)orpending\{([^}]+)\}", body):
        if path not in outputs and not os.path.exists(workspace.path(path)):
            errors.append(
                "<{}> is included but no step declares it as an output".format(path)
            )
    return errors


def compile_report(workspace, main, timeout=600):
    """Compile the report; return (ok, errors) with the LaTeX error lines."""
    if shutil.which("latexmk"):
        cmds = [
            ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error",
             "-file-line-error", main + ".tex"],
        ]  # fmt: skip
    elif shutil.which("pdflatex"):
        once = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", main]
        cmds = [once, ["bibtex", main], once, once]
    else:
        return False, ["No LaTeX installation found (latexmk or pdflatex)"]
    for cmd in cmds:
        try:
            result = subprocess.run(
                cmd,
                cwd=workspace.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, ["LaTeX did not finish within {} s".format(timeout)]
        if result.returncode != 0 and cmd[0] != "bibtex":
            break
    log = workspace.read(main + ".log") or ""
    errors = latex_errors(log)
    blg = workspace.read(main + ".blg") or ""
    errors += [line for line in blg.splitlines() if "I didn't find a database entry" in line]
    pdf = workspace.path(main + ".pdf")
    ok = os.path.exists(pdf) and result.returncode == 0 and not errors
    if not errors and result.returncode != 0:
        errors = [result.stdout.strip()[-1500:] or "LaTeX failed"]
    return ok, errors


def latex_errors(log):
    """Extract the error lines (with context) and undefined references."""
    errors = []
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^(\./)?[^:\s]+\.tex:\d+: ", line) or line.startswith("! "):
            errors.append(" ".join(l.strip() for l in lines[i : i + 3]))
        elif "Citation" in line and "undefined" in line:
            errors.append(line.strip())
        elif re.match(r"^LaTeX Warning: Reference .* undefined", line):
            errors.append(line.strip())
    seen, unique = set(), []
    for error in errors:
        if error not in seen:
            seen.add(error)
            unique.append(error)
    return unique[:15]


def run_step(workspace, step, env=None):
    """Run one step, logging to logs/NN_name.log.

    :return: (exit code, last lines of the log)
    """
    log_path = workspace.path("logs/{}_{}.log".format(step["id"], step["name"]))
    cmd = ["bash", "scripts/run_step.sh"]
    if step["lang"] == "python" and step.get("grass"):
        cmd.append("--grass")
    cmd.append(workspace.step_file(step))
    run_env = dict(env or os.environ)
    run_env["REPORT_DIR"] = workspace.root
    with open(log_path, "w", encoding="utf-8") as log:
        log.write("$ {}\n".format(" ".join(cmd)))
        log.flush()
        process = subprocess.run(
            cmd,
            cwd=workspace.root,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=run_env,
            check=False,
        )
    with open(log_path, encoding="utf-8", errors="replace") as log:
        tail = log.readlines()[-80:]
    gs.verbose(_("Step {} log: {}").format(step["id"], log_path))
    return process.returncode, "".join(tail)
