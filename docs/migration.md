# Migrating from the previous server

Short version: set `ZOTERO_MCP_COMPAT=1` and nothing you have written against
the old server has to change. Every one of its 52 tool names is registered,
each one translating onto the tool that replaced it.

```json
{
  "mcpServers": {
    "zotero": {
      "command": "zotero-mcp",
      "env": {
        "ZOTERO_LIBRARY_ID": "123456",
        "ZOTERO_API_KEY": "xxxxxxxxxxxx",
        "ZOTERO_MCP_COMPAT": "1"
      }
    }
  }
}
```

The environment variables the old server read (`ZOTERO_LIBRARY_ID`,
`ZOTERO_LIBRARY_TYPE`, `ZOTERO_API_KEY`, `ZOTERO_LOCAL`) keep their names and
their meanings, and an old JSON config file is read and migrated in place of a
new one.

Compatibility mode is meant as a bridge, not a destination. The aliases are
registered on top of the consolidated tools, so a client in compatibility mode
sees a large surface again, which is the cost the small default set exists to
avoid.

## Name by name

| Old name | Now |
|---|---|
| `zotero_add_by_doi` | `zotero_add_item` |
| `zotero_add_by_url` | `zotero_add_item` |
| `zotero_add_from_file` | `zotero_add_item` |
| `zotero_advanced_search` | `zotero_search` |
| `zotero_batch_update_tags` | `zotero_tags` |
| `zotero_change_item_type` | `zotero_update_item` |
| `zotero_copy_items_to_library` | `zotero_manage_items(action='copy')` |
| `zotero_create_annotation` | `zotero_manage_annotation` |
| `zotero_create_area_annotation` | `zotero_manage_annotation` |
| `zotero_create_collection` | `zotero_collections` |
| `zotero_create_note` | `zotero_manage_note` |
| `zotero_delete_collection` | `zotero_collections` |
| `zotero_delete_items` | `zotero_manage_items` |
| `zotero_delete_note` | `zotero_manage_note` |
| `zotero_empty_trash` | `zotero_manage_items` |
| `zotero_export_bibtex` | `zotero_export` |
| `zotero_find_duplicates` | `zotero_duplicates` |
| `zotero_get_annotations` | `zotero_get_annotations`, same name, wider signature |
| `zotero_get_collection_items` | `zotero_collections` |
| `zotero_get_collections` | `zotero_collections` |
| `zotero_get_feed_items` | `zotero_library(action='feed_items')` |
| `zotero_get_item_children` | `zotero_get_item` |
| `zotero_get_item_fulltext` | `zotero_read` |
| `zotero_get_item_metadata` | `zotero_get_item` |
| `zotero_get_item_versions` | `zotero_library(action='versions')` |
| `zotero_get_items_by_type` | `zotero_library(action='by_type')` |
| `zotero_get_items_children` | `zotero_get_item` |
| `zotero_get_items_without_collection` | `zotero_library(action='uncollected')` |
| `zotero_get_library_stats` | `zotero_library(action='stats')` |
| `zotero_get_notes` | `zotero_get_annotations` |
| `zotero_get_pdf_outline` | `zotero_read(what='outline')` |
| `zotero_get_recent` | `zotero_library(action='recent')` |
| `zotero_get_search_database_status` | `zotero_index(action='status')` |
| `zotero_get_tags` | `zotero_tags` |
| `zotero_get_trash` | `zotero_manage_items` |
| `zotero_list_feeds` | `zotero_library(action='feeds')` |
| `zotero_list_libraries` | `zotero_library(action='list')` |
| `zotero_manage_collections` | `zotero_collections` |
| `zotero_merge_duplicates` | `zotero_duplicates` |
| `zotero_rename_collection` | `zotero_collections` |
| `zotero_restore_from_trash` | `zotero_manage_items` |
| `zotero_search_by_citation_key` | `zotero_search(mode='citation_key')` |
| `zotero_search_by_tag` | `zotero_search` |
| `zotero_search_collections` | `zotero_collections` |
| `zotero_search_items` | `zotero_search` |
| `zotero_search_notes` | `zotero_search(item_type='note')` |
| `zotero_semantic_search` | `zotero_search(mode='semantic')` |
| `zotero_switch_library` | `zotero_library(action='switch')` |
| `zotero_trash_items` | `zotero_manage_items` |
| `zotero_update_item` | `zotero_update_item`, same name, wider signature |
| `zotero_update_note` | `zotero_manage_note` |
| `zotero_update_search_database` | `zotero_index(action='update')` |

`zotero_update_item` and `zotero_get_annotations` keep their old names in the
new surface, so rather than aliasing them, compatibility mode replaces them
with versions that accept both the old argument shapes and the new ones.

## What deliberately behaves differently

These are the places where an old call still works but does not do exactly what
it used to. Each one is a choice, not an oversight.

**Failures raise.** A call that cannot be completed now returns an MCP error
with a code and a hint. The old server returned a successful result whose text
began with `"Error:"`, which reads to a model as a call that worked. If your
client inspected result text for that prefix, it can stop.

**Destructive calls preview.** `zotero_delete_items`, `zotero_trash_items`,
`zotero_empty_trash`, `zotero_merge_duplicates` and the rest default to
`dry_run=True` and describe what they would do. Pass `dry_run=False` to
perform it. The old tools acted immediately.

**Reads are bounded.** `zotero_get_item_fulltext` returned whole documents;
`zotero_read` returns a page range, reports how much was left, and tells you
how to ask for the next part. A call that used to return 200 pages now returns
the first few and says so.

**Listings paginate.** Tools that returned everything now return a page and a
cursor. The structured half of the result carries `total`, `returned` and
`next_cursor`.

**`zotero_advanced_search` with `join_mode='any'` is refused.** The
consolidated search is an AND of its filters, and an OR across conditions
cannot be expressed faithfully. Rather than quietly returning an AND, the alias
says so and points at running the searches separately. Every other join mode
works.

**A full index rebuild is a command line operation.**
`zotero_update_search_database(force_rebuild=True)` refuses and names the
command instead: rebuilding a large library inside a tool call blocks the
session for as long as it takes, with no way to report progress.

**Collections can still be named, not keyed.** The old tools accepted a
collection name where the new ones take a key. The aliases resolve names, and
refuse an ambiguous name rather than picking one of the matches.

## What is new

Worth knowing about once the bridge is in place:

- `zotero_search(mode='auto')` widens a query that finds nothing and reports
  what it tried, rather than returning an empty list.
- `zotero_add_item` takes any identifier (DOI, arXiv, ISBN, PMID, PMCID, URL)
  in one argument, and can attach the open-access PDF while it is there.
- `zotero_manage_items(action='copy')` copies items into another library.
- `zotero_library(action='versions')` reports what changed since a version, for
  callers keeping their own copy in step.
- `zotero_health` says what is working, what mode the backend resolved to, and
  which optional pieces are missing.
- `zotero_coverage` and `zotero_find_related` work over the semantic index.
- Resources and prompts, for clients that use them.
