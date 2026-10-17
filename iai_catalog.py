"""
MODULE:    i.ai
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Catalog of every GRASS tool installed in the running session.
SPDX-License-Identifier: Unlicense

The catalog is built from the live installation, not from a fixed list:
every executable named like a GRASS tool in $GISBASE/bin, $GISBASE/scripts,
$GRASS_ADDON_BASE/{bin,scripts} and $GRASS_ADDON_PATH is included, so core
tools and addons are all visible to the assistant. Descriptions and
keywords come from the generated module_items.xml and the addons
modules.xml; a tool missing from both is described from its own
--interface-description. Full interfaces are read on demand and cached on
disk, keyed by the executable's modification time.
"""

import difflib
import json
import os
import re
import xml.etree.ElementTree as ET

import grass.script as gs
from grass.script.task import command_info

PREFIXES = {
    "d": "display",
    "db": "database",
    "g": "general",
    "i": "imagery",
    "m": "miscellaneous",
    "ps": "postscript",
    "r": "raster",
    "r3": "3D raster",
    "t": "temporal",
    "v": "vector",
}
NAME_RE = re.compile(r"^(d|db|g|i|m|ps|r|r3|t|v)\.[A-Za-z0-9_.]+$")
WORD_RE = re.compile(r"[a-z0-9]+")


