# Filect MCP (experimental)

A standalone [MCP](https://modelcontextprotocol.io) server that lets an MCP client
(Claude Desktop / Claude Code) use Filect's capabilities. **It does not modify the app** —
it only imports `app.core.*` and calls the same functions the app uses, and it is **not**
part of the shipped build. Lives on the `mcp-experiment` branch.

## v1 is READ-ONLY
Nothing here moves, deletes, or re-indexes files, or changes app settings. It reads the
local index DB + your saved Filect session, and makes the same paid AI calls the app makes.

| tool | what it does | cost |
|------|--------------|------|
| `search_files(query, limit=20)` | semantic search over your index | free (local) |
| `get_file_details(path)` | stored tags/caption/category for one file | free (local) |
| `analyze_image(path)` | vision label + tags + caption (images only) | paid AI call |
| `transcribe_file(path)` | transcribe an audio file | paid AI call |
| `suggest_custom_words(niche)` | suggest dictation custom words for a field (returns only) | paid AI call |

> **Not yet:** video+sound analysis (would need frame + audio-track extraction — a new app
> feature), organization-plan preview, and anything that moves files. Those come later and
> the file-moving ones will require an explicit confirmation.

## Requirements
- You must be **signed into Filect** at least once (the server reuses the session tokens
  Filect saved in `~/.config/ai-file-organizer/settings.json`). Cloud tools need an active
  subscription, same as the app.
- The MCP SDK is installed in the dev venv: `venv311/bin/pip install "mcp>=1.2.0"`.

## Run it
Self-contained (the script puts the app on its own import path):

```bash
QT_QPA_PLATFORM=offscreen \
  /Users/damianosmalliaros/Developer/Mac_app/venv311/bin/python \
  /Users/damianosmalliaros/Developer/Mac_app/filect_mcp/server.py
```

## Register in Claude Desktop
Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "filect": {
      "command": "/Users/damianosmalliaros/Developer/Mac_app/venv311/bin/python",
      "args": ["/Users/damianosmalliaros/Developer/Mac_app/filect_mcp/server.py"],
      "env": { "QT_QPA_PLATFORM": "offscreen" }
    }
  }
}
```

## Register in Claude Code
```bash
claude mcp add filect --env QT_QPA_PLATFORM=offscreen -- \
  /Users/damianosmalliaros/Developer/Mac_app/venv311/bin/python \
  /Users/damianosmalliaros/Developer/Mac_app/filect_mcp/server.py
```

Then ask Claude things like *"search Filect for my tax PDFs"*, *"analyze this screenshot"*,
or *"suggest dictation words for cardiology and tell me which to add."*
