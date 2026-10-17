## DESCRIPTION

*i.ai* is an AI assistant that works inside the running GRASS session. It
answers questions about the data, the session, methods and tools, and
turns tasks into plans of GRASS commands that are validated before
anything runs.

Every GRASS tool installed in the session is available to the assistant.
This covers core tools and addons, so all of them can be found, read and
proposed. The tool catalog is read from the live installation
(`$GISBASE/bin`, `$GISBASE/scripts`, `$GRASS_ADDON_BASE/bin`,
`$GRASS_ADDON_BASE/scripts` and `GRASS_ADDON_PATH`). Each tool's
interface comes from its own `--interface-description`, so the assistant
uses the parameters, flags and option values of the installed version.

The language model can be:

- **ollama**: a local [Ollama](https://ollama.com) server;
- **openai**: any OpenAI-compatible server (OpenAI, llama.cpp server,
  vLLM, LM Studio...), with the key in `IAI_API_KEY` or `OPENAI_API_KEY`;
- **claude**: [Claude Code](https://claude.com/claude-code) on the
  user's logged-in account (the `claude` command), with no API key;
- **opencode**: [OpenCode](https://opencode.ai), by default with the free
  `opencode/big-pickle` model, which needs no account.

### How it works

The design follows
[GeoCopilot](https://github.com/yosgi/GeoCopilot), an agent framework for
3D scene control, adapted to GRASS:

1. An *intent agent* classifies each request as a task (execute), a
   question (query), unclear (clarify), help or chat.
2. Unclear requests go to a *clarification agent*. It asks what is
   missing and suggests complete requests that use real map names.
3. Questions go to a *query agent*. Tasks go to an *execution agent*.
   Both work in steps. In each step the model either calls one read-only
   tool, answers, asks for clarification, or (execution agent only)
   proposes a plan.
4. Each plan goes through a *validation layer* (see below). Any errors go
   back to the agent, which fixes the plan, until the plan is valid or the
   step budget (**max\_steps**) is spent.

The read-only tools available to the model are:

- search all installed tools by keywords;
- list every tool with a given prefix;
- read the full interface of a tool;
- search the installed manual pages;
- list maps and match the user's words to existing maps;
- read map metadata and the computational region;
- validate a command without running it;
- run read-only commands (statistics, queries, metadata such as
  *r.univar* or *v.info*).

Read-only commands can run on a raster map's own region through a scoped
region manager, so the user's computational region never changes. The
session context sent to the model holds the project, mapset, CRS,
computational region, mask status, the maps in the search path and the
installed tools.

### Validation and safety

Nothing the model proposes runs unchecked. Each command is checked
against the live interface of the installed tool and against the existing
maps. The following are errors, reported with suggestions when a close
match exists:

- unknown tools, parameters, flags and option values, and values of the
  wrong type;
- missing required parameters;
- input maps that do not exist, including maps used in *r.mapcalc*
  expressions (an output of an earlier plan step counts as existing);
- output maps that already exist (without **--overwrite**) and illegal
  map names.

Nothing is corrected silently. Each valid command also gets a risk level:

- **read**: only reads data or prints state;
- **create**: only writes new maps or files;
- **modify**: changes existing data or session state. This includes the
  region, the mask, database tables, removals, renames and colour tables.
  Any tool not known to be read-only and with no declared output also
  counts as **modify**.

Plans run only with the **-e** flag. Each step is validated again just
before it runs. A **modify** step needs a *yes* at the terminal, or the
**-y** flag; without either, the plan stops before that step. Only GRASS
tools run, and never through a shell. Interactive tools (such as
*g.gui* or *d.mon*) are refused. **--overwrite** is never added by the
model; it applies only when the user passes it to *i.ai*.

The command-line backends cannot touch the session themselves.
*claude* runs with all its built-in tools disabled and a JSON schema for
the reply. *opencode* runs its read-only *plan* agent in an empty
temporary directory, without auto-approved permissions.

### Sessions and output

With **session**, the dialog history and the last plan are saved under
`$XDG_DATA_HOME/i.ai/sessions/` (by default `~/.local/share/i.ai/`), so a
conversation can continue across calls. The **format** option prints the
answer and plan as text (**plain**), the full result as JSON (**json**),
or the plan as a shell script to review and run later (**shell**).

The **-i** flag starts an interactive dialog. Its commands are `/run`
(execute the last plan), `/plan`, `/context`, `/reset` and `/exit`.

### Operations

- **ask** (default): answer the **query**, or plan commands for it.
- **info**: print the session context that the assistant sees. No model
  is needed.
- **index**: read the interface of every installed tool into the cache
  and index the manual pages. With **embed\_model** (ollama and openai
  backends), it also embeds the manual sections, and manual searches then
  use cosine similarity instead of BM25 keyword ranking. Caches are kept
  in `$XDG_CACHE_HOME/i.ai/` (by default `~/.cache/i.ai/`).

## NOTES

The answer goes to standard output; messages and progress go to standard
error. Plan execution reports its progress through `G_percent()`.

Small local models often struggle to follow the JSON step protocol.
Models that are good at tool use give the best plans. When the
**ollama** backend has no **model**, the first installed model is used.

## EXAMPLES

Ask a question with Claude Code:

```sh
i.ai backend=claude query="Which Landsat bands are in this project, and what is the mean of elevation over its own extent?"
```

Plan a task with OpenCode Big Pickle, review the plan, then execute it:

```sh
i.ai backend=opencode query="Compute slope in degrees from the DEM, then smooth it with a 5x5 average"
i.ai backend=opencode query="Compute slope in degrees from the DEM, then smooth it with a 5x5 average" -e
```

Write the plan as a shell script:

```sh
i.ai backend=claude query="Compute NDVI from the 2002 Landsat 7 bands" format=shell > ndvi.sh
```

Continue a named dialog:

```sh
i.ai backend=claude session=wake query="Delineate the watersheds of the elevation map"
i.ai backend=claude session=wake query="Now keep only basins larger than 1 km2" -e
```

Use an OpenAI-compatible server, here a local llama.cpp server:

```sh
i.ai backend=openai url=http://localhost:8080/v1 model=qwen3 query="..."
```

Show what the assistant sees, and build the caches:

```sh
i.ai operation=info
i.ai operation=index
```

## REQUIREMENTS

- GRASS 8.4 or later (8.5 or later for *r.mask.status*).
- One backend: Ollama, an OpenAI-compatible server, the `claude` command
  (logged in), or the `opencode` command.
- NumPy, only for the embedding index.

## SEE ALSO

*[g.search.modules](https://grass.osgeo.org/grass-stable/manuals/g.search.modules.html),
[r.mapcalc](https://grass.osgeo.org/grass-stable/manuals/r.mapcalc.html),
[r.univar](https://grass.osgeo.org/grass-stable/manuals/r.univar.html)*

[GeoCopilot](https://github.com/yosgi/GeoCopilot), whose agent architecture
*i.ai* follows.

## AUTHORS

Yann Chemin
