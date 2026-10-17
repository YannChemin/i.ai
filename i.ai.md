## DESCRIPTION

*i.ai* is an AI assistant that works inside the running GRASS session. It
answers questions about the data, the session, methods and tools. It
turns tasks into plans of GRASS commands and command-line steps that are
validated before anything runs. It also writes complete report pipelines:
numbered scripts, stored per session, that go from the data to a LaTeX
report, run end to end, repair their own failing steps and compile the PDF.

Every GRASS tool installed in the session is available to the assistant.
This covers core tools and addons, so all of them can be found, read and
proposed. The tool catalog is read from the live installation
(`$GISBASE/bin`, `$GISBASE/scripts`, `$GRASS_ADDON_BASE/bin`,
`$GRASS_ADDON_BASE/scripts` and `GRASS_ADDON_PATH`). Each tool's
interface comes from its own `--interface-description`, so the assistant
uses the parameters, flags and option values of the installed version.

Every command-line tool on `PATH` is available as well: GDAL/OGR, LaTeX
(*latexmk*, *pdflatex*, *bibtex*, *biber*), Graphviz, ImageMagick, pandoc,
jq, curl and the rest. They are searched through the man page index
(*apropos*, *whatis*) and described from their man page or `--help`. The
prompt also lists the installed Python packages that scripts may import.

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
   question (query), an end-to-end report pipeline (pipeline), unclear
   (clarify), help or chat.
2. Unclear requests go to a *clarification agent*. It asks what is
   missing and suggests complete requests that use real map names.
3. Questions go to a *query agent*. Tasks go to an *execution agent*.
   Both work in steps. In each step the model either calls one read-only
   tool, answers, asks for clarification, or (execution agent only)
   proposes a plan.
4. Each plan goes through a *validation layer* (see below). Any errors go
   back to the agent, which fixes the plan, until the plan is valid or the
   step budget (**max\_steps**) is spent.
5. Pipeline requests go to a *pipeline agent* (see *Report pipelines*).

The read-only tools available to the model are:

- search all installed tools by keywords;
- list every tool with a given prefix;
- read the full interface of a tool;
- search the installed manual pages;
- list maps and match the user's words to existing maps;
- read map metadata and the computational region;
- validate a command without running it;
- run read-only commands (statistics, queries, metadata such as
  *r.univar* or *v.info*);
- search the command-line tools and read their manuals;
- run read-only inspection commands without a shell (*gdalinfo*,
  *ogrinfo*, *pdfinfo*, *ncdump*, *ls*, *head*, *find*, *grep*, or any
  command with `--version`/`--help`). Pipes and redirections are refused,
  and so are paths that may hold credentials (`~/.ssh`, `~/.aws`, tokens);
- validate a bash or Python script;
- in report pipelines, list the workspace and look up a DOI at Crossref.

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

Command-line steps and scripts are validated too. Bash goes through
`bash -n` and Python through its parser. Every command a script calls must
be installed, every Python module it imports must be importable, and the
GRASS calls inside it are checked like GRASS steps. This covers bash
lines, `grass_run bash -s <<EOF` blocks and `gs.run_command(...)` calls
with literal arguments. Some patterns are refused outright: *sudo*,
recursive removal of `/`, `~` or `.`, piping a download into a shell,
disk tools, *git push*, package installs and crontab edits. Scripts also
get a risk level: **network** for downloads and remote copies, **modify**
for removals and moves.

Plans run only with the **-e** flag. Each step is validated again just
before it runs. A **modify** or **network** step needs a *yes* at the
terminal, or the **-y** flag; without either, the plan stops before that
step. GRASS steps run directly; command-line steps run with *bash* in the
session's workspace (or the current directory). Interactive tools (such
as *g.gui* or *d.mon*) are refused. **--overwrite** is never added by the
model to GRASS steps; it applies only when the user passes it to *i.ai*.

