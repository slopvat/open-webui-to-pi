# Design Notes

This document is a synopsis of the development session that produced
`owui_to_pi.py`, written as an honest log of what was tried, what failed, and
what had to be revised along the way.

## Goal

The user wanted a Python script that converts Open WebUI chat exports (JSON)
into a form Pi can resume. The two things they cared about most were preserving
conversation trees and messages; any metadata Open WebUI exposed that Pi does
not support could simply be discarded.

## Step 1: Understand both formats

I started by inspecting the actual data rather than guessing at schemas.

**Open WebUI export.** `chat-export-1791570694591.json` is an array, where each
entry wraps one conversation as:

```json
{ "id": "...", "user_id": "...", "title": "...",
  "chat": { "history": { "currentId": "...", "messages": {...} } } }
```

Messages are keyed by id and linked through `parentId`/`childrenIds`.
`history.currentId` marks the active node (a leaf in the tree). Assistant
answers live in a per-message `output[]`: one block of type `message` holds the
rendered text, plus an optional `reasoning` block. This particular export had
754 messages and 74 branch points.

**Pi sessions.** Sessions are JSONL files under
`~/.pi/agent/sessions/--<path>--/<timestamp>_<session-id>.jsonl`. Line zero is a
`session` header; the remaining lines are tree entries with `id`/`parentId`.
Message content is an array of blocks (`text`, `thinking`, `toolCall`,
etc.). I read Pi's own docs and its compiled `SessionManager` source to confirm
the exact shapes required (e.g. assistant messages need a `usage` object).

## Step 2: The key insight about the "leaf"

The crucial discovery — which drove almost everything else — was how Pi picks
the active branch. I traced it in
`packages/coding-agent/dist/core/session-manager.js`: when a session file loads,
`_buildIndex()` iterates every line and sets `leafId = entry.id` for each one.
So the **last non-header entry written is treated as the leaf**; there is no
explicit "current position" field to set.

Consequence: preserving Open WebUI's *active* branch means writing entries in an
order where (a) every child follows its parent, and (b) for any branching node,
the child on `currentId`'s path comes last. That way the final written entry is
exactly Open WebUI's active leaf.

## Step 3: Iteration and failures

Several attempts were wrong before they worked:

- **Timestamp units.** The first run produced header timestamps like
  `1970-01-21T16:45...`. Open WebUI stores epochs in *seconds* (~1.7e9) while Pi
  wants Unix milliseconds; my initial code treated everything as ms. I added a
  heuristic that treats values under ~3 billion as seconds and multiplies by
  1000.

- **The chat wrapper.** My first version read `history` straight off the export
  top level, but it was actually nested one layer down under each record's
  `chat` key. After I made an initial `render()` call return "no messages," I
  realized a conversation could be wrapped in either of two ways and wrote logic
  to accept both.

- **Empty assistant responses.** One Open WebUI message had empty content with no
  output text; Pi rejects an assistant whose `content` array is empty. My fix was
  to synthesize a placeholder block so the tree node still exists. (Pi's loader,
  it turned out, actually tolerates missing/null content — but keeping every
  assistant with non-empty content is cleaner and more robust.)

- **Sibling-ordering of model nodes.** Early on I wrote `entries()` assuming an
  id-order list would let me insert switch nodes "between" entries by repointing
  a message's parent at a preceding sibling. That broke tree order (a child must
  appear after its parent) and produced dangling/out-of-order links under Pi's
  loader. I revised it to thread the `model_change` node *as* the announcing
  message's direct parent, exactly like Pi's own `appendModelChange()`, which
  keeps every real descendant reachable and preserves a valid file order.

## Step 4: Model metadata

The user asked that model metadata be included. Pi tracks this two ways:

1. Per-message — each assistant `message` carries `api`/`provider`/`model`.
2. Session-level — `model_change` tree entries record when the selected model
   switches.

I carried over per-message models from the start, but initially *did not*
synthesize session-level `model_change` nodes because an export gives no
switch timeline (only each message's model). When asked to include them anyway,
the user explicitly said: fabricate timestamps based on the first message
produced after a change. I confirmed from Pi's source that `getSessionContextSettings()`
reads `model_change` entries off the active path, so inserting one before the
first assistant reply under each newly-seen model makes the session context see
the right model — verified by walking an exported file through `SessionManager`.

## Step 5: Live server support

The user wanted to also pull chats straight from a running Open WebUI instance.
I probed the API with `curl` (`/api/v1/chats/` lists, `/api/v1/chats/<ID>`
fetches), which confirmed the responses use the exact same wrapper shape as an
export. I added stdlib-only HTTP fetching (no new dependencies) plus:

- Chat listing and a readable metadata table when no chat id is given.
- Resolution of full ids, unique prefixes, and title fragments.

The server and file paths now share one converter via `_extract_chat()`.

## Validation

Everything was checked by running the produced files back through Pi's own
`SessionManager.loadEntriesFromFile()`, asserting:

- The written leaf id matches Open WebUI's `currentId`.
- No dangling parent links; every non-root entry references an existing parent.
- Active-path entries appear in monotonically increasing file order (required
  for correct leaf selection).
- Assistant messages carry valid content/usage/model; the resolved current model
  from walking the path equals what Open WebUI recorded.

## Current model

This design was authored with model `frob/deepseek-v4-flash-0731:284b-a13b-ud-q8_k_xl`.
