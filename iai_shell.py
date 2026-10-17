"""
MODULE:    i.ai
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Command-line tools for i.ai: catalog, read-only probes, validation.
SPDX-License-Identifier: Unlicense

Every executable on PATH is available to the assistant, like the GRASS
tools: commands are searched through the man page index (apropos,
whatis) and described from their man page or --help. A read-only probe
runs inspection commands (gdalinfo, ogrinfo, pdfinfo, ls...) without a
shell. Shell lines and whole scripts (bash or Python) written by the
model are validated before they are stored or run: syntax, existence of
every command, the GRASS calls inside them, Python imports, and patterns
that are refused outright (sudo, recursive removal of broad paths, piping
downloads into a shell, ...). Each script also gets a risk level.
"""

import ast
import difflib
import importlib.util
import os
import re
import shlex
import shutil
import subprocess

from iai_validate import POSITIONAL, Command

# Tools the prompt names by category, when installed.
CATEGORIES = {
    "LaTeX": ["latexmk", "pdflatex", "xelatex", "lualatex", "bibtex", "biber", "kpsewhich"],
    "diagrams": ["dot", "neato", "inkscape"],
    "GDAL/OGR": ["gdalinfo", "gdal_translate", "gdalwarp", "gdalbuildvrt", "gdaldem",
                 "gdal_calc.py", "gdal_rasterize", "gdal_polygonize.py", "ogrinfo", "ogr2ogr"],
    "PDF and images": ["pdfinfo", "pdftoppm", "pdftotext", "gs", "magick", "convert", "pandoc"],
    "data": ["python3", "jq", "awk", "sed", "sort", "cut", "bc", "sqlite3"],
    "network": ["curl", "wget", "aria2c", "rclone"],
    "archives": ["tar", "unzip", "zip", "gzip", "xz", "zstd"],
}  # fmt: skip

PYTHON_PACKAGES = (
    "numpy", "scipy", "pandas", "matplotlib", "geopandas", "shapely", "pyproj",
    "rasterio", "fiona", "xarray", "zarr", "netCDF4", "h5py", "sklearn", "osgeo",
    "requests", "PIL", "skimage",
)  # fmt: skip

# Commands that only inspect, for the read-only probe.
READ_ONLY_COMMANDS = {
    "cat", "df", "du", "file", "find", "gdalinfo", "gdalsrsinfo", "grep", "head",
    "identify", "jq", "kpsewhich", "ls", "md5sum", "ncdump", "ogrinfo",
    "pdffonts", "pdfinfo", "pdftotext", "realpath", "sha256sum", "stat", "tail",
    "tree", "wc", "whatis", "which", "zipinfo", "h5dump", "h5ls",
}  # fmt: skip
FIND_FORBIDDEN = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"}

# Paths whose content must never reach a language model.
SECRET_PATHS = re.compile(
    r"(^|/)(\.ssh|\.gnupg|\.aws|\.netrc|\.pgpass|\.claude|\.config/gh|"
    r"\.local/share/opencode|\.docker|\.kube)(/|$)|(^|/)\.env$|"
    r"(id_rsa|id_ed25519|token|secret|credential|password)",
    re.IGNORECASE,
)

SHELL_WORDS = {
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
    "case", "esac", "in", "function", "select", "time", "!", "{", "}", "[[", "]]",
    "coproc",
}  # fmt: skip
BUILTINS = {
    ".", ":", "[", "alias", "bg", "bind", "break", "builtin", "cd", "command",
    "continue", "declare", "dirs", "disown", "echo", "enable", "eval", "exec",
    "exit", "export", "false", "fg", "getopts", "hash", "help", "history",
    "jobs", "kill", "let", "local", "mapfile", "popd", "printf", "pushd", "pwd",
    "read", "readarray", "readonly", "return", "set", "shift", "shopt",
    "source", "test", "trap", "true", "type", "typeset", "ulimit", "umask",
    "unalias", "unset", "wait",
}  # fmt: skip

