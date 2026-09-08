# zotero-mcp-next

A [Model Context Protocol](https://modelcontextprotocol.io) server for
[Zotero](https://www.zotero.org): search a library, read what is in it,
and write back to it, from any MCP client.

It is a ground-up rewrite of an existing Zotero MCP server. Everything that
server could do, this one can do, and every tool name it shipped still works
(see [the migration guide](docs/migration.md)). What changed is underneath.

```
zotero_search(query="attention mechanisms", mode="auto")
zotero_read(item_key="ABCD2345", what="document", pages="3-8")
zotero_add_item(identifier="10.48550/arxiv.1706.03762", dry_run=False)
```

## Why this one

**Nothing returns unbounded output.** Every list paginates behind an opaque
cursor, every document is read by page range, and every response is clamped
against a configured character budget. When something is truncated the result
says so, and says how to get the rest. The predecessor's `get_item_fulltext`
returned an entire PDF and then apologised for it, after the tokens were spent.

**A failure is a failure.** Tools raise typed errors that reach the client with
MCP's `isError` flag set, a stable machine-readable `code`, and a hint naming
the next thing to try. They do not return a successful result whose text begins
with `"Error:"`, which teaches a model that the call worked.

**Writes preview first.** Anything destructive or creative defaults to
`dry_run=True` and shows exactly what it would do. Updates carry the item's
version, so an edit made in the Zotero desktop client while the model was
thinking produces a `write_conflict` rather than a silent overwrite.

**The tool surface is small.** 18 tools by default rather than 57. Every tool
sent to a client costs context on every request, and near-duplicate tools
compete for a model's attention. Optional groups are opt-in by name, and the
old names are one environment variable away.

**Local and web are one seam.** The backend is chosen once at startup. No tool
asks "am I in local mode?", which is where the predecessors' "works on the web
API but not locally" bugs came from.

## Install

```bash
pip install "zotero-mcp-next[all]"
```

Extras, if you would rather not take all of it:

| Extra | Adds |
|---|---|
| `pdf` | PDF, EPUB and HTML text extraction (PyMuPDF, ebooklib, markitdown) |
| `semantic` | The optional semantic index (ChromaDB, sentence-transformers) |
| `scite` | [Scite.ai](https://scite.ai) citation tallies and retraction checks |
| `all` | All of the above |

Python 3.10 or newer.

## Quick start

Point it at your library, then register it with a client:

```bash
export ZOTERO_LIBRARY_ID=123456        # from zotero.org/settings/keys
export ZOTERO_API_KEY=xxxxxxxxxxxx

zotero-mcp doctor                      # is Zotero reachable?
zotero-mcp setup --client claude-desktop
```

`setup` writes the server into the client's configuration file;
`--client print` shows the JSON instead of writing anything, for a client it
does not know about.

If the Zotero desktop application is running and you have enabled *Settings ->
Advanced -> Allow other applications on this computer to communicate with
Zotero*, no API key is needed to read. With both a running Zotero and an API
key the server reads locally and writes through the web API, which is faster
than either alone.

## The tools

| Tool | What it does |
|---|---|
| `zotero_search` | One search with a mode: `auto`, `metadata`, `fulltext`, `semantic`, `tag`, `citation_key`. `auto` widens the query itself when a narrow one finds nothing, and says what it tried. |
| `zotero_get_item` | One item, its children, or several at once, in brief or full detail. |
| `zotero_read` | Read a document, its stored full text, its outline or its abstract, by page range. |
| `zotero_get_annotations` | Highlights, notes and comments on an item's attachments. |
| `zotero_add_item` | Add by DOI, arXiv id, ISBN, PMID, PMCID or URL, or from typed fields. Fetches metadata, and can attach the open-access PDF. |
| `zotero_update_item` | Change fields, creators, tags or collections on one item. |
| `zotero_manage_items` | Trash, restore, delete, empty the trash, or copy items into another library. |
| `zotero_manage_note` | Create, update, append to or delete a note. |
| `zotero_manage_annotation` | Create, update or delete an annotation on an attachment. |
| `zotero_collections` | List, create, rename, move, delete collections, and file items into them. |
| `zotero_tags` | List, rename, merge, add and remove tags. |
| `zotero_library` | Libraries, recent items, statistics, items by type, uncollected items, saved searches, RSS feeds and item versions. |
| `zotero_export` | Bibliographies and citations in any Zotero style, or BibTeX. |
| `zotero_health` | Is the backend reachable, what mode is it in, what is optional and missing. |
| `zotero_duplicates` | Find and merge duplicates. |
| `zotero_index` | Build, update or inspect the semantic index. |
| `zotero_find_related` | Items near a given item in the semantic index. |
| `zotero_coverage` | What a library does and does not cover on a topic. |

There are also MCP **resources** for clients that read them
(`zotero://collections`, `zotero://collections/{key}/items`,
`zotero://items/{key}`, `zotero://tags`), and **prompts** for four common
workflows: `literature_review`, `summarise_paper`, `tidy_library` and
`find_support`.

### Optional groups

`ZOTERO_MCP_TOOLSETS` decides which optional groups are registered. Unset means
core plus `duplicates`, `search-admin` and `discovery`.

| Group | Tools |
|---|---|
| `duplicates` | `zotero_duplicates` |
| `discovery` | `zotero_find_related`, `zotero_coverage` |
| `search-admin` | `zotero_index` |
| `scite` | `scite_enrich_item`, `scite_enrich_search`, `scite_check_retractions` |
| `chatgpt-connector` | `search`, `fetch`, the deep-research connector contract |
| `compat` | Every pre-1.0 tool name |

Values: `all`, `none`, a comma-separated list, or `all,-scite` to take
everything except one group. `zotero-mcp tools` prints what a given
configuration actually exposes.

### Old tool names

Set `ZOTERO_MCP_COMPAT=1` (or `--compat`, or add `compat` to the toolsets) and
all 52 tool names from the previous server are registered as thin aliases onto
their replacements. Nothing that calls this server by an old name has to
change. See [docs/migration.md](docs/migration.md) for the full mapping and for
the handful of behaviours that deliberately differ.

## Configuration

Configuration comes from command line arguments, then environment variables,
then a JSON config file, then defaults. The file lives at
`~/.config/zotero-mcp/config.json` (or wherever `ZOTERO_MCP_CONFIG` points).

### Library

| Variable | Meaning |
|---|---|
| `ZOTERO_LIBRARY_ID` | Library id from <https://www.zotero.org/settings/keys> |
| `ZOTERO_LIBRARY_TYPE` | `user` (default) or `group` |
| `ZOTERO_API_KEY` | Web API key |
| `ZOTERO_MCP_MODE` | `auto` (default), `web`, `local` or `hybrid` |
| `ZOTERO_LOCAL` | The legacy switch: `true` means local, or hybrid when an API key is also set |
| `ZOTERO_LOCAL_API_BASE` | Override the desktop client's API address |
| `ZOTERO_DATA_DIR` | Zotero's data directory, if it is not the default |
| `ZOTERO_SQLITE_PATH` | Path to `zotero.sqlite`, if it is not in the data directory |
| `ZOTERO_STORAGE_PATH` | Attachment storage directory |

### Limits

| Variable | Default | Meaning |
|---|---|---|
| `ZOTERO_MCP_MAX_RESPONSE_CHARS` | 24000 | Hard ceiling on any one response |
| `ZOTERO_MCP_MAX_CONTENT_CHARS` | 60000 | Ceiling for tools whose purpose is bulk text |
| `ZOTERO_MCP_PAGE_SIZE` | 20 | Default page size for listings |
| `ZOTERO_MCP_MAX_PDF_PAGES` | 40 | Most PDF pages read in one call |

### Surface and services

| Variable | Meaning |
|---|---|
| `ZOTERO_MCP_TOOLSETS` | Optional tool groups (above) |
| `ZOTERO_MCP_COMPAT` | `1` to register the pre-1.0 tool names |
| `ZOTERO_MCP_STRUCTURED_OUTPUT` | `0` for clients that mishandle structured content |
| `ZOTERO_MCP_ALLOW_EXTERNAL` | `0` to forbid every outbound call to Crossref, arXiv, Unpaywall and friends |
| `ZOTERO_MCP_CONTACT_EMAIL` | Sent to Crossref and Unpaywall, which ask for one and give faster service in return |
| `ZOTERO_MCP_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, ... |

### Semantic index

| Variable | Meaning |
|---|---|
| `ZOTERO_MCP_SEMANTIC` | `0` to disable semantic search entirely |
| `ZOTERO_MCP_EMBEDDING_PROVIDER` | `default` (local), `openai` or `gemini` |
| `ZOTERO_MCP_EMBEDDING_MODEL` | Model name for that provider |
| `ZOTERO_MCP_DB_PATH` | Where the index lives |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` | Credentials for the hosted providers |

Building the index is a command line operation, because it is long-running and
a tool call is the wrong place for it:

```bash
zotero-mcp index build          # first time, embeds the whole library
zotero-mcp index update         # only what changed
zotero-mcp index status
```

Changing the embedding model resets the index rather than mixing vector spaces:
the model signature is stored with it and checked on every open.

## Command line

```
zotero-mcp serve      # run the server (the default)
zotero-mcp doctor     # check Zotero is reachable and report the mode
zotero-mcp setup      # write client configuration
zotero-mcp index      # build / update / status / clear
zotero-mcp tools      # list what this configuration exposes
```

`serve --transport http --port 8000` exposes it over HTTP instead of stdio.

## What talks to the outside world

Adding an item by identifier asks Crossref, arXiv, OpenLibrary, PubMed or
Semantic Scholar for metadata; attaching an open-access PDF asks Unpaywall.
Set `ZOTERO_MCP_ALLOW_EXTERNAL=0` and none of it happens: the tools that need
it say plainly that external lookups are disabled rather than reporting that
nothing was found.

Nothing else leaves the machine. There is no telemetry.

## Development

```bash
pip install -e ".[dev]"
pre-commit install
pytest
```

The test suite runs against an in-memory library that behaves like the Zotero
API, including its awkward parts, so the tools are exercised end to end without
a network or a Zotero installation.

## License

MIT. See [LICENSE](LICENSE).
