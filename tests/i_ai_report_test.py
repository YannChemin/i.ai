"""End-to-end test of a report pipeline with a scripted backend.

The scripted model writes a two-step pipeline (a bash step with GRASS and
a Python step), then the report body. The pipeline runs in the test
project, a failing step is repaired, and the report compiles to a PDF. No
language model and no network are used.
"""

import importlib.util
import os
import shutil

import pytest

from conftest import ADDON_DIR
from iai_llm import Backend

OUTLINE = {
    "action": "outline",
    "outline": {
        "title": "Terrain test report",
        "subtitle": "Scripted pipeline",
        "main": "TT_Report",
        "meta": [{"label": "Study area", "value": "XY test project"}],
        "env": [{"name": "FACTOR", "value": "2", "comment": "Scale factor."}],
        "steps": [
            {
                "id": "01",
                "name": "scale_elevation",
                "lang": "bash",
                "purpose": "Scale the elevation and tabulate its statistics.",
                "outputs": ["elev_scaled", "tables/stats.tex"],
            },
            {
                "id": "02",
                "name": "summary",
                "lang": "python",
                "purpose": "Write the summary table.",
                "outputs": ["tables/summary.tex"],
            },
        ],
        "sections": [{"title": "Results", "brief": "Statistics"}],
    },
}

STEP1 = r"""#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/00_env.sh"
grass_run r.mapcalc expression="elev_scaled = elevation * $FACTOR" --overwrite
mean=$(grass_run r.univar -g map=elev_scaled | sed -n 's/^mean=//p')
printf '\\begin{tabular}{lr}\\toprule Statistic & Value \\\\ \\midrule Mean & %s \\\\ \\bottomrule\\end{tabular}\n' "$mean" > "$REPORT_DIR/tables/stats.tex"
"""

STEP2_BROKEN = '''#!/usr/bin/env python3
"""Summary table."""
import os
import sys

sys.exit("deliberate failure")
'''

STEP2_FIXED = '''#!/usr/bin/env python3
"""Summary table."""
import os

out = os.path.join(os.environ["REPORT_DIR"], "tables", "summary.tex")
with open(out, "w") as handle:
    handle.write("\\\\begin{tabular}{l}\\\\toprule Done \\\\\\\\ \\\\bottomrule\\\\end{tabular}\\n")
'''

BODY = r"""\section*{Executive summary}
\addcontentsline{toc}{section}{Executive summary}
Test.
\section{Results}
\begin{table}[h]\centering
\taborpending{tables/stats.tex}{Statistics}
\caption{Statistics.}\label{tab:stats}
\end{table}
\taborpending{tables/summary.tex}{Summary}
\begin{pending}[Pending]Results after the run.\end{pending}
"""

BODY_FINAL = BODY.replace(
    r"\begin{pending}[Pending]Results after the run.\end{pending}",
    "The mean of the scaled elevation is in Table~\\ref{tab:stats}.",
)


class ScriptedBackend(Backend):
    name = "scripted"

    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)

    def complete_json(self, system, messages, schema):
        return self.replies.pop(0)


def load_main():
    spec = importlib.util.spec_from_file_location("i_ai_main", os.path.join(ADDON_DIR, "i.ai.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="needs LaTeX")
def test_pipeline_end_to_end(parts, tmp_path):
    from iai_agents import Copilot
    from iai_context import ContextManager
    from iai_rag import ManualIndex
    from iai_report import Workspace
    from iai_shell import CommandCatalog, ShellValidator
    from iai_tools import ToolRegistry

    catalog, registry, matcher, validator = parts
    commands = CommandCatalog()
    shell_validator = ShellValidator(commands, catalog, validator)
    backend = ScriptedBackend(
        [
            OUTLINE,
            {"action": "script", "code": STEP1},
            {"action": "script", "code": STEP2_BROKEN},
            {"action": "report", "body": BODY, "bibtex": ""},
            {"action": "script", "code": STEP2_FIXED},  # The repair.
            {"action": "report", "body": BODY_FINAL, "bibtex": ""},  # Results pass.
        ]
    )
    context = ContextManager(catalog, registry, commands)
    tools = ToolRegistry(
        catalog, registry, matcher, validator, ManualIndex(),
        commands=commands, shell_validator=shell_validator,
    )  # fmt: skip
    copilot = Copilot(backend, context, tools, validator)
    workspace = Workspace(tmp_path / "report")
    meta = {"author": "Tester", "contact": "", "organization": "", "classification": "", "session": None}
    copilot.workspace_factory = lambda: (workspace, meta)

    result = copilot.run("Write a terrain report", intent="pipeline")
    assert result["kind"] == "pipeline", result
    assert result["compiled"], workspace.manifest.get("report")
    for name in ("Makefile", "README.md", "TT_Report.tex", "scripts/00_env.sh",
                 "scripts/run_step.sh", "scripts/01_scale_elevation.sh",
                 "scripts/02_summary.py", "tables/runbook.tex"):  # fmt: skip
        assert os.path.exists(workspace.path(name)), name
    assert 'FACTOR="${FACTOR:-2}"' in workspace.read("scripts/00_env.sh")

    main = load_main()

    class Session:
        pass

    session = Session()
    session.copilot = copilot
    session.shell_validator = shell_validator
    session.registry = registry
    session.context = context
    options = {"from_step": "", "repairs": "2", "author": "Tester", "contact": "",
               "organization": "", "classification": "", "session": ""}  # fmt: skip
    flags = {"y": True, "e": True}
    assert main.run_pipeline(session, workspace, options, flags)
    assert "Mean" in workspace.read("tables/stats.tex")
    assert os.path.exists(workspace.path("scripts/02_summary.py.orig"))
    assert registry.find("elev_scaled", "raster")
    assert ["raster", "elev_scaled"] in workspace.manifest["grass_outputs"]
    assert main.finalize_report(session, workspace, options)
    assert os.path.exists(workspace.path("TT_Report.pdf"))
    assert "Table~\\ref{tab:stats}" in workspace.read("TT_Report.tex")


def test_outline_validation():
    from iai_report import validate_outline

    bad = {"title": "x", "main": "bad name", "steps": [{"id": "1", "name": "Bad", "lang": "sh",
           "outputs": ["/etc/passwd"]}], "sections": [{}], "env": [{"name": "PATH"}]}  # fmt: skip
    errors = validate_outline(bad)
    assert len(errors) >= 5


def test_report_body_checks(tmp_path):
    from iai_report import Workspace, check_report_body

    ws = Workspace(tmp_path)
    errors = check_report_body(
        "\\includegraphics{figures/a.pdf}\\cite{nokey}\\write18{ls}", "", ws, set()
    )
    assert any("figorpending" in e for e in errors)
    assert any("nokey" in e for e in errors)
    assert any("Shell escapes" in e for e in errors)
