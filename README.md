# zotero-mcp-next

A ground-up rewrite of the Zotero [Model Context Protocol](https://modelcontextprotocol.io)
server.

Same capabilities as the server it replaces — and then some — but built around
three rules the original could not retrofit:

1. **No tool may return an unbounded response.** Every read is paginated,
   ranged or clamped, and truncation is reported explicitly.
2. **A failure is a failure.** Errors raise `ToolError` with a machine-readable
   code, never a successful result whose text begins with `"Error:"`.
3. **The tool surface is small by default.** ~15 consolidated tools ship out of
   the box; optional groups and the full pre-1.0 name set are opt-in.

Status: **under construction.** See [`docs/design.md`](docs/design.md).
