# open-webui-to-pi

Convert [Open WebUI](https://github.com/open-webui/open-webui) chat exports (and
live server chats) into Pi session files.

Pi stores each conversation as a JSONL file under `~/.pi/agent/sessions/`, where
every line is a tree entry linked by `id`/`parentId`. This tool translates an
Open WebUI conversation — its message text, reasoning traces, model choices, and
branched trees — into that format so the history can be resumed in Pi.

## Usage

```bash
python3 owui_to_pi.py chat-export.json                  # local JSON export(s)
python3 owui_to_pi.py --server URL -t "$TOKEN"          # list available chats
python3 owui_to_pi.py --server URL -t "$TOKEN" <CHAT>   # fetch + convert a chat
```

Options:

- `export` — Path to an Open WebUI JSON export file, or (with `--server`) a
  chat id, unique id prefix, or title fragment.
- `-o, --output-dir` — Where session files are written. Defaults to Pi's
  sessions directory (`~/.pi/agent/sessions`).
- `--server URL` — Open WebUI base URL (e.g. `http://example.com`) instead
  of a local file.
- `-t, --token` — Bearer token for the server; falls back to `$OPENWEBUI_TOKEN`.

### Examples

```bash
# Convert every chat in an export file into Pi sessions.
python3 owui_to_pi.py ~/chat-export.json

# Show a table of chats available on a server (no chat id given).
python3 owui_to_pi.py --server http://example.com -t "$TOKEN"

# Fetch and convert one specific chat, by full id.
python3 owui_to_pi.py --server http://example.com -t "$TOKEN" \
    ae769e1a-634b-4716-a1c3-53c7a0d3f054

# Unique prefixes and title fragments also resolve.
python3 owui_to_pi.py --server http://example.com -t "$TOKEN" ae769e1a
```

## What is preserved

- **Conversation trees** — Every message keeps its `parentId`/`childrenIds`
  relationship, so branches survive the conversion.
- **Messages and reasoning** — Assistant text becomes Pi `text` blocks; Open
  WebUI `reasoning` output becomes Pi `thinking` blocks. User content stays as
  plain text.
- **Model metadata** — Each assistant message records its model, and synthetic
  `model_change` nodes mark when a different model started producing replies so
  Pi's context builder sees the selected model.

Anything Open WebUI exposes that has no equivalent in Pi (favorites, usage
breakdowns beyond token counts, image attachments) is intentionally dropped.

## Design

See [DESIGN.md](DESIGN.md) for the development history and decisions behind the
script. The conversion pipeline itself is documented in module docstrings.