The command-line backends cannot touch the session themselves.
*claude* runs with all its built-in tools disabled and a JSON schema for
the reply. *opencode* runs its read-only *plan* agent in an empty
temporary directory, without auto-approved permissions.

### Report pipelines

A pipeline request ("write a report on ...", or **operation=report**)
produces a report workspace. The workspace is either **workdir** or, for a
**session**, `~/.local/share/i.ai/sessions/<session>/`, and is laid out
like a scripted report:

- `scripts/00_env.sh`: paths, GRASS project and mapset, the study
  settings shared by every step, `grass_run` (GRASS tools directly inside
  a session, through `grass ... --exec` outside one) and `mm` (MetaMonitor
  progress, a no-op without it);
- `scripts/run_step.sh` and `scripts/NN_<name>.sh|py`: one script per
  step, from data preparation to figures (Python), diagrams (Graphviz),
  maps (GRASS display tools, cairo driver) and tables (LaTeX `tabular`
  and CSV);
- `<main>.tex` and `references.bib`: the report. The preamble and title
  block follow a fixed style; the body is written by the model;
- `Makefile` (`make analysis`, `make step-NN`, `make report`, `make`),
  `README.md` (the run book), `figures/`, `maps/`, `tables/`, `data/`,
  `logs/`, and the manifest `.i.ai/pipeline.json`.

The pipeline agent first inspects the data and tools and gives an
outline: steps, study settings and report sections. It then writes each
script, and each one must validate before the next is written. Last, it
writes the report body. The report includes every pipeline output with
`\figorpending` and `\taborpending`, so it compiles before the data
exist, with a placeholder box for each missing output. The checks on the
report are:

- every `\cite` has a BibTeX entry;
- every included file is declared as a step output;
- no shell escapes;
- every DOI exists at Crossref, with a matching title. Entries without a
  DOI are listed in the README under *References to verify*.

A script may overwrite only the GRASS maps that this pipeline created
(recorded in the manifest), never other existing maps.

With **-e**, or later with **operation=run -e**, *i.ai* runs the steps in
order and logs each one to `logs/`. It records the GRASS maps each step
created. When a step fails, a *repair agent* reads its log, inspects files
and tools, and rewrites the script, up to **repairs** times. The rewrite is
validated, and the first version is kept as `<script>.orig`. After the
last step, the model rewrites the Executive summary and Results from the
real outputs (tables, CSV files, logs and the list of figures). The report
is then compiled with *latexmk*, and LaTeX errors go back to the model
for repair. **from\_step** restarts a run at a given step.

A later request in the same session revises the pipeline: unchanged steps
are kept, and only new or changed steps are rewritten. The report title
block takes **author** and **contact** (by default from `git config`),
**organization** and an optional **classification** banner.

### Voice input

By default, requests are typed. With **input=voice**, they can also be
spoken into the PC microphone. In the dialog (**-i**), an empty line
(Enter) starts recording and Enter stops it. Without **-i** and without
**query**, one spoken request is taken. The transcription is put on the
`i.ai>` prompt line, where it can be edited before Enter sends it. Typed
lines and `/commands` still work.

Speech is recorded with PipeWire (*pw-record*, the Debian 13 default;
*arecord* otherwise) as 16 kHz mono WAV, and transcribed by whisper.cpp
with the large-v3 model on a GPU, never on a CPU. The recording is
deleted after transcription and nothing is stored on the GPU machine.
Transcription runs:

- by default on this machine, when the GPU search finds a GPU and a
  whisper.cpp build with a GPU backend;
- on a GPU server given by **voice\_host** (IP address or name) and
  **voice\_user** (login name; key-based ssh login is required). The
  defaults can be set once in the environment variables `IAI_VOICE_HOST`
  and `IAI_VOICE_USER`.

The GPU search looks for:

