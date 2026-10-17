"""
MODULE:    i.ai
AUTHOR(S): Yann Chemin <dr.yann.chemin@gmail.com>
PURPOSE:   Retrieval over the installed GRASS manual pages.
SPDX-License-Identifier: Unlicense

The manual pages of every installed tool (core Markdown sources, addon
Markdown or HTML pages) are split into sections. A BM25 keyword index is
built on first use and cached; it needs no model. When an embedding model
is available, 'i.ai operation=index' also embeds every section, and
searches then rank by cosine similarity instead.
"""

import html
import json
import math
import os
import re

import grass.script as gs

from iai_catalog import cache_dir

WORD_RE = re.compile(r"[a-z0-9][a-z0-9._]*[a-z0-9]|[a-z0-9]")
MAX_CHUNK = 1800
# Bump when the chunking changes, so cached indexes are rebuilt.
INDEX_VERSION = 2
STOP = set(
    "a an and are as at be by can for from has have in is it its of on or that the "
    "this to was were will with which when where you your if not use used using "
    "may also more than then these those into each such".split()
)


def _tokens(text):
    return [t for t in WORD_RE.findall(text.lower()) if t not in STOP]


def _html_to_text(text):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<h[1-6][^>]*>", "\n## ", text)
    text = re.sub(r"(?i)<(br|p|li|div|tr|pre)[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def manual_sources():
    """Return (tool name, path) for every manual page found."""
    gisbase = os.environ.get("GISBASE", "")
    addon_base = os.environ.get("GRASS_ADDON_BASE", "")
    found = {}
    for directory in (
        os.path.join(gisbase, "docs", "html"),
        os.path.join(addon_base, "docs", "html") if addon_base else "",
        os.path.join(gisbase, "docs", "mkdocs", "source"),
        os.path.join(addon_base, "docs", "mkdocs", "source") if addon_base else "",
    ):
        if not directory or not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            stem, ext = os.path.splitext(entry)
            if ext in (".md", ".html") and re.match(r"^[a-z0-9]+\.", stem):
                found[stem] = os.path.join(directory, entry)  # Markdown wins.
    return sorted(found.items())


def split_page(name, path):
    """Split one manual page into sections of at most MAX_CHUNK characters."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return []
    if path.endswith(".html"):
        text = _html_to_text(text)
    else:
        text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
    chunks = []
    for section in re.split(r"\n(?=#{2,3} )", "\n" + text):
        section = section.strip()
        if not section:
            continue
        title = section.splitlines()[0].lstrip("# ").strip()[:80]
        for start in range(0, len(section), MAX_CHUNK):
            body = re.sub(r"\n{3,}", "\n\n", section[start : start + MAX_CHUNK])
            chunks.append({"tool": name, "section": title, "text": body})
    return chunks


class ManualIndex:
    """BM25 index of manual sections, with an optional embedding index."""

    def __init__(self, backend=None):
        self.backend = backend
        self.directory = cache_dir()
        self.chunks = []
        self._df = {}
        self._tf = []
        self._lengths = []
        self._vectors = None

    def _signature(self):
        sources = manual_sources()
        newest = max((int(os.path.getmtime(p)) for _, p in sources), default=0)
        return "v{}-{}-{}".format(INDEX_VERSION, len(sources), newest)

    def _lexical_path(self):
        return os.path.join(self.directory, "manual-bm25.json")

    def _dense_paths(self):
        model = getattr(self.backend, "embed_model", None) or "none"
        tag = "{}-{}".format(getattr(self.backend, "name", "none"), model)
        tag = re.sub(r"[^A-Za-z0-9.]+", "_", tag)
        return (
            os.path.join(self.directory, "manual-vectors-{}.npy".format(tag)),
            os.path.join(self.directory, "manual-vectors-{}.json".format(tag)),
        )

    def load(self):
        """Load the cached BM25 index, rebuilding it when the manuals changed."""
        signature = self._signature()
        try:
            with open(self._lexical_path(), encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("signature") == signature:
                self._set_lexical(data)
                self._load_dense(signature)
                return self
        except (OSError, ValueError):
            pass
        self.build_lexical(signature)
        self._load_dense(signature)
        return self

    def _set_lexical(self, data):
        self.chunks = data["chunks"]
        self._tf = data["tf"]
        self._df = data["df"]
        self._lengths = [sum(tf.values()) for tf in self._tf]

    def build_lexical(self, signature=None, progress=False):
        signature = signature or self._signature()
        sources = manual_sources()
        chunks, tf_list, df = [], [], {}
        for i, (name, path) in enumerate(sources):
            if progress:
                gs.percent(i, len(sources), 5)
            for chunk in split_page(name, path):
                counts = {}
                # The tool name counts several times so it ranks its own page first.
                for token in _tokens(chunk["text"]) + [name] * 3:
                    counts[token] = counts.get(token, 0) + 1
                for token in counts:
                    df[token] = df.get(token, 0) + 1
                chunks.append(chunk)
                tf_list.append(counts)
        if progress:
            gs.percent(1, 1, 1)
        data = {"signature": signature, "chunks": chunks, "tf": tf_list, "df": df}
        tmp = self._lexical_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, self._lexical_path())
        except OSError as error:
            gs.verbose(_("Cannot cache the manual index: {}").format(error))
        self._set_lexical(data)

    def _load_dense(self, signature):
        vectors_path, meta_path = self._dense_paths()
        if not (os.path.exists(vectors_path) and os.path.exists(meta_path)):
            return
        try:
            import numpy as np

            with open(meta_path, encoding="utf-8") as handle:
                meta = json.load(handle)
            if meta.get("signature") != signature or meta.get("count") != len(
                self.chunks
            ):
                return
            self._vectors = np.load(vectors_path)
        except (OSError, ValueError, ImportError):
            self._vectors = None

    def build_dense(self, batch=32):
        """Embed every manual section with the backend's embedding model."""
        import numpy as np

        if not self.chunks:
            self.load()
        vectors = []
        total = len(self.chunks)
        for start in range(0, total, batch):
            gs.percent(start, total, 1)
            texts = [
                "{} - {}\n{}".format(c["tool"], c["section"], c["text"])
                for c in self.chunks[start : start + batch]
            ]
            vectors.extend(self.backend.embed(texts))
        gs.percent(1, 1, 1)
        matrix = np.asarray(vectors, dtype=np.float32)
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12
        vectors_path, meta_path = self._dense_paths()
        np.save(vectors_path, matrix)
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump({"signature": self._signature(), "count": total}, handle)
        self._vectors = matrix

    @property
    def dense(self):
        return self._vectors is not None

    def search(self, query, k=5, tool=None):
        """Return the k most relevant manual sections for a query."""
        if not self.chunks:
            self.load()
        candidates = [
            i for i, c in enumerate(self.chunks) if tool is None or c["tool"] == tool
        ]
        if not candidates:
            return []
        if self.dense:
            try:
                return self._search_dense(query, k, candidates)
            except Exception as error:  # noqa: BLE001 - fall back to keywords
                gs.verbose(
                    _("Embedding search failed, using keywords: {}").format(error)
                )
        return self._search_bm25(query, k, candidates)

    def _search_bm25(self, query, k, candidates, k1=1.4, b=0.75):
        terms = _tokens(query)
        n = len(self.chunks)
        average = sum(self._lengths) / max(n, 1)
        scores = []
        for i in candidates:
            tf = self._tf[i]
            score = 0.0
            for term in terms:
                f = tf.get(term)
                if not f:
                    continue
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                score += (
                    idf
                    * f
                    * (k1 + 1)
                    / (f + k1 * (1 - b + b * self._lengths[i] / average))
                )
            if score > 0:
                scores.append((score, i))
        scores.sort(reverse=True)
        return [dict(self.chunks[i], score=round(s, 2)) for s, i in scores[:k]]

    def _search_dense(self, query, k, candidates):
        import numpy as np

        vector = np.asarray(self.backend.embed([query])[0], dtype=np.float32)
        vector /= np.linalg.norm(vector) + 1e-12
        subset = self._vectors[candidates]
        sims = subset @ vector
        order = np.argsort(-sims)[:k]
        return [
            dict(self.chunks[candidates[j]], score=round(float(sims[j]), 3))
            for j in order
        ]
