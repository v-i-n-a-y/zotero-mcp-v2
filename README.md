# zotero-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server for
[Zotero](https://www.zotero.org). It lets Claude Code, Claude Desktop and any
other MCP client search your library, read items, take notes, file things into
collections and find papers by meaning rather than by exact words.

This is a ground-up rewrite of the earlier `zotero-mcp` server with a small,
typed tool surface, bounded responses, real error reporting and a semantic
search index that is chunk-level, incremental and reranked.

## What it does

**Read**

| Tool | Purpose |
|---|---|
| `search_library` | Keyword search across the library, paged, with type filter |
| `get_item` | One item's full record, children, tags and collections; flags items in the Trash |
| `list_collections` / `collection_items` | Browse the collection tree and its contents |
| `list_tags` / `library_stats` | Tags and a library overview |
| `server_health` | Is Zotero reachable, is the index built, which optional features are on |

**Find by meaning**

| Tool | Purpose |
|---|---|
| `semantic_search` | Vector search over titles, abstracts and PDF fulltext, reranked by a cross-encoder. Each hit shows the passage that matched, whether it came from the metadata or the fulltext, and how many passages agreed. Filter by item type, year range or collection. |

**Write** (only when a web API key with write permission is configured)

| Tool | Purpose |
|---|---|
| `create_item` / `update_item` / `delete_item` | Create, edit and trash items. Delete is always "move to Trash", never permanent. |
| `modify_tags` / `modify_collections` | Add or remove tags and collection memberships |
| `create_note` | Attach a note to an item, or create a standalone one |

Every write tool defaults to `dry_run=true` and returns a preview of the change.
The client must pass `dry_run=false` to apply it. Updates carry Zotero's version
header, so a concurrent edit in the desktop app produces a clear conflict error
rather than a silent overwrite.

## Install

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install 'zotero-mcp-next[semantic,pdf] @ git+https://github.com/v-i-n-a-y/zotero-mcp-v2'
```

The `semantic` extra pulls in ChromaDB and sentence-transformers for semantic
search; `pdf` adds PyMuPDF and ebooklib for fulltext extraction. Omit both for a
lean install with keyword search only.

## Configure

The server reads environment variables (or a JSON file via `ZOTERO_MCP_CONFIG`).

| Variable | Meaning |
|---|---|
| `ZOTERO_MCP_MODE` | `local`, `web`, `hybrid` or `auto` (default). See below. |
| `ZOTERO_API_KEY` | Web API key from https://www.zotero.org/settings/keys |
| `ZOTERO_LIBRARY_ID` | Your numeric user ID (shown on the same page) |
| `ZOTERO_LIBRARY_TYPE` | `user` (default) or `group` |
| `ZOTERO_MCP_INDEX_SCHEDULE` | `daily` (default), `weekly`, `startup` or `manual` |
| `ZOTERO_MCP_RERANK` | `false` to skip cross-encoder reranking |
| `ZOTERO_MCP_EMBEDDING_MODEL` | Sentence-transformers model; default `BAAI/bge-small-en-v1.5` |
| `ZOTERO_MCP_DB_PATH` | Where the index lives; default `~/.config/zotero-mcp/chroma` |

**Modes.** `local` talks to the running Zotero desktop app (port 23119): fast,
no key needed, read-only. `web` talks to api.zotero.org and needs a key.
`hybrid` reads locally and writes through the web API, which is the best
everyday setting when the desktop app is open. `auto` picks local if the app is
reachable, otherwise web.

### Claude Code

```sh
claude mcp add zotero -s user \
  -e ZOTERO_MCP_MODE=hybrid \
  -e ZOTERO_API_KEY=... \
  -e ZOTERO_LIBRARY_ID=... \
  -- zotero-mcp
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "zotero": {
      "command": "zotero-mcp",
      "env": {
        "ZOTERO_MCP_MODE": "hybrid",
        "ZOTERO_API_KEY": "...",
        "ZOTERO_LIBRARY_ID": "..."
      }
    }
  }
}
```

## Semantic search

Build the index once from a terminal; it needs the desktop app open (or web
mode) to read fulltext:

```sh
zotero-mcp index build     # first run: roughly an hour per thousand PDFs
zotero-mcp index status
```

After that the running server refreshes the index on the schedule above,
re-embedding only items whose Zotero version changed. Extracted fulltext is
cached beside the index, so switching embedding models rebuilds in minutes.

How a query is answered: the vector index returns a pool of candidate chunks
(metadata chunk plus fulltext chunks per item, with trailing reference lists
stripped), a cross-encoder rescores the pool against the query, citation-dense
chunks are penalised, and chunks collapse to one hit per item with a small bonus
for items that matched in several places. Everything runs locally; on Apple
silicon the models use Metal.

## CLI

```
zotero-mcp                 # serve over stdio (what MCP clients run)
zotero-mcp health          # can the configured library be reached?
zotero-mcp index build     # build or incrementally refresh the semantic index
zotero-mcp index status
zotero-mcp index clear
zotero-mcp version
```

## Design

- Every response is bounded. Lists are paged with opaque cursors; long text is
  truncated with an explicit marker.
- Failures raise MCP tool errors with a stable code (`not_found`,
  `invalid_input`, `auth`, `write_conflict`, `backend_unavailable`,
  `unsupported`) and a hint, never a success whose text starts with "Error:".
- Tools return both markdown for humans and structured content for clients
  that can use it.
- Logging goes to stderr; stdout carries only the protocol.

More in [`docs/design.md`](docs/design.md).

## Development

```sh
uv sync --extra dev
uv run pytest
uv run ruff check src tests
```

## Licence

MIT.