# Refused in any script or shell step, whatever the context.
FORBIDDEN = [
    (re.compile(r"(^|[\s;&|(])sudo\b"), "sudo is not allowed"),
    (re.compile(r"(^|[\s;&|(])su\s"), "su is not allowed"),
    (
        re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*"
                   r"(/|~|\$HOME|\"\$HOME\"|\$\{HOME\}|\*|\.\.?)(/?\s|/?$|/?[;&|])"),
        "recursive removal of /, ~, $HOME, . or * is not allowed",
    ),
    (re.compile(r"\b(mkfs|fdisk|parted|wipefs|shred)\b"), "disk tools are not allowed"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/"), "writing to devices is not allowed"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|mmcblk)"), "writing to devices is not allowed"),
    (re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b"),
     "piping a download into a shell is not allowed"),
    (re.compile(r"\bchmod\s+(-R\s+)?[0-7]*777\b"), "chmod 777 is not allowed"),
    (re.compile(r"\bgit\s+push\b"), "git push is not allowed"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "fork bombs are not allowed"),
    (re.compile(r"\b(shutdown|reboot|poweroff|halt)\b"), "power commands are not allowed"),
    (re.compile(r"\bcrontab\b"), "editing crontabs is not allowed"),
    (re.compile(r"\b(apt|apt-get|dnf|yum|pacman)\s+(install|remove|purge)"),
     "installing system packages is not allowed; list what is missing instead"),
]  # fmt: skip

MODIFY_PATTERNS = re.compile(
    r"(^|[\s;&|(])(rm|mv|g\.remove|g\.rename|db\.execute|db\.droptable|t\.remove|"
    r"v\.db\.dropcolumn|v\.db\.droptable|r\.mask|v\.edit)\b"
)
NETWORK_PATTERNS = re.compile(
    r"\b(curl|wget|aria2c|rclone|scp|rsync|ssh|ftp|sftp)\b|\brequests\.(get|post)|"
    r"urllib\.request|urlopen\(|\bhttpx\."
)
GRASS_CALLS = {
    "run_command", "read_command", "parse_command", "write_command",
    "start_command", "feed_command", "pipe_command", "exec_command",
}  # fmt: skip


def _path_dirs():
    return [d for d in os.environ.get("PATH", "").split(os.pathsep) if os.path.isdir(d)]


