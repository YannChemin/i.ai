# i.ai

AI assistant for GRASS. It answers questions about the session and plans
GRASS commands that are validated against the installed tools and the
existing maps before anything runs. Every tool installed in the session,
core or addon, is available to it.

Backends: Ollama, any OpenAI-compatible server, Claude Code (logged-in
`claude` command, no API key) and OpenCode (free `opencode/big-pickle`
model, no account).

The agent design follows [GeoCopilot](https://github.com/yosgi/GeoCopilot):
an intent agent routes each request to a clarification, query or
execution agent; these work in JSON steps with read-only tools; plans go
through a validation layer and only run with `-e`.

## Install

```sh
make MODULE_TOPDIR=$HOME/dev/grass
```

or, from a GRASS session, `g.extension i.ai url=$HOME/dev/i.ai`.

## Use

```sh
i.ai backend=claude query="What is the mean of elevation over its own extent?"
i.ai backend=opencode query="Compute slope in degrees from the DEM" -e
i.ai backend=claude query="Compute NDVI from the Landsat bands" format=shell > ndvi.sh
i.ai backend=claude -i                 # interactive dialog
i.ai operation=info                    # what the assistant sees
i.ai operation=index                   # cache every tool interface, index manuals
```

See [i.ai.md](i.ai.md) for the full manual.

## Files

| File | Role |
|---|---|
| `i.ai.py` | GRASS parser interface, output, plan execution |
| `iai_llm.py` | Backends (ollama, openai, claude, opencode) |
| `iai_catalog.py` | Live catalog of installed tools and their interfaces |
| `iai_registry.py` | Maps in the search path, fuzzy and synonym matching |
| `iai_validate.py` | Command parsing, validation and risk levels |
| `iai_context.py` | Session context, dialog history, saved sessions |
| `iai_tools.py` | Read-only tools the model calls |
| `iai_rag.py` | Manual page search (BM25, optional embeddings) |
| `iai_agents.py` | Intent, clarification, query and execution agents |
| `tests/` | pytest suite (no language model needed) |

## Tests

```sh
grass --tmp-project XY --exec python3 -m pytest tests
```

## Licence

[Unlicense](LICENSE).
