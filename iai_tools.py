"""
MODULE:    i.ai
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Tools the language model calls while it works out an answer.
SPDX-License-Identifier: Unlicense

All tools are read-only. They let the model look up any installed GRASS
tool (search, full list per prefix, full interface, manual pages), inspect
the data (maps, metadata, region) and run read-only tools such as r.univar
or v.info. Commands that create or change data are never run here: the
model proposes them as a plan, which is validated and executed only on
the user's request.
"""

import json
import os
import re

import grass.script as gs

from iai_validate import Command

MAX_OUTPUT = 6000


class Tool:
    def __init__(self, name, description, args, function):
        self.name = name
        self.description = description
        self.args = args  # name -> short description; "?" suffix means optional.
        self.function = function

    def signature(self):
        args = ", ".join(
            "{}: {}".format(key, value) for key, value in self.args.items()
        )
        return "- {}({}): {}".format(self.name, args, self.description)


class ToolRegistry:
    """Named tools with descriptions, executed from model requests."""

    def __init__(
        self,
        catalog,
        registry,
        matcher,
        validator,
        manual,
        readonly=True,
        commands=None,
        shell_validator=None,
    ):
        self.catalog = catalog
        self.registry = registry
        self.matcher = matcher
        self.validator = validator
        self.manual = manual
        self.commands = commands
        self.shell_validator = shell_validator
        self.workspace = None
        self.tools = {}
        self.register(
            "search_modules",
            "Search all installed GRASS tools (core and addons) by keywords.",
            {
                "query": "words describing the task",
                "prefix?": "r, v, i, ... to restrict",
            },
            self._search_modules,
        )
        self.register(
            "list_modules",
            "List the names of every installed GRASS tool with a given prefix.",
            {"prefix": "one of d, db, g, i, m, ps, r, r3, t, v"},
            self._list_modules,
        )
        self.register(
            "module_help",
            "Full interface of one installed tool: every parameter, option value "
            "and flag. Read it before proposing a command with that tool.",
            {"module": "tool name, e.g. r.slope.aspect"},
            self._module_help,
        )
        self.register(
            "search_manual",
            "Search the installed manual pages; returns the most relevant sections.",
            {"query": "question or keywords", "module?": "restrict to one tool"},
            self._search_manual,
        )
        self.register(
            "list_maps",
            "List maps in the search path.",
            {
                "type": "raster, vector, raster_3d, group, strds, stvds or str3ds",
                "pattern?": "substring filter",
            },
            self._list_maps,
        )
        self.register(
            "match_map",
            "Find the existing maps that best match a name or description.",
            {"text": "map name or words", "type?": "map type"},
            self._match_map,
        )
        self.register(
            "map_info",
            "Metadata of a map (r.info, v.info, r3.info, i.group or t.info).",
            {"name": "map name", "type": "map type"},
            self._map_info,
        )
        self.register(
            "region_info",
            "Current computational region in full.",
            {},
            self._region_info,
        )
        self.register(
            "check_command",
            "Validate a GRASS command without running it; returns errors, "
            "suggestions and its risk level (read, create, modify).",
            {
                "module": "tool name",
                "params?": "object of key: value",
                "flags?": "letters",
            },
            self._check_command,
        )
        if readonly:
            self.register(
                "run_readonly",
                "Run a read-only GRASS command (statistics, queries, metadata) and "
                "return its output. Refused for commands that create or change data. "
                "Raster statistics follow the current computational region; give "
                "region_raster to compute over a raster map's own extent and "
                "resolution instead (the user's region is not changed).",
                {
                    "module": "tool name",
                    "params?": "object of key: value",
                    "flags?": "letters",
                    "region_raster?": "raster map whose region to use",
                },
                self._run_readonly,
            )

        if commands is not None:
            self.register(
                "search_commands",
                "Search every command-line tool on PATH (GDAL, LaTeX, Graphviz, "
                "ImageMagick, pandoc, jq, curl, ...) by keywords in their manuals.",
                {"query": "words describing the task"},
                self._search_commands,
            )
            self.register(
                "command_help",
                "Manual page (or --help) of an installed command-line tool.",
                {"command": "command name, e.g. gdalwarp"},
                self._command_help,
            )
            self.register(
                "run_shell_readonly",
                "Run one read-only inspection command without a shell (gdalinfo, "
                "ogrinfo, pdfinfo, ncdump, ls, head, find, grep, wc, file, du, "
                "kpsewhich, or any command with --version/--help). No pipes or "
                "redirections.",
                {"command": "command line, e.g. gdalinfo -json data/dem.tif"},
                self._run_shell_readonly,
            )
            self.register(
                "check_script",
                "Validate a bash or Python script without running it: syntax, "
                "installed commands and modules, GRASS calls, forbidden patterns.",
                {"code": "script text", "lang": "bash or python"},
                self._check_script,
            )

    def enable_report_tools(self, workspace):
        """Add the tools of the report route, bound to a workspace."""
        self.workspace = workspace
        self.register(
            "list_workspace",
            "List the files already in the report workspace.",
            {},
            lambda: workspace.files() or "The workspace is empty.",
        )
        self.register(
            "check_doi",
            "Look up a DOI at Crossref; returns the registered title, authors, "
            "journal and year, to verify a reference before citing it.",
            {"doi": "e.g. 10.1016/j.rse.2019.111383"},
            self._check_doi,
        )

    def register(self, name, description, args, function):
        self.tools[name] = Tool(name, description, args, function)

    def describe(self):
        return "\n".join(tool.signature() for tool in self.tools.values())

    def call(self, name, args):
        """Run a tool; errors are returned as text for the model to read."""
        tool = self.tools.get(name)
        if tool is None:
            return "Unknown tool '{}'. Available: {}".format(
                name, ", ".join(self.tools)
            )
        if not isinstance(args, dict):
            args = {}
        try:
            result = tool.function(**args)
        except TypeError as error:
            return "Bad arguments for {}: {}".format(name, error)
        except Exception as error:  # noqa: BLE001 - reported to the model
            return "{} failed: {}".format(name, error)
        if not isinstance(result, str):
            result = json.dumps(result, indent=1, default=str)
        if len(result) > MAX_OUTPUT:
            result = result[:MAX_OUTPUT] + "\n... (output truncated)"
        return result

    def _search_modules(self, query, prefix=None, k=15):
        found = self.catalog.search(query, k=int(k), prefix=prefix)
        if not found:
            return "No installed tool matches '{}'. Try other words or list_modules.".format(
                query
            )
        return found

    def _list_modules(self, prefix):
        names = self.catalog.names(prefix)
        if not names:
            return "No installed tool with prefix '{}'.".format(prefix)
        return ", ".join(names)

    def _module_help(self, module):
        if not self.catalog.exists(module):
            similar = self.catalog.similar(module)
            return "Tool <{}> is not installed.{}".format(
                module, " Close names: " + ", ".join(similar) if similar else ""
            )
        return self.catalog.interface_text(module)

    def _search_manual(self, query, module=None, k=4):
        hits = self.manual.search(query, k=int(k), tool=module)
        if not hits:
            return "No manual section matches."
        return "\n\n".join(
            "[{} - {}]\n{}".format(h["tool"], h["section"], h["text"]) for h in hits
        )

    def _list_maps(self, type="raster", pattern=None):  # noqa: A002 - model-facing name
        names = [e["fullname"] for e in self.registry.of_type(type)]
        if pattern:
            names = [n for n in names if pattern.lower() in n.lower()]
        return names or "No {} maps{}.".format(
            type, " matching '{}'".format(pattern) if pattern else ""
        )

    def _match_map(self, text, type=None):  # noqa: A002
        matches = self.matcher.match(text, type)
        if not matches:
            return "No map resembles '{}'.".format(text)
        return [
            {
                "map": m["entity"]["fullname"],
                "type": m["entity"]["type"],
                "confidence": m["confidence"],
                "why": m["explanation"],
            }
            for m in matches
        ]

    def _map_info(self, name, type="raster"):  # noqa: A002
        entity = self.registry.find(name, type)
        if entity is None:
            return self._match_map(name, type)
        if type == "raster":
            return gs.read_command("r.info", map=entity["fullname"])
        if type == "vector":
            return gs.read_command("v.info", map=entity["fullname"])
        if type == "raster_3d":
            return gs.read_command("r3.info", map=entity["fullname"])
        if type == "group":
            return gs.read_command("i.group", flags="l", group=entity["fullname"])
        return gs.read_command("t.info", input=entity["fullname"], type=type)

    def _region_info(self):
        return gs.read_command("g.region", flags="p3")

    def _check_command(self, module, params=None, flags=""):
        command = Command(module, params or {}, flags)
        return self.validator.validate(command).to_dict()

    def _run_readonly(self, module, params=None, flags="", region_raster=None):
        command = Command(module, params or {}, flags)
        result = self.validator.validate(command)
        if not result.valid:
            return result.to_dict()
        if result.risk != "read":
            return (
                "Refused: <{}> is classified '{}'. Put it in the plan instead; the "
                "user decides whether it runs.".format(command.shell(), result.risk)
            )
        kwargs = dict(command.params)
        if command.flags:
            kwargs["flags"] = command.flags
        if region_raster and not self.registry.find(region_raster, "raster"):
            return "Raster map <{}> for region_raster does not exist.".format(
                region_raster
            )
        gs.verbose(_("Running read-only command: {}").format(command.shell()))
        # A scoped region on a copy of the environment leaves the user's region alone.
        region_args = {"raster": region_raster} if region_raster else {}
        with gs.RegionManager(env=os.environ.copy(), **region_args) as manager:
            process = gs.start_command(
                command.module,
                stdout=gs.PIPE,
                stderr=gs.PIPE,
                quiet=True,
                env=manager.env,
                **kwargs,
            )
            stdout, stderr = process.communicate()
        stdout = gs.decode(stdout)
        stderr = gs.decode(stderr)
        if process.returncode != 0:
            return "Command failed (exit {}): {}".format(
                process.returncode, stderr.strip()[-2000:]
            )
        return stdout or stderr or "(no output)"

    def _search_commands(self, query, k=20):
        found = self.commands.search(query, k=int(k))
        return found or "No installed command matches '{}'.".format(query)

    def _command_help(self, command):
        text = self.commands.help_text(command)
        if text is None:
            similar = self.commands.similar(command)
            return "Command {} is not installed.{}".format(
                command, " Close names: " + ", ".join(similar) if similar else ""
            )
        return text

    def _run_shell_readonly(self, command):
        from iai_shell import probe

        cwd = self.workspace.root if self.workspace else None
        gs.verbose(_("Probe: {}").format(command))
        return probe(command, cwd=cwd)

    def _check_script(self, code, lang="bash"):
        functions = ("grass_run", "mm") if self.workspace else ()
        return self.shell_validator.check(code, lang, functions).to_dict()

    def _check_doi(self, doi):
        import urllib.parse
        import urllib.request

        doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi.strip(), flags=re.I)
        url = "https://api.crossref.org/works/" + urllib.parse.quote(doi)
        request = urllib.request.Request(url, headers={"User-Agent": "i.ai (GRASS addon)"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                item = json.loads(response.read().decode("utf-8"))["message"]
        except Exception as error:  # noqa: BLE001 - reported to the model
            return "DOI {} not found at Crossref ({}); do not cite it.".format(doi, error)
        authors = [
            "{} {}".format(a.get("given", ""), a.get("family", "")).strip()
            for a in item.get("author", [])[:8]
        ]
        year = (item.get("issued", {}).get("date-parts") or [[None]])[0][0]
        return {
            "doi": doi,
            "title": (item.get("title") or [""])[0],
            "authors": authors,
            "journal": (item.get("container-title") or [""])[0],
            "year": year,
            "volume": item.get("volume"),
            "issue": item.get("issue"),
            "pages": item.get("page"),
            "type": item.get("type"),
        }
