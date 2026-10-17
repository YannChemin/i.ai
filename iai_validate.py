"""
MODULE:    i.ai
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Validation of GRASS commands proposed by the language model.
SPDX-License-Identifier: Unlicense

Every command the model proposes is checked against the live interface of
the installed tool and against the maps that exist, before anything runs:
unknown tools, parameters, flags and option values, missing required
parameters, missing input maps and existing output maps are errors, each
with a suggestion when a close match exists. Nothing is corrected
silently; the errors go back to the model so it fixes its own command.

Each valid command also gets a risk level:

- read: only reads data or prints session state; may run automatically.
- create: writes new maps or files and nothing else.
- display: a d.* tool that draws on a monitor and changes no data; runs
  without confirmation in a plan, but not as a read-only lookup.
- modify: changes existing data or session state (region, mask, database
  tables, removals, colour tables...), or a tool that is not known to be
  read-only and has no declared output. These always need confirmation.
"""

import difflib
import re
import shlex

import grass.script as gs

# Tools known to only read data or print state, whatever their arguments.
READ_ONLY = {
    "db.columns", "db.describe", "db.drivers", "db.select", "db.tables",
    "g.extension.all", "g.findfile", "g.list", "g.mapsets.list", "g.search.modules",
    "g.version", "i.signatures", "m.measure",
    "r.coin", "r.covar", "r.describe", "r.info", "r.kappa", "r.object.geometry",
    "r.quantile", "r.regression.line", "r.regression.multi", "r.report",
    "r.stats", "r.stats.quantile", "r.univar", "r.what", "r.what.color",
    "r3.info", "r3.univar", "r3.what",
    "t.info", "t.list", "t.rast.list", "t.rast.univar", "t.rast3d.list",
    "t.rast3d.univar", "t.topology", "t.vect.db.select", "t.vect.list",
    "t.vect.univar",
    "v.db.select", "v.db.univar", "v.info", "v.report", "v.univar", "v.what",
    "v.what.rast.label",
}  # fmt: skip

# Tools read-only only when called with print flags and no parameters.
PRINT_ONLY = {
    "g.region": set("pgucl3ebwn"),
    "g.proj": set("pgjwfe"),
    "g.gisenv": set("ns"),
    "g.mapsets": set("lp"),
    "g.mapset": set("lp"),
    "i.group": set("lg"),
    "db.connect": set("pg"),
    "g.extension": set("lca"),
}

# Parameters that only shape printed output, allowed in print-only calls.
PRINT_PARAMS = {"format", "separator", "precision"}

# Read-only when the given parameter has one of these values.
READ_WHEN = {
    "v.category": ("option", {"report", "print", "layers"}),
    "v.db.connect": ("flags", set("pgc")),
}

# Tools that declare an output but also change or remove existing state.
MODIFY_WITH_OUTPUT = {"g.rename", "g.region", "r.mask", "v.edit", "t.rename"}

# Tools that need an interactive GUI or terminal and cannot run from i.ai.
INTERACTIVE = re.compile(r"^(g\.gui(\..*)?|g\.manual|v\.digit|i\.ask)$")

MAP_PROMPTS = {"raster", "vector", "raster_3d", "group", "strds", "stvds", "str3ds"}
POSITIONAL = "__positional__"
CONTROL_FLAGS = {"overwrite", "help", "verbose", "quiet", "ui"}
ILLEGAL_NAME = re.compile(r"[\s/\"'@,=*~]|[^\x00-\x7f]")
MAPCALC_WORDS = {
    "if", "isnull", "null", "eval", "graph", "rand", "row", "col", "x", "y",
    "ewres", "nsres", "area", "depth", "and", "or", "not", "float", "double",
    "int", "round", "abs", "exp", "log", "sqrt", "sin", "cos", "tan", "atan",
    "max", "min", "median", "mode", "nmax", "nmin", "pi", "e",
}  # fmt: skip


