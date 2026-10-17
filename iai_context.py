"""
MODULE:    i.ai
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Session context and dialog history for i.ai.
SPDX-License-Identifier: Unlicense

The context manager snapshots the GRASS session (project, mapset, CRS,
computational region, mask, available maps and installed tools) for the
prompt, and keeps the dialog history. Named sessions are stored as JSON
under $XDG_DATA_HOME/i.ai/sessions so a conversation can continue across
calls.
"""

import json
import os
import re
import time

import grass.script as gs

MAX_TURNS = 12
MAX_LISTED = 60


def data_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, "i.ai")
    os.makedirs(path, exist_ok=True)
    return path


class ContextManager:
    """Session snapshot plus dialog history."""

    def __init__(self, catalog, registry):
        self.catalog = catalog
        self.registry = registry
        self.history = []  # dicts: role, content
        self.last_plan = []  # command dicts of the last proposed plan
        self.environment = {}
        self.refresh()

    def refresh(self):
        env = gs.gisenv()
        info = {
            "grass_version": os.environ.get("GRASS_VERSION")
            or gs.version().get("version"),
            "gisdbase": env.get("GISDBASE"),
            "project": env.get("LOCATION_NAME"),
            "mapset": env.get("MAPSET"),
            "search_path": gs.mapsets(search_path=True),
        }
        try:
            try:
                proj = gs.parse_command("g.proj", flags="p", format="shell")
            except Exception:  # noqa: BLE001 - before GRASS 8.5 only -g exists
                proj = gs.parse_command("g.proj", flags="g")
            info["crs"] = {
                k: proj[k]
                for k in ("name", "srid", "proj", "zone", "datum", "units", "epsg")
                if k in proj
            }
        except Exception:  # noqa: BLE001 - an XY project has no CRS
            info["crs"] = {"name": "XY (unprojected)"}
        region = gs.region(region3d=True)
        info["region"] = {
            k: region[k]
            for k in (
                "n",
                "s",
                "e",
                "w",
                "nsres",
                "ewres",
                "rows",
                "cols",
                "t",
                "b",
                "depths",
            )
            if k in region
        }
        try:
            status = gs.parse_command("r.mask.status", format="json")
            info["mask"] = bool(status.get("present"))
            info["mask_name"] = status.get("name")
        except Exception:  # noqa: BLE001 - r.mask.status is new in GRASS 8.5
            info["mask"] = bool(
                gs.find_file("MASK", element="cell", mapset=env.get("MAPSET"))["name"]
            )
        self.environment = info

    def add_turn(self, role, content):
        self.history.append({"role": role, "content": content})
        if len(self.history) > 2 * MAX_TURNS:
            self.history = self.history[-2 * MAX_TURNS :]

    def for_prompt(self):
        """Return the session description included in every system prompt."""
        env = self.environment
        lines = [
            "GRASS {} session: project <{}>, mapset <{}>, search path {}.".format(
                env["grass_version"],
                env["project"],
                env["mapset"],
                ", ".join(env["search_path"]),
            ),
            "CRS: " + json.dumps(env.get("crs", {})),
            "Computational region: " + json.dumps(env.get("region", {})),
            "Raster mask active: {}.".format("yes" if env.get("mask") else "no"),
        ]
        counts = self.registry.counts()
        for element in (
            "raster",
            "vector",
            "raster_3d",
            "group",
            "strds",
            "stvds",
            "str3ds",
        ):
            names = [e["fullname"] for e in self.registry.of_type(element)]
            if not names:
                continue
            shown = names[:MAX_LISTED]
            more = (
                " ... ({} more; use list_maps)".format(len(names) - MAX_LISTED)
                if len(names) > MAX_LISTED
                else ""
            )
            lines.append(
                "{} maps ({}): {}{}".format(
                    element, counts[element], ", ".join(shown), more
                )
            )
        if not self.registry.entities:
            lines.append("No maps in the search path yet.")
        tools = ", ".join(
            "{}.* {}".format(prefix, count)
            for prefix, count in self.catalog.counts().items()
        )
        lines.append(
            "Installed GRASS tools: {} in total ({}), including {} addons.".format(
                len(self.catalog.modules), tools, len(self.catalog.addons())
            )
        )
        addons = self.catalog.addons()
        if addons:
            lines.append("Installed addons: " + ", ".join(addons))
        return "\n".join(lines)

    def summary(self):
        """Return the environment as a dict, for format=json."""
        return {
            **self.environment,
            "maps": self.registry.counts(),
            "tools": {
                "total": len(self.catalog.modules),
                "by_prefix": self.catalog.counts(),
                "addons": self.catalog.addons(),
            },
        }


class SessionStore:
    """Named dialog sessions stored as JSON files."""

    def __init__(self):
        self.directory = os.path.join(data_dir(), "sessions")
        os.makedirs(self.directory, exist_ok=True)

    def _path(self, name):
        if not re.match(r"^[A-Za-z0-9_.-]+$", name):
            gs.fatal(
                _(
                    "Session name <{}> may only use letters, digits, '.', '_' and '-'"
                ).format(name)
            )
        return os.path.join(self.directory, name + ".json")

    def load(self, name, context):
        path = self._path(name)
        if not os.path.exists(path):
            return False
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as error:
            gs.fatal(_("Cannot read session <{}>: {}").format(name, error))
        context.history = data.get("history", [])[-2 * MAX_TURNS :]
        context.last_plan = data.get("last_plan", [])
        return True

    def save(self, name, context):
        path = self._path(name)
        tmp = path + ".tmp"
        data = {
            "saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "project": context.environment.get("project"),
            "mapset": context.environment.get("mapset"),
            "history": context.history,
            "last_plan": context.last_plan,
        }
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1)
            os.replace(tmp, path)
        except OSError as error:
            gs.warning(_("Cannot save session <{}>: {}").format(name, error))