class CommandCatalog:
    """Every executable on PATH, searchable through the man page index."""

    def __init__(self):
        self.commands = {}
        for directory in _path_dirs():
            try:
                entries = os.listdir(directory)
            except OSError:
                continue
            for entry in entries:
                if entry in self.commands:
                    continue  # The first match on PATH wins, as in the shell.
                path = os.path.join(directory, entry)
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    self.commands[entry] = path
        self._whatis = {}

    def exists(self, name):
        return name in self.commands or bool(shutil.which(name))

    def similar(self, name, n=5):
        return difflib.get_close_matches(name, list(self.commands), n=n, cutoff=0.75)

    def categories(self):
        """Return the installed tools of each prompt category."""
        return {
            category: [c for c in names if c in self.commands]
            for category, names in CATEGORIES.items()
        }

    @staticmethod
    def python_packages():
        found = []
        for name in PYTHON_PACKAGES:
            try:
                if importlib.util.find_spec(name) is not None:
                    found.append(name)
            except (ImportError, ValueError):
                continue
        return found

    def describe(self, names):
        """Return one-line descriptions from whatis, for installed names."""
        wanted = [n for n in names if n not in self._whatis and n in self.commands]
        if wanted and shutil.which("whatis"):
            result = _run(["whatis", "-l", *wanted], timeout=20)
            for line in result.splitlines():
                match = re.match(r"^(\S+) \(([^)]*)\)\s+- (.*)$", line)
                if match and match.group(1) in wanted:
                    self._whatis.setdefault(match.group(1), match.group(3).strip())
        return {n: self._whatis.get(n, "") for n in names}

    def search(self, query, k=20):
        """Search installed commands by keywords in their man page summaries."""
        words = [w for w in re.findall(r"[A-Za-z0-9_.+-]+", query) if len(w) > 1]
        if not words:
            return []
        found = {}
        if shutil.which("apropos"):
            # All words first, then any word, so precise matches rank first.
            for args in (["-a", *words], words):
                output = _run(["apropos", "-l", "-s", "1:6:8", *args], timeout=20)
                for line in output.splitlines():
                    match = re.match(r"^(\S+) \(([^)]*)\)\s+- (.*)$", line)
                    if not match:
                        continue
                    name = match.group(1)
                    if name in self.commands and name not in found:
                        found[name] = match.group(3).strip()
                if len(found) >= k:
                    break
        for word in words:
            for name in self.commands:
                if word.lower() in name.lower() and name not in found:
                    found[name] = self.describe([name])[name]
                if len(found) >= 2 * k:
                    break
        return [{"command": n, "description": d} for n, d in list(found.items())[:k]]

    def help_text(self, name, max_chars=6000):
        """Return the man page, or --help output, of an installed command."""
        if not self.exists(name):
            return None
        env = dict(os.environ, MANWIDTH="100", MANPAGER="cat", PAGER="cat")
        text = ""
        if shutil.which("man"):
            text = _run(["man", "-P", "cat", name], timeout=20, env=env)
        if not text.strip():
            text = _run([name, "--help"], timeout=10)
        text = re.sub(r".\x08", "", text)  # Strip man page overstrike.
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"
        return text or "No help available for {}.".format(name)

    def prompt_summary(self):
        lines = [
            "Command-line tools: {} executables on PATH (find them with "
            "search_commands, read their manual with command_help).".format(
                len(self.commands)
            )
        ]
        for category, names in self.categories().items():
            if names:
                lines.append("  {}: {}".format(category, ", ".join(names)))
        packages = self.python_packages()
        if packages:
            lines.append("  Python packages: " + ", ".join(packages))
        return "\n".join(lines)


def _run(argv, timeout=30, env=None, cwd=None):
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return "Cannot run {}: {}".format(argv[0], error)
    return result.stdout if result.stdout.strip() else result.stderr