def cache_dir():
    """Return the per-user cache directory for i.ai."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "i.ai")
    os.makedirs(path, exist_ok=True)
    return path


def _words(text):
    return WORD_RE.findall(text.lower())


class ModuleCatalog:
    """Every GRASS tool installed in the session, with search and interfaces."""

    def __init__(self):
        self.modules = {}  # name -> {"path", "description", "keywords", "addon"}
        self._interfaces = {}
        self._disk = {}
        self._disk_path = os.path.join(
            cache_dir(), "interfaces-{}.json".format(_grass_version_tag())
        )
        self._disk_dirty = False
        self._load_disk_cache()
        self._scan()

    def _search_dirs(self):
        gisbase = os.environ.get("GISBASE", "")
        addon_base = os.environ.get("GRASS_ADDON_BASE", "")
        dirs = [
            (os.path.join(gisbase, "bin"), False),
            (os.path.join(gisbase, "scripts"), False),
        ]
        if addon_base:
            dirs += [
                (os.path.join(addon_base, "bin"), True),
                (os.path.join(addon_base, "scripts"), True),
            ]
        for extra in os.environ.get("GRASS_ADDON_PATH", "").split(os.pathsep):
            if extra:
                dirs.append((extra, True))
        return dirs

    def _scan(self):
        for directory, addon in self._search_dirs():
            if not os.path.isdir(directory):
                continue
            for entry in sorted(os.listdir(directory)):
                name = entry[:-3] if entry.endswith(".py") else entry
                if not NAME_RE.match(name):
                    continue
                path = os.path.join(directory, entry)
                if not (os.path.isfile(path) and os.access(path, os.X_OK)):
                    continue
                # Addons override core tools of the same name, as on PATH.
                if name in self.modules and not addon:
                    continue
                self.modules[name] = {
                    "path": path,
                    "description": "",
                    "keywords": [],
                    "addon": addon,
                }
        gisbase = os.environ.get("GISBASE", "")
        self._read_items(
            os.path.join(gisbase, "gui", "wxpython", "xml", "module_items.xml"),
            "module-item",
        )
        addon_base = os.environ.get("GRASS_ADDON_BASE", "")
        if addon_base:
            self._read_items(os.path.join(addon_base, "modules.xml"), "task")
        for name, info in self.modules.items():
            if not info["description"]:
                cached = self._disk.get(name)
                if cached and cached.get("mtime") == _mtime(info["path"]):
                    info["description"] = cached["info"].get("description", "")
                    info["keywords"] = cached["info"].get("keywords", [])

    def _read_items(self, path, tag):
        if not os.path.isfile(path):
            return
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as error:
            gs.warning(_("Cannot parse <{}>: {}").format(path, error))
            return
        for item in root.iter(tag):
            name = item.get("name")
            if name not in self.modules:
                continue
            description = (item.findtext("description") or "").strip()
            keywords = (item.findtext("keywords") or "").strip()
            self.modules[name]["description"] = description
            self.modules[name]["keywords"] = [
                k.strip() for k in keywords.split(",") if k.strip()
            ]

    def _load_disk_cache(self):
        try:
            with open(self._disk_path, encoding="utf-8") as handle:
                self._disk = json.load(handle)
        except (OSError, ValueError):
            self._disk = {}

    def save(self):
        """Write new interface descriptions to the disk cache."""
        if not self._disk_dirty:
            return
        tmp = self._disk_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._disk, handle)
            os.replace(tmp, self._disk_path)
            self._disk_dirty = False
        except OSError as error:
            gs.verbose(_("Cannot write the interface cache: {}").format(error))

    def exists(self, name):
        return name in self.modules

    def names(self, prefix=None):
        """Return all tool names, optionally for one prefix such as 'r'."""
        if not prefix:
            return sorted(self.modules)
        prefix = prefix.rstrip(".") + "."
        return sorted(n for n in self.modules if n.startswith(prefix))

    def counts(self):
        """Return the number of installed tools per prefix."""
        result = {}
        for name in self.modules:
            prefix = name.split(".", 1)[0]
            result[prefix] = result.get(prefix, 0) + 1
        return dict(sorted(result.items()))

    def addons(self):
        return sorted(n for n, i in self.modules.items() if i["addon"])

    def similar(self, name, n=5):
        """Return installed tool names close to a misspelled one."""
        return difflib.get_close_matches(name, list(self.modules), n=n, cutoff=0.6)

    def interface(self, name):
        """Return the full interface of a tool, as grass.script.task.command_info does.

        :raises KeyError: When the tool is not installed
        """
        if name not in self.modules:
            raise KeyError(name)
        if name in self._interfaces:
            return self._interfaces[name]
        path = self.modules[name]["path"]
        mtime = _mtime(path)
        cached = self._disk.get(name)
        if cached and cached.get("mtime") == mtime:
            info = cached["info"]
        else:
            info = command_info(name)
            info = {
                "description": info.get("description", ""),
                "keywords": info.get("keywords", []),
                "usage": info.get("usage", ""),
                "params": info.get("params", []),
                "flags": info.get("flags", []),
            }
            self._disk[name] = {"mtime": mtime, "info": info}
            self._disk_dirty = True
        self._interfaces[name] = info
        entry = self.modules[name]
        if not entry["description"]:
            entry["description"] = info.get("description", "")
            entry["keywords"] = info.get("keywords", [])
        return info

    def describe_all(self, progress=True):
        """Read the interface of every installed tool into the disk cache.

        :return: Names of tools whose interface could not be read
        """
        failed = []
        names = sorted(self.modules)
        for i, name in enumerate(names):
            if progress:
                gs.percent(i, len(names), 2)
            try:
                self.interface(name)
            except Exception:  # noqa: BLE001 - a broken tool must not stop the scan
                failed.append(name)
        if progress:
            gs.percent(1, 1, 1)
        self.save()
        return failed

    def search(self, query, k=15, prefix=None):
        """Rank installed tools by keyword overlap with a query.

        Name, keyword and description matches are weighted in that order.
        """
        terms = set(_words(query))
        if not terms:
            return []
        scored = []
        for name, info in self.modules.items():
            if prefix and not name.startswith(prefix.rstrip(".") + "."):
                continue
            name_words = set(_words(name))
            keywords = set(_words(" ".join(info["keywords"])))
            description = set(_words(info["description"]))
            score = (
                3.0 * len(terms & name_words)
                + 2.0 * len(terms & keywords)
                + 1.0 * len(terms & description)
            )
            if query.strip() == name:
                score += 10
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [self.summary(name) for _, name in scored[:k]]

    def summary(self, name):
        info = self.modules[name]
        return {
            "name": name,
            "description": info["description"],
            "keywords": info["keywords"],
            "addon": info["addon"],
        }

    def interface_text(self, name):
        """Return a compact, model-readable rendering of a tool interface."""
        info = self.interface(name)
        lines = ["{}: {}".format(name, info.get("description", ""))]
        if info.get("keywords"):
            lines.append("keywords: " + ", ".join(info["keywords"]))
        lines.append("parameters:")
        for p in info.get("params", []):
            kind = p.get("type", "string")
            if p.get("gisprompt") and p.get("prompt"):
                kind = "{} {} map".format(p.get("age", ""), p.get("prompt")).strip()
            attrs = [kind]
            if p.get("required"):
                attrs.append("required")
            if p.get("multiple"):
                attrs.append("multiple")
            if p.get("default"):
                attrs.append("default=" + str(p["default"]))
            if p.get("values"):
                values = p["values"]
                shown = ",".join(values[:25]) + (",..." if len(values) > 25 else "")
                attrs.append("options=" + shown)
            lines.append(
                "  {} ({}): {}".format(
                    p["name"], "; ".join(attrs), p.get("description", "")
                )
            )
        flags = [
            f
            for f in info.get("flags", [])
            if f["name"] not in ("overwrite", "help", "verbose", "quiet", "ui")
        ]
        if flags:
            lines.append("flags:")
            for f in flags:
                lines.append("  -{}: {}".format(f["name"], f.get("description", "")))
        return "\n".join(lines)


def _mtime(path):
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


def _grass_version_tag():
    version = os.environ.get("GRASS_VERSION")
    if not version:
        try:
            version = gs.version()["version"]
        except Exception:  # noqa: BLE001 - outside a session the tag is cosmetic
            version = "unknown"
    return re.sub(r"[^A-Za-z0-9.]+", "_", version)