1. GPU hardware (*lspci*, `/dev/dri/renderD*`, `/dev/kfd`, *nvidia-smi*);
2. a *whisper-server* linked to a GPU backend of ggml (Vulkan, CUDA, HIP,
   OpenCL, SYCL, Metal, MUSA), in `PATH`, `~/dev/whisper.cpp`,
   `~/whisper.cpp` or the home directory, or at `IAI_WHISPER_BIN`;
3. a `ggml-large-v3` model, or the one at `IAI_WHISPER_MODEL`.

*whisper-server* then starts with the model kept in GPU memory. On a
server it is reached through an ssh tunnel on a random port bound to
127.0.0.1. It must report its GPU backend (`whisper_backend_init_gpu:
using ...`), or voice input stops with the reason. The server process is
stopped when *i.ai* exits. **voice\_language** sets the spoken language
(`auto` by default; `en`, `fr`, ...).

### Sessions and output

With **session**, the dialog history and the last plan are saved under
`$XDG_DATA_HOME/i.ai/sessions/` (by default `~/.local/share/i.ai/`), so a
conversation can continue across calls. The **format** option prints the
answer and plan as text (**plain**), the full result as JSON (**json**),
or the plan as a shell script to review and run later (**shell**).

The **-i** flag starts an interactive dialog. Its commands are `/run`
(execute the last plan), `/plan`, `/context`, `/reset` and `/exit`.

### Operations

- **ask** (default): answer the **query**, plan commands for it, or
  write a report pipeline, as the request needs.
- **report**: write a report pipeline for the **query**.
- **run**: run the stored pipeline (needs **-e**), repair failing steps,
  write the results into the report and compile it.
- **compile**: compile the stored report.
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

Pipeline scripts run with the user's own permissions. Validation refuses
known destructive patterns and asks before network or modifying steps,
but it is not a sandbox: read the scripts before running a pipeline on
data that matters, or run it in a separate mapset.

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

Write a report pipeline in a dated report folder, run it end to end, and
compile the PDF:

```sh
i.ai backend=claude operation=report workdir=~/Documents/LaTex/Report/20261017_Terrain \
    query="Terrain analysis report of the elevation map: slope, aspect, streams, maps, figures and tables" -e -y
```

Run it again later from step 04, after editing a script:

```sh
i.ai backend=claude workdir=~/Documents/LaTex/Report/20261017_Terrain operation=run from_step=04 -e
```

The same pipeline runs without *i.ai*:

```sh
cd ~/Documents/LaTex/Report/20261017_Terrain && make
```

Speak the requests of a dialog, transcribed on a GPU server:

```sh
i.ai backend=claude -i input=voice voice_host=10.42.0.89 voice_user=yann
```

Or set the server once and speak a single request:

```sh
export IAI_VOICE_HOST=10.42.0.89 IAI_VOICE_USER=yann
i.ai backend=claude input=voice
```

Mix GRASS and command-line steps in a plan:

```sh
i.ai backend=claude query="Export the slope map to a Cloud Optimized GeoTIFF and make a 1000 px PNG preview" -e
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
- For voice input: PipeWire (*pw-record*) or ALSA (*arecord*), *ssh* for a
  GPU server, and on the GPU machine a GPU-enabled whisper.cpp build
  (e.g. `cmake -B build -DGGML_VULKAN=ON`) with the `ggml-large-v3` model.
- For report pipelines: a LaTeX installation (*latexmk* or *pdflatex*
  with *bibtex*) and whatever the steps use (Python with matplotlib,
  Graphviz).

## SEE ALSO

*[g.search.modules](https://grass.osgeo.org/grass-stable/manuals/g.search.modules.html),
[r.mapcalc](https://grass.osgeo.org/grass-stable/manuals/r.mapcalc.html),
[r.univar](https://grass.osgeo.org/grass-stable/manuals/r.univar.html)*

[GeoCopilot](https://github.com/yosgi/GeoCopilot), whose agent architecture
*i.ai* follows.

## AUTHORS

Yann Chemin
