# open-webui-to-pi

Convert [Open WebUI](https://github.com/open-webui/open-webui) chat exports (and
live server chats) into Pi session files.

Pi stores each conversation as a JSONL file under `~/.pi/agent/sessions/`, where
every line is a tree entry linked by `id`/`parentId`. This tool translates an
Open WebUI conversation — its message text, reasoning traces, model choices, and
branched trees — into that format so the history can be resumed in Pi.

## Usage

```bash
python3 owui_to_pi.py chat-export.json...               # local JSON export(s)
python3 owui_to_pi.py --server URL -t "$TOKEN"          # list available chats
python3 owui_to_pi.py --server URL -t "$TOKEN" CHAT...  # fetch + convert chats
```

Options:

- `export` — Paths to Open WebUI JSON export files, or (with `--server`) chat
  ids, unique id prefixes, or title fragments.
- `-o, --output-dir` — Where session files are written. Defaults to Pi's
  sessions directory (`~/.pi/agent/sessions`).
- `-C, --cwd` — Working directory recorded in the sessions. Pi only lists a
  session in `/resume` when started from that directory, so point this at the
  project the chats belong to. Defaults to the current directory.
- `--server URL` — Open WebUI base URL (e.g. `http://example.com`) instead
  of a local file.
- `-t, --token` — Bearer token for the server; falls back to `$OPENWEBUI_TOKEN`.

### Examples

```bash
# Convert every chat in an export file into Pi sessions for ~/src/fractals.
python3 owui_to_pi.py -C ~/src/fractals ~/chat-export.json

# Show a table of chats available on a server (no chat id given).
python3 owui_to_pi.py --server http://example.com -t "$TOKEN"

# Fetch and convert one specific chat, by full id.
python3 owui_to_pi.py --server http://example.com -t "$TOKEN" \
    ae769e1a-634b-4716-a1c3-53c7a0d3f054

# Unique prefixes and title fragments also resolve.
python3 owui_to_pi.py --server http://example.com -t "$TOKEN" ae769e1a
```

## What is preserved

- **Conversation trees** — Every message keeps its `parentId` relationship, so
  branches survive the conversion, including edits of the first message
  (which Open WebUI stores as additional roots).
- **Messages and reasoning** — Assistant text becomes Pi `text` blocks; Open
  WebUI `reasoning` output becomes Pi `thinking` blocks. User content stays as
  plain text.
- **Model metadata** — Each assistant message records its model, and synthetic
  `model_change` nodes mark where a different model started producing replies
  on each branch.

Anything Open WebUI exposes that has no equivalent in Pi (favorites, usage
breakdowns beyond token counts, image attachments, tool calls and their
results) is intentionally dropped.

Archived chats are not shown in the server listing, but can still be converted
by passing their full id.

## Design

See [DESIGN.md](DESIGN.md) for the development history and decisions behind the
script. The conversion pipeline itself is documented in module docstrings.

**AI Disclosure:**
- **Authorship:** AI-generated
- **Models:** frob/deepseek-v4-flash-0731:284b-a13b-ud-q8_k_xl, claude-opus-5-5
  (Anthropic)
- **Scope:** Written alongside the script; the second model updated the usage
  and preservation notes after reviewing and fixing the script.
