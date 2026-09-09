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
at the boundary by `@tool_errors` into `ToolError` — so MCP's `isError` flag
is set and the model is told plainly that the call did not succeed. Errors
carry a stable `code` and, where one exists, a `hint` naming the next action.

**3. Small surface by default.** 14 consolidated tools ship enabled: seven
reads, `semantic_search`, and six writes that only register when a writable
web API key is configured. (`ZOTERO_MCP_TOOLSETS` and `ZOTERO_MCP_COMPAT` are
parsed by the config layer for a future opt-in toolset mechanism and pre-1.0
name compatibility, neither of which is built yet.)

**4. Writes are reversible or refused.** Destructive operations default to
`dry_run=True` and return a diff. Updates send `If-Unmodified-Since-Version`,
so a concurrent edit in the Zotero desktop client produces a `write_conflict`
rather than a silent clobber.

**5. One seam for local vs. web.** A `LibraryBackend` protocol is selected once
at startup. Tools never ask "am I in local mode?" — every "works on the web API
but not locally" bug in the predecessors traces to that question being asked in
thirty places.

## Layout

| Module | Responsibility |
|---|---|
| `errors.py` | Typed failures; `@tool_errors` boundary translation |
| `config.py` | One frozen `ZoteroConfig`, resolved once from file + env + args |
| `identifiers.py` | DOI / arXiv / ISBN / PMID / PMCID / Zotero-key parsing (stdlib only) |
| `schema.py` | Zotero's own `/schema`: item types, base-field resolution |
| `models.py` | Pydantic result models — the structured half of every tool result |
| `paging.py` | Opaque cursors, response clamping |
| `render.py` | Markdown rendering — the human half of every tool result |
| `backends/` | `LibraryBackend` protocol + web / local-HTTP / SQLite / hybrid |
| `content/` | PDF, EPUB and HTML extraction; ranged reads; fulltext cache |
| `external/` | Crossref, Unpaywall, arXiv, Semantic Scholar, PMC, Scite, Better BibTeX |
| `index/` | Optional semantic index (built by CLI, read-only in the server) |
| `tools/` | The consolidated tool surface |
| `compat/` | Pre-1.0 tool names as thin aliases |

## Backends

`LibraryBackend` is the only thing that talks to Zotero.

- **`WebBackend`** — the Zotero Web API via pyzotero. Full read/write.
- **`LocalHttpBackend`** — the desktop client's read-only API on `:23119`.
- **`SqliteBackend`** — direct `zotero.sqlite` reads (`immutable=1`, so a
  running Zotero's write lock does not block us). Fastest reads, no network,
  read-only by construction.
- **`HybridBackend`** — reads from the fastest available local source, writes
  through `WebBackend`. This is what `ZOTERO_LOCAL=true` plus an API key
  resolves to, because it is what that combination has always meant in
  practice.

Selection happens once, in `backends/factory.py`, from `LibraryMode`.

## Response shape

Every tool returns both halves:

- **structured** — a Pydantic model, so the caller gets item keys, versions and
  DOIs as data instead of re-parsing prose to find the key it needs for the
  next call;
- **markdown** — a rendering for the human in the loop and for clients that
  ignore structured content.

`SurfaceSettings.structured_output` disables the former for clients that
mishandle it.

## Build order

Shipped in 1.0:

1. Foundation: errors, config, identifiers
2. Zotero schema, result models, paging, markdown rendering
3. Backend: web, local HTTP, hybrid (local reads, web writes), factory
4. Content: PDF/EPUB extraction, chunking, reference stripping, fulltext cache
5. Tool surface: search, items, collections, tags, stats, health; write tools
   with dry-run previews (items, tags, collections, notes)
6. Semantic index: chunk-level ChromaDB store, incremental by item version,
   cross-encoder rerank, filters, scheduled refresh
7. CLI (`serve`, `health`, `index`), docs, CI

Not built (and not currently planned unless asked for):

- External services: Crossref, Unpaywall, arXiv, S2, PMC, Scite, Better BibTeX
- Ranged page reads and annotation tools
- Resources, prompts, toolsets, and the pre-1.0 compatibility name set
