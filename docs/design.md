# Design

## Why a rewrite

The server this replaces grew by accretion: 57 tools registered
unconditionally, configuration read ad-hoc from eight modules, every failure
returned as a successful result whose text began with `"Error:"`, and a
`get_item_fulltext` that emitted an entire paper and prepended a warning
saying so *after* the tokens were already spent.

Those are not bugs to patch individually. They are consequences of decisions
made at the bottom of the stack, so they are fixed at the bottom of the stack.

## Rules

**1. No unbounded response.** Every read tool clamps against
`LimitSettings.max_response_chars`. Lists paginate with an opaque cursor.
Documents are read by page range, never whole. Truncation is always reported
in the result, with the cursor or range needed to continue.

**2. A failure is a failure.** Tools raise typed `ZoteroMcpError`s, translated
at the boundary by `@tool_errors` into `ToolError`, so MCP's `isError` flag
is set and the model is told plainly that the call did not succeed. Errors
carry a stable `code` and, where one exists, a `hint` naming the next action.

**3. Small surface by default.** 18 consolidated tools ship enabled. Optional
capability groups are opt-in via `ZOTERO_MCP_TOOLSETS`. The full pre-1.0 name
set is available behind `ZOTERO_MCP_COMPAT=1` for installations that reference
the old names.

**4. Writes are reversible or refused.** Destructive operations default to
`dry_run=True` and return a diff. Updates send `If-Unmodified-Since-Version`,
so a concurrent edit in the Zotero desktop client produces a `write_conflict`
rather than a silent clobber.

**5. One seam for local vs. web.** A `LibraryBackend` protocol is selected once
at startup. Tools never ask "am I in local mode?". Every "works on the web API
but not locally" bug in the predecessors traces to that question being asked in
thirty places.

## Layout

| Module | Responsibility |
|---|---|
| `errors.py` | Typed failures; `@tool_errors` boundary translation |
| `config.py` | One frozen `ZoteroConfig`, resolved once from file + env + args |
| `identifiers.py` | DOI / arXiv / ISBN / PMID / PMCID / Zotero-key parsing (stdlib only) |
| `schema.py` | Zotero's own `/schema`: item types, base-field resolution |
| `models.py` | Pydantic result models: the structured half of every tool result |
| `paging.py` | Opaque cursors, response clamping |
| `render.py` | Markdown rendering: the human half of every tool result |
| `backends/` | `LibraryBackend` protocol + web / local-HTTP / hybrid, and a read-only `zotero.sqlite` reader |
| `content/` | PDF, EPUB and HTML extraction; ranged reads; fulltext cache |
| `external/` | Crossref, Unpaywall, arXiv, Semantic Scholar, PMC, Scite, Better BibTeX |
| `index/` | Optional semantic index (built by CLI, read-only in the server) |
| `tools/` | The consolidated tool surface |
| `compat/` | Pre-1.0 tool names as thin aliases |

## Backends

`LibraryBackend` is the only thing that talks to Zotero.

- **`WebBackend`**: the Zotero Web API via pyzotero. Full read and write.
- **`LocalHttpBackend`**: the desktop client's read-only API on `:23119`.
- **`HybridBackend`**: reads through `LocalHttpBackend`, writes through
  `WebBackend`, and falls back to the web for any read the local API cannot
  serve. This is what a running Zotero plus an API key resolves to, because it
  is what that combination has always meant in practice.

Selection happens once, in `backends/factory.py`, from `LibraryMode`. `AUTO`
probes: hybrid if both halves are available, then web, then local. The probe is
non-fatal, because a server that refuses to start while Zotero happens to be
closed is worse than one that starts, says so through `zotero_health`, and
works the moment Zotero opens.

There is deliberately no fourth backend reading `zotero.sqlite`. The database
is an internal schema with no compatibility promise, and a backend built on it
has to reimplement every field, type and relation mapping the API already
does. What it uniquely offers is the handful of things that exist nowhere in
either API, RSS feeds most obviously, so `backends/localdb.py` is a narrow
read-only reader for exactly those, not a backend:

- opened read-only through a URI, so nothing this process does can corrupt a
  library;
- read from a snapshot copy when Zotero holds the write lock, rather than
  blocking a tool call on another application;
- field ids resolved by name. The predecessor hardcoded `fieldID = 1` for the
  title, which is true of most installs and quietly wrong on the rest.

## Response shape

Every tool returns both halves:

- **structured**: a Pydantic model, so the caller gets item keys, versions and
  DOIs as data instead of re-parsing prose to find the key it needs for the
  next call;
- **markdown**: a rendering for the human in the loop and for clients that
  ignore structured content.

`SurfaceSettings.structured_output` disables the former for clients that
mishandle it.

## Build order

1. ~~Foundation: errors, config, identifiers~~
2. ~~Zotero schema, result models, paging, markdown rendering~~
3. ~~Backends: protocol, web, local HTTP, hybrid, factory~~
4. ~~Content: PDF/EPUB extraction, ranged page reads, fulltext cache~~
5. ~~Tool surface: search, items, content, annotations, notes, write,
   organize, library, admin~~
6. ~~External services: Crossref, Unpaywall, arXiv, S2, PMC, Scite,
   Better BibTeX~~
7. ~~Semantic index and discovery tools~~
8. ~~Resources, prompts, toolsets~~
9. ~~Compatibility layer: all pre-1.0 tool names~~
10. ~~CLI, setup helper, docs, CI~~

## Testing

The suite runs the real tools against an in-memory library that implements
`LibraryBackend` and behaves like the Zotero API, awkward parts included: its
`/items` equivalent returns child notes and attachments alongside their
parents, notes match on their body text, and item keys obey Zotero's base32
alphabet. That fidelity is the point. Every product bug the suite has found so
far, a health tool that could not report a startup failure, a case indexed
without its title, fetched metadata losing its authors, disabled external
lookups reported as "nothing found", was found because the fake refused to be
more convenient than the real thing.