class Command:
    """A GRASS tool call: module name, parameters and one-letter flags."""

    def __init__(self, module, params=None, flags="", purpose=""):
        self.module = module.strip()
        self.params = {}
        for key, value in (params or {}).items():
            if key in CONTROL_FLAGS or value is None or value == "":
                continue
            if isinstance(value, (list, tuple)):
                value = ",".join(str(v) for v in value)
            elif isinstance(value, bool):
                value = "yes" if value else "no"
            self.params[str(key)] = str(value)
        if isinstance(flags, (list, tuple)):
            flags = "".join(str(f) for f in flags)
        self.flags = "".join(
            sorted(set(str(flags or "").replace("-", "").replace(" ", "")))
        )
        self.purpose = purpose or ""

    @classmethod
    def from_any(cls, value):
        """Build a Command from a dict or a shell-like command string."""
        if isinstance(value, Command):
            return value
        if isinstance(value, str):
            return cls.from_string(value)
        if not isinstance(value, dict):
            raise ValueError("A command must be an object or a string")
        if "module" not in value and "command" in value:
            command = cls.from_string(value["command"])
            command.purpose = value.get("purpose", "")
            return command
        return cls(
            value.get("module", ""),
            value.get("params") or {},
            value.get("flags", ""),
            value.get("purpose", ""),
        )

    @classmethod
    def from_string(cls, text):
        tokens = shlex.split(text.strip())
        if not tokens:
            raise ValueError("Empty command")
        params, flags = {}, ""
        for token in tokens[1:]:
            if token.startswith("--"):
                continue  # --overwrite and friends are controlled by i.ai.
            if token.startswith("-") and "=" not in token:
                flags += token[1:]
            elif "=" in token and re.match(r"^[a-z_0-9]+=", token):
                key, value = token.split("=", 1)
                params[key] = value
            elif POSITIONAL not in params:
                params[POSITIONAL] = token  # The first parameter, without key=.
            else:
                raise ValueError(
                    "Cannot read <{}> in <{}>: use key=value".format(token, text)
                )
        return cls(tokens[0], params, flags)

    def shell(self, overwrite=False):
        """Return the command as a copy-pasteable shell line."""
        parts = [self.module]
        if self.flags:
            parts.append("-" + self.flags)
        parts += [
            "{}={}".format(key, shlex.quote(value))
            for key, value in self.params.items()
        ]
        if overwrite:
            parts.append("--overwrite")
        return " ".join(parts)

    def to_dict(self):
        return {
            "module": self.module,
            "params": dict(self.params),
            "flags": self.flags,
            "purpose": self.purpose,
        }


class ValidationResult:
    def __init__(self, command):
        self.command = command
        self.errors = []
        self.warnings = []
        self.suggestions = []
        self.risk = "modify"
        self.outputs = []
        self.script_mode = False

    @property
    def valid(self):
        return not self.errors

    def to_dict(self):
        return {
            "command": self.command.shell(),
            "valid": self.valid,
            "risk": self.risk,
            "errors": self.errors,
            "warnings": self.warnings,
            "suggestions": self.suggestions,
        }


