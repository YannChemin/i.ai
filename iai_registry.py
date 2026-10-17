"""
MODULE:    i.ai
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Registry of the data maps visible in the session, with fuzzy matching.
SPDX-License-Identifier: Unlicense

The registry lists every map in the mapset search path. The matcher maps
the names a user or a model writes ("the dem", "landsat band 4") to real
maps, by exact, case-insensitive, synonym and fuzzy matching, and returns
each match with a confidence and an explanation. Validation uses it to
suggest the real map when a command names one that does not exist.
"""

import difflib
import re

import grass.script as gs

# Element types listed by g.list, as named in GRASS gisprompt "prompt" fields.
ELEMENTS = ("raster", "vector", "raster_3d", "group")
TEMPORAL = ("strds", "stvds", "str3ds")

# Common GIS words for the same thing, used to match user wording to map names.
SYNONYMS = {
    "elevation": ["dem", "dtm", "dsm", "height", "altitude", "srtm", "terrain"],
    "landcover": ["land_cover", "landuse", "land_use", "lulc", "lc", "corine"],
    "ndvi": ["vegetation_index", "vi", "greenness"],
    "roads": ["road", "streets", "highways", "network"],
    "streams": ["rivers", "river", "stream", "hydro", "drainage", "waterways"],
    "boundary": ["border", "admin", "limits", "outline", "aoi"],
    "buildings": ["building", "footprints", "houses"],
    "slope": ["gradient", "steepness"],
    "aspect": ["exposition", "orientation"],
    "soils": ["soil", "pedology"],
    "geology": ["lithology", "rocks"],
    "precipitation": ["rain", "rainfall", "precip", "pr"],
    "temperature": ["temp", "tas", "lst"],
}


def _normalize(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class EntityRegistry:
    """Maps visible in the session: name, type, mapset and full name."""

    def __init__(self, temporal=True):
        self.entities = []
        self.refresh(temporal)

    def refresh(self, temporal=True):
        self.entities = []
        for element in ELEMENTS:
            try:
                grouped = gs.list_grouped(element)
            except Exception:  # noqa: BLE001 - an unreadable mapset must not stop i.ai
                continue
            for mapset, names in grouped.items():
                for name in names:
                    self.entities.append(
                        {
                            "name": name,
                            "type": element,
                            "mapset": mapset,
                            "fullname": "{}@{}".format(name, mapset),
                        }
                    )
        if temporal:
            self._add_temporal()

    def _add_temporal(self):
        for kind in TEMPORAL:
            try:
                output = gs.read_command(
                    "t.list", type=kind, columns="name,mapset", quiet=True
                )
            except Exception:  # noqa: BLE001 - no temporal database is normal
                return
            for line in output.splitlines()[1:]:
                parts = line.strip().split("|")
                if len(parts) >= 2 and parts[0]:
                    self.entities.append(
                        {
                            "name": parts[0],
                            "type": kind,
                            "mapset": parts[1],
                            "fullname": "{}@{}".format(parts[0], parts[1]),
                        }
                    )

    def of_type(self, element):
        return [e for e in self.entities if e["type"] == element]

    def names(self, element):
        return sorted({e["name"] for e in self.of_type(element)})

    def find(self, name, element=None):
        """Return the entity for an exact name or name@mapset, else None."""
        if "@" in name:
            matches = [e for e in self.entities if e["fullname"] == name]
        else:
            matches = [e for e in self.entities if e["name"] == name]
        if element:
            matches = [e for e in matches if e["type"] == element]
        return matches[0] if matches else None

    def counts(self):
        result = {}
        for entity in self.entities:
            result[entity["type"]] = result.get(entity["type"], 0) + 1
        return result


class EntityMatcher:
    """Match free wording to registered maps, with a confidence per match."""

    def __init__(self, registry, fuzzy_threshold=0.6, max_results=5):
        self.registry = registry
        self.fuzzy_threshold = fuzzy_threshold
        self.max_results = max_results
        self._synonym_index = {}
        for canonical, words in SYNONYMS.items():
            group = {canonical, *words}
            for word in group:
                self._synonym_index.setdefault(word, set()).update(group)

    def match(self, text, element=None):
        """Return matches sorted by confidence.

        :param str text: A map name or the user's words for it
        :param str element: Restrict to raster, vector, etc.
        :return: List of dicts with entity, confidence, match_type, explanation
        """
        candidates = (
            self.registry.of_type(element) if element else self.registry.entities
        )
        if not candidates or not text.strip():
            return []
        wanted = _normalize(text.split("@", 1)[0])
        results = {}

        def keep(entity, confidence, kind, why):
            key = (entity["fullname"], entity["type"])
            if key not in results or results[key]["confidence"] < confidence:
                results[key] = {
                    "entity": entity,
                    "confidence": round(confidence, 2),
                    "match_type": kind,
                    "explanation": why,
                }

        wanted_words = set(wanted.split("_"))
        synonyms = set()
        for word in wanted_words:
            synonyms |= self._synonym_index.get(word, set())
        for entity in candidates:
            name = entity["name"]
            norm = _normalize(name)
            if name == text or entity["fullname"] == text:
                keep(entity, 1.0, "exact", "same name")
                continue
            if norm == wanted:
                keep(entity, 0.95, "exact", "same name ignoring case and separators")
                continue
            words = set(norm.split("_"))
            if wanted and (wanted in words or norm.startswith(wanted)):
                keep(entity, 0.8, "substring", "name starts with or contains the word")
            elif wanted and (wanted in norm or norm in wanted):
                keep(entity, 0.6, "substring", "one name contains the other")
            shared = synonyms & words
            if shared:
                keep(
                    entity,
                    0.75,
                    "synonym",
                    "'{}' is a synonym of '{}'".format(sorted(shared)[0], text.strip()),
                )
            ratio = difflib.SequenceMatcher(None, wanted, norm).ratio()
            if ratio >= self.fuzzy_threshold:
                keep(
                    entity,
                    0.7 * ratio,
                    "fuzzy",
                    "spelling similarity {:.0%}".format(ratio),
                )
        ranked = sorted(results.values(), key=lambda r: -r["confidence"])
        return ranked[: self.max_results]

    def best(self, text, element=None, threshold=0.7):
        """Return the best match above a confidence threshold, else None."""
        matches = self.match(text, element)
        if matches and matches[0]["confidence"] >= threshold:
            return matches[0]
        return None
