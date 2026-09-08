"""Tool registration: the read surface exposed to the MCP client.

One function, :func:`register_tools`, binds a configured backend to a FastMCP
instance. Each tool is thin by design — it validates and pages its arguments,
asks the backend for raw payloads, maps them through :mod:`zotero_mcp.mapping`,
and hands markdown-plus-structured back through :func:`zotero_mcp.results.reply`.
All the hard rules (bounded responses, typed errors, cursor integrity) live in
the modules those calls route through, so a tool cannot forget to apply them.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from zotero_mcp import mapping, render, schema
from zotero_mcp.backends import ZoteroBackend
from zotero_mcp.config import ZoteroConfig
from zotero_mcp.errors import InvalidInput, Unsupported, tool_errors
from zotero_mcp.index import SemanticIndex, semantic_available
from zotero_mcp.models import (
    Change,
    CollectionPage,
    HealthReport,
    ItemSummary,
    LibraryStats,
    ResultPage,
    SearchDiagnostics,
    WriteResult,
)
from zotero_mcp.paging import Page, decode_cursor, normalize_page_size
from zotero_mcp.results import reply, text_reply
from zotero_mcp.schema import schema_version


def register_tools(mcp: Any, config: ZoteroConfig, backend: ZoteroBackend) -> None:
    """Register every read tool on *mcp*."""

    limits = config.limits
    allow_structured = config.surface.structured_output
    library = backend.library_ref()
    index = SemanticIndex(config) if config.semantic.enabled else None
    if index is not None:
        index.warm_up()

    def _page_size(value: int | str | None) -> int:
        return normalize_page_size(
            value, default=limits.default_page_size, maximum=limits.max_page_size
        )

    def _snippet(text: str, chars: int) -> str:
        """One-line preview of a matched passage, bounded like an abstract preview."""
        flat = " ".join(text.split())
        return flat if len(flat) <= chars else flat[:chars].rstrip() + "…"

    @mcp.tool
    @tool_errors
    def search_library(
        query: Annotated[
            str,
            Field(
                description="Words to match anywhere in the item (title, author, abstract, tags, fulltext). Empty lists recent items."
            ),
        ] = "",
        item_type: Annotated[
            str | None,
            Field(description="Restrict to one Zotero item type, e.g. 'journalArticle', 'book'."),
        ] = None,
        limit: Annotated[int | None, Field(description="Max items to return this page.")] = None,
        cursor: Annotated[
            str | None,
            Field(
                description="The 'next_cursor' from a previous call, to fetch the next page. Keep every other argument identical."
            ),
        ] = None,
    ) -> Any:
        """Search the active Zotero library and return a page of matching items.

        Each result carries the item key you pass to `get_item`, plus author,
        year, and a short abstract preview.
        """
        size = _page_size(limit)
        q = {"q": query or "", "itemType": item_type or ""}
        offset = decode_cursor(cursor, q) if cursor else 0

        raw = backend.search(query or None, item_type=item_type, limit=size, start=offset)
        summaries = [
            mapping.to_item_summary(
                r, abstract_chars=limits.abstract_preview_chars, library=library
            )
            for r in raw
        ]
        page: Page = Page.build(summaries, offset=offset, page_size=size, query=q)

        result = ResultPage(
            items=page.items,
            offset=page.offset,
            returned=len(page.items),
            next_cursor=page.next_cursor,
        )
        heading = f"Search results for “{query}”" if query else "Recent items"
        md = render.render_result_page(result, heading=heading)
        return reply(md, result, limit=limits.max_response_chars, allow_structured=allow_structured)

    @mcp.tool
    @tool_errors
    def get_item(
        item_key: Annotated[
            str, Field(description="The 8-character Zotero item key from a search result.")
        ],
    ) -> Any:
        """Fetch the full record for one item, including its attachments and note count."""
        raw = backend.item(item_key)
        children = backend.children(item_key)
        detail = mapping.to_item_detail(raw, library=library, children=children)
        md = render.render_item_detail(detail)
        return reply(md, detail, limit=limits.max_response_chars, allow_structured=allow_structured)

    @mcp.tool
    @tool_errors
    def list_collections(
        limit: Annotated[int | None, Field(description="Max collections per page.")] = None,
        cursor: Annotated[
            str | None, Field(description="The 'next_cursor' from a previous call.")
        ] = None,
    ) -> Any:
        """List the library's collections as slash-joined paths (Parent/Child)."""
        size = _page_size(limit)
        q = {"kind": "collections"}
        offset = decode_cursor(cursor, q) if cursor else 0

        raw = backend.collections()
        paths = mapping.collection_paths(raw)
        refs = sorted(
            (
                mapping.to_collection_ref(
                    c, path=paths.get(c.get("key") or (c.get("data") or {}).get("key"))
                )
                for c in raw
            ),
            key=lambda c: (c.path or c.name).lower(),
        )
        window = refs[offset : offset + size]
        page: Page = Page.build(window, offset=offset, page_size=size, query=q, total=len(refs))
        result = CollectionPage(
            collections=page.items,
            total=len(refs),
            offset=page.offset,
            returned=len(page.items),
            next_cursor=page.next_cursor,
        )
        md = render.render_collections(result, heading="Collections")
        return reply(md, result, limit=limits.max_response_chars, allow_structured=allow_structured)

    @mcp.tool
    @tool_errors
    def collection_items(
        collection_key: Annotated[
            str, Field(description="The collection key from `list_collections`.")
        ],
        limit: Annotated[int | None, Field(description="Max items per page.")] = None,
        cursor: Annotated[
            str | None, Field(description="The 'next_cursor' from a previous call.")
        ] = None,
    ) -> Any:
        """List the top-level items in one collection."""
        size = _page_size(limit)
        q = {"collection": collection_key}
        offset = decode_cursor(cursor, q) if cursor else 0

        raw = backend.collection_items(collection_key, limit=size, start=offset)
        summaries = [
            mapping.to_item_summary(
                r, abstract_chars=limits.abstract_preview_chars, library=library
            )
            for r in raw
        ]
        page: Page = Page.build(summaries, offset=offset, page_size=size, query=q)
        result = ResultPage(
            items=page.items,
            offset=page.offset,
            returned=len(page.items),
            next_cursor=page.next_cursor,
        )
        md = render.render_result_page(result, heading=f"Items in collection {collection_key}")
        return reply(md, result, limit=limits.max_response_chars, allow_structured=allow_structured)

    @mcp.tool
    @tool_errors
    def list_tags(
        limit: Annotated[int | None, Field(description="Max tags to return.")] = None,
    ) -> Any:
        """List tags used in the library."""
        size = _page_size(limit)
        raw = backend.tags(limit=size)
        tags = [mapping.to_tag_count(t) for t in raw]
        md = render.render_tags(tags, heading="Tags")
        return text_reply(md, limit=limits.max_response_chars)

    @mcp.tool
    @tool_errors
    def library_stats() -> Any:
        """Report a quantitative overview of the active library."""
        collections = backend.collections()
        stats = LibraryStats(
            library=library,
            total_items=backend.count(),
            collection_count=len(collections),
            tag_count=len(backend.all_tags()),
        )
        md = render.render_stats(stats)
        return reply(md, stats, limit=limits.max_response_chars, allow_structured=allow_structured)

    @mcp.tool
    @tool_errors
    def server_health() -> Any:
        """Check that the server can actually reach the configured Zotero library."""
        warnings: list[str] = []
        reachable = True
        try:
            backend.ping()
        except Exception as exc:  # noqa: BLE001 - reported, not raised, so health always answers
            reachable = False
            warnings.append(str(exc))

        index_state, indexed = ("disabled", 0)
        if index is not None:
            index_state, indexed = index.status()
            if index_state == "empty":
                warnings.append("Semantic index is empty; run `zotero-mcp index build`.")
            elif index_state == "unavailable":
                warnings.append("Semantic search needs the 'semantic' extra installed.")

        report = HealthReport(
            backend="local" if backend.local else "web",
            reachable=reachable,
            library=library,
            schema_version=schema_version(),
            semantic_index=index_state,
            indexed_items=indexed if index is not None else None,
            optional_features={
                "semantic_search": index_state == "ready",
                "writes": backend.can_write,
                "external_services": config.network.allow_external_services,
            },
            warnings=warnings,
        )
        md = render.render_health(report)
        return reply(md, report, limit=limits.max_response_chars, allow_structured=allow_structured)

    if index is not None:

        @mcp.tool
        @tool_errors
        def semantic_search(
            query: Annotated[
                str,
                Field(
                    description="A natural-language description of what you are looking for — a topic, claim, method, or question. Matches by meaning, not exact words."
                ),
            ],
            limit: Annotated[int | None, Field(description="Max items to return.")] = None,
        ) -> Any:
            """Find items by meaning using the vector index. Each hit shows the passage that matched.

            Complements `search_library` (exact-word matching): use this when you
            know the idea but not the wording. Requires the index to have been built.
            """
            if not semantic_available():
                raise Unsupported(
                    "Semantic search needs the optional dependencies.",
                    hint="Reinstall with the 'semantic' extra, then run `zotero-mcp index build`.",
                )
            state, _ = index.status()
            if state != "ready":
                raise Unsupported(
                    "The semantic index has not been built yet.",
                    hint="Run `zotero-mcp index build` from a terminal, then retry.",
                )
            size = _page_size(limit)
            hits = index.search(query, limit=size)
            items = [
                ItemSummary(
                    key=h.item_key,
                    title=h.metadata.get("title") or "Untitled",
                    item_type=h.metadata.get("item_type") or "document",
                    year=h.metadata.get("year") or None,
                    creator_summary=h.metadata.get("creator_summary") or None,
                    publication=h.metadata.get("publication") or None,
                    score=round(h.score, 3),
                    matched_text=_snippet(h.matched_text, limits.abstract_preview_chars),
                    zotero_uri=mapping.zotero_uri(h.item_key, library),
                )
                for h in hits
            ]
            result = ResultPage(
                items=items,
                returned=len(items),
                diagnostics=SearchDiagnostics(
                    strategy="semantic",
                    attempts=[f"vector search: {len(items)} items"],
                ),
            )
            md = render.render_result_page(result, heading=f"Semantic matches for “{query}”")
            return reply(
                md, result, limit=limits.max_response_chars, allow_structured=allow_structured
            )

    # -- writes -------------------------------------------------------------
    # Registered only when the backend can actually mutate the library, so a
    # read-only install does not advertise tools that can only fail.
    if not backend.can_write:
        return

    def _write_reply(result: WriteResult) -> Any:
        md = render.render_write_result(result)
        return reply(md, result, limit=limits.max_response_chars, allow_structured=allow_structured)

    def _normalize_tags(value: list[str] | str | None) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            value = [value]
        return [t.strip() for t in value if t and t.strip()]

    @mcp.tool
    @tool_errors
    def create_item(
        item_type: Annotated[
            str, Field(description="Zotero item type, e.g. 'journalArticle', 'book', 'webpage'.")
        ],
        title: Annotated[str, Field(description="The item's title.")],
        creators: Annotated[
            list[dict] | None,
            Field(
                description="Authors etc. as objects: {'creator_type':'author','first_name':'Ada','last_name':'Lovelace'} or {'creator_type':'author','name':'Org Name'}."
            ),
        ] = None,
        fields: Annotated[
            dict | None,
            Field(
                description="Other Zotero fields under their real names, e.g. {'date':'2021','DOI':'10.x/y','publicationTitle':'Nature'}."
            ),
        ] = None,
        tags: Annotated[list[str] | None, Field(description="Tags to attach.")] = None,
        collections: Annotated[
            list[str] | None, Field(description="Collection keys to file the item under.")
        ] = None,
        dry_run: Annotated[
            bool,
            Field(
                description="Preview only when true (the default). Pass false to actually create the item."
            ),
        ] = True,
    ) -> Any:
        """Create a new item in the library. Previews by default; pass dry_run=false to write."""
        if not schema.is_item_type(item_type):
            raise InvalidInput(
                f"Unknown item type {item_type!r}.",
                hint="Use a Zotero type such as 'journalArticle', 'book', or 'conferencePaper'.",
            )
        proposed = {"itemType": item_type, "title": title}
        if fields:
            proposed.update(fields)
        changes = [Change(field=k, after=v) for k, v in proposed.items()]
        if creators:
            changes.append(Change(field="creators", after=f"{len(creators)} creator(s)"))
        if tags:
            changes.append(Change(field="tags", after=", ".join(tags)))
        if collections:
            changes.append(Change(field="collections", after=", ".join(collections)))

        if dry_run:
            return _write_reply(
                WriteResult(
                    action="create_item",
                    dry_run=True,
                    changes=changes,
                    message=f"Would create a {item_type}.",
                )
            )

        template = backend.item_template(item_type)
        template["title"] = title
        for k, v in (fields or {}).items():
            template[k] = v
        if creators:
            template["creators"] = [
                {
                    "creatorType": c.get("creator_type", "author"),
                    **(
                        {"name": c["name"]}
                        if c.get("name")
                        else {
                            "firstName": c.get("first_name", ""),
                            "lastName": c.get("last_name", ""),
                        }
                    ),
                }
                for c in creators
            ]
        if tags:
            template["tags"] = [{"tag": t} for t in _normalize_tags(tags)]
        if collections:
            template["collections"] = list(collections)

        resp = backend.create_items([template])
        if resp.get("failed"):
            reason = next(iter(resp["failed"].values()))
            raise InvalidInput(f"Zotero rejected the item: {reason}")
        created = resp["successful"]["0"]
        return _write_reply(
            WriteResult(
                action="create_item",
                dry_run=False,
                succeeded=[created["key"]],
                created_key=created["key"],
                version=created.get("version"),
                message=f"Created {item_type} “{title}”.",
            )
        )

    @mcp.tool
    @tool_errors
    def update_item(
        item_key: Annotated[str, Field(description="Key of the item to update.")],
        fields: Annotated[
            dict,
            Field(
                description="Fields to set, under their real Zotero names, e.g. {'title':'New title','date':'2022'}."
            ),
        ],
        dry_run: Annotated[
            bool, Field(description="Preview only when true (default). Pass false to apply.")
        ] = True,
    ) -> Any:
        """Update fields on an existing item. Previews by default; dry_run=false to apply."""
        if not fields:
            raise InvalidInput("No fields given to update.")
        item = backend.write_item(item_key)
        data = item["data"]
        changes = [
            Change(field=k, before=data.get(k), after=v)
            for k, v in fields.items()
            if data.get(k) != v
        ]
        if not changes:
            return _write_reply(
                WriteResult(
                    action="update_item",
                    dry_run=dry_run,
                    unchanged=[item_key],
                    message="Every field already has the requested value.",
                )
            )
        if dry_run:
            return _write_reply(
                WriteResult(
                    action="update_item", dry_run=True, succeeded=[item_key], changes=changes
                )
            )
        data.update(fields)
        new_version = backend.update_item(item)
        return _write_reply(
            WriteResult(
                action="update_item",
                dry_run=False,
                succeeded=[item_key],
                changes=changes,
                version=new_version,
            )
        )

    @mcp.tool
    @tool_errors
    def delete_item(
        item_key: Annotated[str, Field(description="Key of the item to move to Trash.")],
        dry_run: Annotated[
            bool, Field(description="Preview only when true (default). Pass false to trash it.")
        ] = True,
    ) -> Any:
        """Move an item to Zotero's Trash (recoverable). Previews by default."""
        item = backend.write_item(item_key)
        title = item["data"].get("title") or "(untitled)"
        if dry_run:
            return _write_reply(
                WriteResult(
                    action="delete_item",
                    dry_run=True,
                    succeeded=[item_key],
                    message=f"Would move “{title}” to the Trash (recoverable).",
                )
            )
        backend.trash_item(item)
        return _write_reply(
            WriteResult(
                action="delete_item",
                dry_run=False,
                succeeded=[item_key],
                message=f"Moved “{title}” to the Trash. Restore it from the Zotero client if needed.",
            )
        )

    @mcp.tool
    @tool_errors
    def modify_tags(
        item_key: Annotated[str, Field(description="Key of the item to retag.")],
        add_tags: Annotated[list[str] | None, Field(description="Tags to add.")] = None,
        remove_tags: Annotated[list[str] | None, Field(description="Tags to remove.")] = None,
        dry_run: Annotated[
            bool, Field(description="Preview only when true (default). Pass false to apply.")
        ] = True,
    ) -> Any:
        """Add and/or remove tags on an item, keeping its other tags. Previews by default."""
        add = _normalize_tags(add_tags)
        remove = set(_normalize_tags(remove_tags))
        if not add and not remove:
            raise InvalidInput("Give add_tags and/or remove_tags.")
        item = backend.write_item(item_key)
        current = [t.get("tag") for t in item["data"].get("tags") or []]
        current_set = set(current)
        added = [t for t in add if t not in current_set]
        removed = [t for t in current if t in remove]
        if not added and not removed:
            return _write_reply(
                WriteResult(
                    action="modify_tags",
                    dry_run=dry_run,
                    unchanged=[item_key],
                    message="No tag changes needed.",
                )
            )
        changes = [
            Change(field="tags", before=current, after=sorted((current_set | set(added)) - remove))
        ]
        if dry_run:
            return _write_reply(
                WriteResult(
                    action="modify_tags", dry_run=True, succeeded=[item_key], changes=changes
                )
            )
        item["data"]["tags"] = [{"tag": t} for t in sorted((current_set | set(added)) - remove)]
        new_version = backend.update_item(item)
        return _write_reply(
            WriteResult(
                action="modify_tags",
                dry_run=False,
                succeeded=[item_key],
                changes=changes,
                version=new_version,
            )
        )

    @mcp.tool
    @tool_errors
    def modify_collections(
        item_key: Annotated[str, Field(description="Key of the item to move.")],
        add_to: Annotated[
            list[str] | None, Field(description="Collection keys to add the item to.")
        ] = None,
        remove_from: Annotated[
            list[str] | None, Field(description="Collection keys to remove the item from.")
        ] = None,
        dry_run: Annotated[
            bool, Field(description="Preview only when true (default). Pass false to apply.")
        ] = True,
    ) -> Any:
        """Add/remove an item's collection membership, keeping its other collections. Previews by default."""
        add = [c for c in (add_to or []) if c]
        remove = {c for c in (remove_from or []) if c}
        if not add and not remove:
            raise InvalidInput("Give add_to and/or remove_from collection keys.")
        item = backend.write_item(item_key)
        current = list(item["data"].get("collections") or [])
        current_set = set(current)
        target = sorted((current_set | set(add)) - remove)
        if set(target) == current_set:
            return _write_reply(
                WriteResult(
                    action="modify_collections",
                    dry_run=dry_run,
                    unchanged=[item_key],
                    message="Collection membership already matches.",
                )
            )
        changes = [Change(field="collections", before=current, after=target)]
        if dry_run:
            return _write_reply(
                WriteResult(
                    action="modify_collections", dry_run=True, succeeded=[item_key], changes=changes
                )
            )
        item["data"]["collections"] = target
        new_version = backend.update_item(item)
        return _write_reply(
            WriteResult(
                action="modify_collections",
                dry_run=False,
                succeeded=[item_key],
                changes=changes,
                version=new_version,
            )
        )

    @mcp.tool
    @tool_errors
    def create_note(
        text: Annotated[
            str,
            Field(
                description="Note body. Plain text or HTML; plain text is wrapped in a paragraph."
            ),
        ],
        item_key: Annotated[
            str | None,
            Field(description="Parent item key to attach the note to. Omit for a standalone note."),
        ] = None,
        dry_run: Annotated[
            bool, Field(description="Preview only when true (default). Pass false to create it.")
        ] = True,
    ) -> Any:
        """Create a child note on an item (or a standalone note). Previews by default."""
        if not text or not text.strip():
            raise InvalidInput("A note needs some text.")
        html = text if "<" in text and ">" in text else f"<p>{text}</p>"
        if dry_run:
            where = f"on item {item_key}" if item_key else "as a standalone note"
            return _write_reply(
                WriteResult(
                    action="create_note",
                    dry_run=True,
                    changes=[Change(field="note", after=text[:200])],
                    message=f"Would create a note {where}.",
                )
            )
        note = backend.item_template("note")
        note["note"] = html
        if item_key:
            note["parentItem"] = item_key
        resp = backend.create_items([note])
        if resp.get("failed"):
            reason = next(iter(resp["failed"].values()))
            raise InvalidInput(f"Zotero rejected the note: {reason}")
        created = resp["successful"]["0"]
        return _write_reply(
            WriteResult(
                action="create_note",
                dry_run=False,
                succeeded=[created["key"]],
                created_key=created["key"],
                version=created.get("version"),
                message="Note created.",
            )
        )


__all__ = ["register_tools"]