class ValidationLayer:
    """Check proposed commands against installed tools and existing maps."""

    def __init__(self, catalog, registry, matcher, overwrite=False):
        self.catalog = catalog
        self.registry = registry
        self.matcher = matcher
        self.overwrite = overwrite
        # Outputs planned by earlier steps count as existing inputs.
        self.planned = set()

    def current_mapset(self):
        return gs.gisenv()["MAPSET"]

    def validate(self, command, planned=None, script_mode=False):
        """Validate one command.

        :param Command command: Command to check
        :param set planned: (type, name) pairs created by earlier plan steps
        :param bool script_mode: The command sits in a script: values holding
            shell or Python variables are not checked, and missing or existing
            maps are warnings, since earlier steps may create or replace them
        :rtype: ValidationResult
        """
        planned = self.planned if planned is None else planned
        result = ValidationResult(command)
        result.script_mode = script_mode
        name = command.module
        if not self.catalog.exists(name):
            result.errors.append("Tool <{}> is not installed".format(name))
            similar = self.catalog.similar(name)
            if similar:
                result.suggestions.append("Did you mean: " + ", ".join(similar))
            else:
                result.suggestions.append(
                    "Use search_modules to find an installed tool for this task"
                )
            return result
        if INTERACTIVE.match(name):
            result.errors.append(
                "Tool <{}> is interactive and cannot run from i.ai".format(name)
            )
            return result
        try:
            info = self.catalog.interface(name)
        except Exception as error:  # noqa: BLE001 - reported to the model
            result.errors.append(
                "Cannot read the interface of <{}>: {}".format(name, error)
            )
            return result

        params = {p["name"]: p for p in info.get("params", [])}
        flags = {f["name"] for f in info.get("flags", [])}
        if POSITIONAL in command.params and info.get("params"):
            # GRASS reads a value without key= as the first parameter.
            first = info["params"][0]["name"]
            value = command.params.pop(POSITIONAL)
            command.params.setdefault(first, value)
        for key in command.params:
            if key not in params:
                result.errors.append("<{}> has no parameter '{}'".format(name, key))
                close = difflib.get_close_matches(key, list(params), n=3)
                if close:
                    result.suggestions.append(
                        "Parameters of <{}> close to '{}': {}".format(
                            name, key, ", ".join(close)
                        )
                    )
        for flag in command.flags:
            if flag not in flags:
                result.errors.append("<{}> has no flag -{}".format(name, flag))
        missing = [
            p["name"]
            for p in params.values()
            if p.get("required")
            and not p.get("default")
            and p["name"] not in command.params
        ]
        if missing:
            message = "Missing required parameter(s) of <{}>: {}".format(
                name, ", ".join(missing)
            )
            # Some flags (such as list or print flags) lift the requirement.
            if command.flags:
                result.warnings.append(message)
            else:
                result.errors.append(message)

        for key, value in command.params.items():
            spec = params.get(key)
            if spec and not (script_mode and _is_dynamic(value)):
                self._check_value(name, spec, value, planned, result)
        if name in ("r.mapcalc", "r3.mapcalc") and "expression" in command.params:
            expression = command.params["expression"]
            if not (script_mode and _is_dynamic(expression)):
                self._check_mapcalc(name, expression, planned, result)
            else:
                # The maps it writes still count, when their names are literal.
                element = "raster" if name == "r.mapcalc" else "raster_3d"
                for target in re.findall(
                    r"(?:^|[\n;])\s*([A-Za-z0-9_.]+)\s*=(?!=)", expression
                ):
                    result.outputs.append((element, target))

        if script_mode:
            # Maps may come from earlier steps or be replaced on purpose.
            keep = []
            for error in result.errors:
                if "does not exist" in error or "already exists" in error:
                    result.warnings.append(error)
                else:
                    keep.append(error)
            result.errors = keep
        result.risk = self._risk(command, info, result)
        return result

    def _check_value(self, module, spec, value, planned, result):
        key = spec["name"]
        multi_part = spec.get("multiple") or len(spec.get("key_desc") or []) > 1
        values = value.split(",") if multi_part else [value]
        kind = spec.get("type")
        for item in values:
            if kind == "integer":
                try:
                    int(item)
                except ValueError:
                    result.errors.append("{}={} must be an integer".format(key, item))
            elif kind == "float":
                try:
                    float(item)
                except ValueError:
                    result.errors.append("{}={} must be a number".format(key, item))
            options = spec.get("values") or []
            if kind == "string" and options and not _is_range(options):
                # The GRASS parser accepts a unique prefix of an option value.
                prefixed = [o for o in options if o.startswith(item)] if item else []
                if item not in options and len(prefixed) != 1:
                    result.errors.append(
                        "{}={} is not one of: {}".format(
                            key, item, ", ".join(options[:30])
                        )
                    )
        if not spec.get("gisprompt"):
            return
        prompt = spec.get("prompt", "")
        age = spec.get("age", "")
        if prompt not in MAP_PROMPTS and prompt != "stds":
            return
        for item in values:
            item = item.strip()
            if not item:
                continue
            element = None if prompt == "stds" else prompt
            if age == "old":
                if self._exists(item, element, planned):
                    continue
                result.errors.append(
                    "Input {} map <{}> does not exist".format(prompt, item)
                )
                matches = self.matcher.match(item, element)
                if matches:
                    result.suggestions.append(
                        "Existing {} maps close to <{}>: {}".format(
                            prompt,
                            item,
                            ", ".join(
                                "{} ({})".format(
                                    m["entity"]["fullname"], m["explanation"]
                                )
                                for m in matches[:3]
                            ),
                        )
                    )
            elif age == "new":
                if ILLEGAL_NAME.search(item) or item.startswith("."):
                    result.errors.append(
                        "<{}> is not a legal map name (no spaces, @, quotes, = , "
                        "* ~ / or non-ASCII)".format(item)
                    )
                    continue
                result.outputs.append((element or "stds", item))
                current = gs.gisenv()["MAPSET"]
                existing = self.registry.find(item + "@" + current, element)
                if existing and not self.overwrite:
                    result.errors.append(
                        "Output {} map <{}> already exists; choose another name or "
                        "run i.ai with --overwrite".format(prompt, item)
                    )

    def _exists(self, name, element, planned):
        if self.registry.find(name, element):
            return True
        bare = name.split("@", 1)[0]
        return any(
            n == bare and (element is None or t in (element, "stds"))
            for t, n in planned
        )

    def _check_mapcalc(self, module, expression, planned, result):
        element = "raster" if module == "r.mapcalc" else "raster_3d"
        current = gs.gisenv()["MAPSET"]
        for statement in re.split(r"[\n;]", expression):
            match = re.match(r"\s*([A-Za-z0-9_.]+)\s*=(?!=)(.*)", statement)
            if not match:
                continue
            target, rhs = match.group(1), match.group(2)
            if (
                self.registry.find(target + "@" + current, element)
                and not self.overwrite
            ):
                result.errors.append(
                    "Output {} map <{}> already exists; choose another name or run "
                    "i.ai with --overwrite".format(element, target)
                )
            result.outputs.append((element, target))
            rhs = re.sub(r'"[^"]*"', " ", rhs)  # Quoted names are checked below.
            names = set(re.findall(r'"([^"]+)"', match.group(2)))
            for token in re.finditer(
                r"(?<![\w.])([A-Za-z_][A-Za-z0-9_.@]*)(\s*\()?", rhs
            ):
                if token.group(2) or token.group(1).lower() in MAPCALC_WORDS:
                    continue
                names.add(re.sub(r"\[.*$", "", token.group(1)))
            for name in sorted(names):
                if not self._exists(name, element, planned):
                    result.errors.append(
                        "Map <{}> used in the expression does not exist".format(name)
                    )
                    matches = self.matcher.match(name, element)
                    if matches:
                        result.suggestions.append(
                            "Existing {} maps close to <{}>: {}".format(
                                element,
                                name,
                                ", ".join(m["entity"]["fullname"] for m in matches[:3]),
                            )
                        )

    def _risk(self, command, info, result):
        name = command.module
        if name.startswith("d.") and not result.outputs:
            # Display tools draw on a monitor (d.mon) and change no data.
            return "display"
        if result.outputs or any(
            p.get("age") == "new" and p["name"] in command.params
            for p in info.get("params", [])
            if p.get("gisprompt")
        ):
            # Writing new maps or files, unless it also edits existing state.
            return "modify" if name in MODIFY_WITH_OUTPUT else "create"
        if name in READ_ONLY:
            return "read"
        if name in PRINT_ONLY:
            allowed = PRINT_ONLY[name]
            if (
                set(command.params) <= PRINT_PARAMS
                and command.flags
                and set(command.flags) <= allowed
            ):
                return "read"
            if (
                name == "g.gisenv"
                and "set" not in command.params
                and "unset" not in command.params
            ):
                return "read"
            return "modify"
        if name in READ_WHEN:
            key, allowed = READ_WHEN[name]
            if key == "flags":
                if command.flags and set(command.flags) <= allowed:
                    return "read"
            elif command.params.get(key) in allowed:
                return "read"
        return "modify"

    def validate_plan(self, commands):
        """Validate a sequence; outputs of a step are inputs for the next ones."""
        planned = set(self.planned)
        results = []
        for command in commands:
            result = self.validate(command, planned)
            planned.update(result.outputs)
            results.append(result)
        return results


def _is_dynamic(value):
    """True when a value is built at run time (shell or Python variables)."""
    return bool(re.search(r"[$`]|\{\w*\}", str(value)))


def _is_range(options):
    return len(options) == 1 and re.match(r"^-?[\d.]+-[\d.]+$", options[0])