def probe(command_line, cwd=None, timeout=60):
    """Run a read-only inspection command without a shell.

    :return: Output, or the reason it was refused
    """
    lexer = shlex.shlex(command_line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as error:
        return "Refused: cannot parse the command ({})".format(error)
    if not tokens:
        return "Refused: empty command"
    if any(
        t in ("|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "(", ")") for t in tokens
    ):
        return (
            "Refused: pipes, redirections and command lists are not allowed in probes"
        )
    name = tokens[0]
    args = tokens[1:]
    version_or_help = args and all(
        a in ("--version", "-V", "--help", "-h") for a in args
    )
    if name not in READ_ONLY_COMMANDS and not version_or_help:
        return (
            "Refused: {} is not a read-only inspection command. Allowed: {}; any "
            "command with --version or --help.".format(
                name, ", ".join(sorted(READ_ONLY_COMMANDS))
            )
        )
    if name == "find" and FIND_FORBIDDEN & set(args):
        return "Refused: find actions that run or delete are not allowed"
    if name == "tail" and any(a.startswith("-f") or a == "--follow" for a in args):
        return "Refused: tail -f never ends"
    for arg in args:
        if SECRET_PATHS.search(os.path.expanduser(arg)):
            return "Refused: {} may hold credentials".format(arg)
    if not shutil.which(name):
        return "Command {} is not installed".format(name)
    output = _run(
        [name, *[os.path.expanduser(a) for a in args]], timeout=timeout, cwd=cwd
    )
    return output or "(no output)"


class ScriptCheck:
    def __init__(self, kind):
        self.kind = kind
        self.errors = []
        self.warnings = []
        self.risk = "create"
        self.grass_outputs = set()
        self.commands = set()

    @property
    def valid(self):
        return not self.errors

    def to_dict(self):
        return {
            "kind": self.kind,
            "valid": self.valid,
            "risk": self.risk,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class ShellValidator:
    """Validate shell lines and scripts before they are stored or run."""

    def __init__(self, commands, catalog, grass_validator):
        self.commands = commands
        self.catalog = catalog
        self.grass_validator = grass_validator

    def check(
        self, code, kind, functions=(), planned=None, local_files=(), known_outputs=None
    ):
        """Validate code.

        :param str kind: bash or python
        :param functions: Shell functions defined elsewhere (e.g. in 00_env.sh)
        :param set planned: (type, name) GRASS outputs of earlier steps
        :param local_files: Script files of the pipeline, callable by path
        :param set known_outputs: (type, name) maps earlier runs of this
            pipeline created; any other existing map a script writes is an
            error, so a pipeline never overwrites the user's own maps
        :rtype: ScriptCheck
        """
        result = ScriptCheck(kind)
        planned = set() if planned is None else planned
        shell_text = code if kind != "python" else _python_shell_strings(code)
        for pattern, message in FORBIDDEN:
            if pattern.search(shell_text):
                result.errors.append(message)
        self._local_files = {os.path.basename(f) for f in local_files} | set(
            local_files
        )
        if kind == "python":
            self._check_python(code, result, planned)
        else:
            self._check_bash(code, result, set(functions), planned, set(local_files))
        if MODIFY_PATTERNS.search(shell_text) or (
            kind == "python" and re.search(r"g\.remove|os\.remove|shutil\.rmtree", code)
        ):
            result.risk = "modify"
        if NETWORK_PATTERNS.search(code):
            result.risk = "network" if result.risk != "modify" else "modify"
        if known_outputs is not None:
            registry = self.grass_validator.registry
            mapset = self.grass_validator.current_mapset()
            for element, name in sorted(result.grass_outputs):
                if (element, name) in known_outputs or "$" in name:
                    continue
                if registry.find(
                    "{}@{}".format(name, mapset), None if element == "stds" else element
                ):
                    result.errors.append(
                        "The script writes {} map <{}>, which already exists in mapset "
                        "<{}> and was not created by this pipeline; use a new name".format(
                            element, name, mapset
                        )
                    )
        return result

    def _check_bash(self, code, result, functions, planned, local_files):
        syntax = subprocess.run(
            ["bash", "-n"], input=code, capture_output=True, text=True, check=False
        )
        if syntax.returncode != 0:
            result.errors.append("bash syntax: " + syntax.stderr.strip()[-600:])
            return
        functions |= set(
            re.findall(r"(?m)^\s*(?:function\s+)?([A-Za-z_][\w.-]*)\s*\(\)", code)
        )
        functions |= set(re.findall(r"(?m)^\s*function\s+([A-Za-z_][\w.-]*)", code))
        for line_number, words in _simple_commands(code):
            name = words[0]
            if (
                name
                in (
                    "grass_run",
                    "command",
                    "exec",
                    "time",
                    "nice",
                    "nohup",
                    "env",
                    "timeout",
                    "xargs",
                    "mmrun",
                )
                and len(words) > 1
            ):
                rest, skip = [], False
                for w in words[1:]:
                    if skip:
                        skip = False
                    elif rest:
                        rest.append(w)
                    elif name == "xargs" and re.fullmatch(r"-[PnILdsaEe]", w):
                        skip = True  # Option with a separate argument.
                    elif not re.match(r"^-|^[A-Z_]+=", w):
                        rest.append(w)
                if name == "timeout" and rest and re.match(r"^\d", rest[0]):
                    rest = rest[1:]
                if name == "mmrun" and "--" in words:
                    rest = words[words.index("--") + 1 :]
                if not rest:
                    continue
                words, name = rest, rest[0]
            if name in functions or name in BUILTINS or name in SHELL_WORDS:
                continue
            if (
                "$" in name
                or "`" in name
                or re.match(r"^[A-Za-z_]\w*(\[.*\])?\+?=", name)
            ):
                continue
            if "/" in name:
                stem = name.replace("$SCRIPTS_DIR/", "scripts/").replace(
                    "${SCRIPTS_DIR}/", "scripts/"
                )
                if stem in local_files or os.path.basename(stem) in local_files:
                    continue
                if os.path.isabs(name) and os.access(name, os.X_OK):
                    continue
                result.warnings.append(
                    "line {}: {} is not a pipeline script; make sure it exists".format(
                        line_number, name
                    )
                )
                continue
            result.commands.add(name)
            if self.catalog.exists(name):
                self._check_grass(line_number, words, result, planned)
                continue
            if name == "grass":
                continue
            if not self.commands.exists(name):
                similar = self.commands.similar(name) + self.catalog.similar(name)
                result.errors.append(
                    "line {}: command {} is not installed{}".format(
                        line_number,
                        name,
                        "; close names: " + ", ".join(similar[:5]) if similar else "",
                    )
                )

    def _check_grass(self, line_number, words, result, planned):
        params, flags = {}, ""
        for word in words[1:]:
            if word.startswith("--"):
                continue
            if word.startswith("-") and "=" not in word and len(word) > 1:
                flags += word[1:]
            elif "=" in word and re.match(r"^[a-z_0-9]+=", word):
                key, value = word.split("=", 1)
                params[key] = value
            elif POSITIONAL not in params:
                params[POSITIONAL] = word
        if "help" in flags or any(
            w in ("--help", "--interface-description") for w in words
        ):
            return
        command = Command(
            words[0], params, flags if re.match(r"^[a-zA-Z]*$", flags) else ""
        )
        check = self.grass_validator.validate(command, planned, script_mode=True)
        planned.update(check.outputs)
        result.grass_outputs.update(check.outputs)
        for error in check.errors:
            result.errors.append(
                "line {} ({}): {}".format(line_number, words[0], error)
            )
        for warning in check.warnings:
            result.warnings.append(
                "line {} ({}): {}".format(line_number, words[0], warning)
            )
        for suggestion in check.suggestions:
            result.warnings.append(
                "line {}: suggestion: {}".format(line_number, suggestion)
            )

    def _check_python(self, code, result, planned):
        self._code = code
        try:
            tree = ast.parse(code)
        except SyntaxError as error:
            result.errors.append(
                "Python syntax: line {}: {}".format(error.lineno, error.msg)
            )
            return
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for name in names:
                    top = name.split(".")[0]
                    if not top or getattr(node, "level", 0):
                        continue
                    try:
                        found = importlib.util.find_spec(top) is not None
                    except (ImportError, ValueError):
                        found = False
                    if (
                        found
                        or top + ".py" in self._local_files
                        or top in self._local_files
                    ):
                        continue
                    message = "line {}: Python module {} is not installed".format(
                        node.lineno, top
                    )
                    if "sys.path" in code:
                        # Found at run time through a path the script adds.
                        result.warnings.append(
                            message + " (unless on the added sys.path)"
                        )
                    else:
                        result.errors.append(message)
            if isinstance(node, ast.Call):
                self._check_python_call(node, result, planned)

    def _check_python_call(self, node, result, planned):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        module = None
        if name in GRASS_CALLS and node.args and isinstance(node.args[0], ast.Constant):
            module = node.args[0].value
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in ("tools", "t")
            and re.match(r"^(d|db|g|i|m|ps|r|r3|t|v)_\w+$", name)
        ):
            module = name.replace("_", ".")
            if not self.catalog.exists(module):
                return  # Tools(...) method that is not a tool call.
        if (
            name in ("run", "check_call", "check_output", "Popen", "call")
            and isinstance(func, ast.Attribute)
            and getattr(func.value, "id", "") == "subprocess"
            and node.args
            and isinstance(node.args[0], (ast.List, ast.Tuple))
            and node.args[0].elts
            and isinstance(node.args[0].elts[0], ast.Constant)
        ):
            command = str(node.args[0].elts[0].value)
            if (
                "/" not in command
                and not self.commands.exists(command)
                and not self.catalog.exists(command)
            ):
                result.errors.append(
                    "line {}: command {} is not installed".format(node.lineno, command)
                )
            return
        if not isinstance(module, str):
            return
        params, flags = {}, ""
        for keyword in node.keywords:
            if keyword.arg is None:
                return  # **kwargs: cannot check.
            if keyword.arg in (
                "overwrite",
                "quiet",
                "verbose",
                "env",
                "stdout",
                "stderr",
                "stdin",
                "errors",
                "superquiet",
            ):
                continue
            value = keyword.value
            if keyword.arg == "flags":
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    flags = value.value
                continue
            if isinstance(value, ast.Constant):
                params[keyword.arg] = value.value
            elif isinstance(value, (ast.List, ast.Tuple)) and all(
                isinstance(e, ast.Constant) for e in value.elts
            ):
                params[keyword.arg] = [e.value for e in value.elts]
            else:
                params[keyword.arg] = "$dynamic"  # Not a literal: name check only.
        command = Command(module, params, flags)
        check = self.grass_validator.validate(command, planned, script_mode=True)
        planned.update(check.outputs)
        result.grass_outputs.update(check.outputs)
        for error in check.errors:
            result.errors.append("line {} ({}): {}".format(node.lineno, module, error))
        for warning in check.warnings:
            result.warnings.append(
                "line {} ({}): {}".format(node.lineno, module, warning)
            )


def _simple_commands(code, offset=0):
    """Yield (line number, words) for each simple command in a bash script.

    This is a light tokenizer, not a full bash parser: it splits on
    operators, skips comments, assignments, test brackets, loop and case
    headers, and is used only to find the command names a script calls.
    Heredocs fed to bash or sh (such as 'grass_run bash -s <<EOF') are
    checked as scripts too; other heredocs are data and are skipped.
    """
    lines = code.splitlines()
    i, in_case = 0, 0
    while i < len(lines):
        number = i + 1 + offset
        line = lines[i]
        while i + 1 < len(lines) and (
            line.rstrip().endswith("\\") or _open_quote(line)
        ):
            i += 1
            if line.rstrip().endswith("\\"):
                line = line.rstrip()[:-1] + " " + lines[i]
            else:
                line = line + "\n" + lines[i]
        i += 1
        heredoc = re.search(r"<<-?\s*(['\"]?)(\w+)\1", line)
        body = []
        if heredoc:
            end = heredoc.group(2)
            body_start = i
            while i < len(lines) and lines[i].strip() != end:
                body.append(lines[i])
                i += 1
            i += 1  # The terminator line.
        if in_case and re.match(r"^\s*[^()=]*\)", line):
            line = re.sub(r"^\s*[^()=]*\)", "", line, count=1)  # Case pattern.
        for words in _commands_in_line(line):
            if words[0] == "case":
                in_case += 1
                continue
            if words[0] == "esac":
                in_case = max(0, in_case - 1)
                continue
            yield number, words
        if heredoc and re.search(r"\b(ba)?sh\b(\s+-s)?\s*<<", line):
            text = "\n".join(body)
            if not heredoc.group(1):  # Unquoted: \$ is a literal dollar.
                text = text.replace("\\$", "$")
            yield from _simple_commands(text, offset=body_start + offset)


def _commands_in_line(line):
    """Return the word lists of the simple commands on one logical line."""
    line = re.sub(r"\$\(\([^()]*(\([^()]*\)[^()]*)*\)\)", "0", line)  # $(( ... ))
    line = _protect_braces(line)
    line = re.sub(r"(?<![\w$])\d+(?=[<>])", "", line)  # File descriptors: 2>&1.
    line = re.sub(r"^\s*\(\(.*?\)\)", " ", line)  # (( ... )) commands.
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    found, words = [], []
    expect_command, skip_next, ignore, pattern = True, False, False, False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if pattern:
            # Inside a one-line case: skip the pattern up to its ")".
            if token.startswith(")"):
                pattern, expect_command, words = False, True, []
            continue
        if token == "in" and found and found[-1] == ["case"]:
            pattern, ignore = True, False
            continue
        if token == ";;" or token == ";;&":
            if words:
                found.append(words)
            words, expect_command, pattern = [], True, True
            continue
        if re.fullmatch(r"[;&|()]+", token):
            # A lone word before ")" is a case pattern, not a command.
            if words and not (token.startswith(")") and len(words) == 1 and not found):
                found.append(words)
            words, expect_command, ignore = [], True, False
            continue
        if ignore:
            continue
        if re.fullmatch(r"\d?[<>]+&?\d?", token):
            skip_next = True  # The redirection target is not a command.
            continue
        if expect_command:
            if token in ("for", "select", "[[", "[", "test", "function"):
                ignore = True  # Loop headers and tests hold no command.
                continue
            if token in SHELL_WORDS:
                if token == "case":
                    found.append(["case"])
                    ignore = True
                elif token == "esac":
                    found.append(["esac"])
                continue
            if re.match(r"^[A-Za-z_]\w*(\[[^\]]*\])?\+?=", token):
                continue  # Assignment before the command.
            expect_command = False
        words.append(token)
    if words:
        found.append(words)
    return found


def _open_quote(text):
    """True when a line ends inside an unterminated quote."""
    try:
        lexer = shlex.shlex(text, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        list(lexer)
    except ValueError as error:
        return "quotation" in str(error)
    return False


def _protect_braces(line):
    """Remove spaces inside ${...} so the words stay one token."""
    out, depth = [], 0
    i = 0
    while i < len(line):
        if line.startswith("${", i):
            depth += 1
            out.append("${")
            i += 2
            continue
        char = line[i]
        if depth and char == "}":
            depth -= 1
        elif depth and char.isspace():
            char = "_"
        out.append(char)
        i += 1
    return "".join(out)


def _python_shell_strings(code):
    """Return the shell command strings of a Python script, joined.

    Only text that reaches a shell (os.system, subprocess with a string or
    an argument list) is checked against the forbidden patterns; ordinary
    Python code and prose are not.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        owner = getattr(getattr(func, "value", None), "id", "")
        if (owner, name) not in {
            ("os", "system"), ("os", "popen"), ("subprocess", "run"),
            ("subprocess", "call"), ("subprocess", "check_call"),
            ("subprocess", "check_output"), ("subprocess", "Popen"),
        }:  # fmt: skip
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append(first.value)
        elif isinstance(first, (ast.List, ast.Tuple)):
            found.append(
                " ".join(
                    str(e.value) for e in first.elts if isinstance(e, ast.Constant)
                )
            )
    return "\n".join(found)


class ShellStep:
    """A shell command line in a plan, next to GRASS commands."""

    module = "shell"

    def __init__(self, line, purpose=""):
        self.line = line.strip()
        self.purpose = purpose or ""
        self.params = {}
        self.flags = ""

    def shell(self, overwrite=False):
        return self.line

    def to_dict(self):
        return {"shell": self.line, "purpose": self.purpose}


class ShellResult:
    """Validation of a ShellStep, shaped like a GRASS ValidationResult."""

    def __init__(self, step, check):
        self.command = step
        self.errors = check.errors
        self.warnings = check.warnings
        self.suggestions = []
        self.risk = check.risk
        self.outputs = sorted(check.grass_outputs)

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
        }


def plan_item(item):
    """Build a GRASS Command or a ShellStep from a plan item."""
    if isinstance(item, dict) and item.get("shell"):
        return ShellStep(item["shell"], item.get("purpose", ""))
    return Command.from_any(item)


def validate_steps(items, validator, shell_validator, planned=None):
    """Validate a mixed plan; outputs of a step are inputs for the next ones."""
    planned = set(validator.planned) if planned is None else planned
    results = []
    for item in items:
        if isinstance(item, ShellStep):
            if shell_validator is None:
                check = ScriptCheck("bash")
                check.errors.append("Shell steps are not enabled")
            else:
                check = shell_validator.check(item.line, "bash", planned=planned)
            result = ShellResult(item, check)
        else:
            result = validator.validate(item, planned)
            planned.update(result.outputs)
        results.append(result)
    return results
