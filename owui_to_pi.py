#!/usr/bin/env python3

# AI Disclosure:
# - Authorship: AI-generated
# - Models: claude-opus-5-5 (Anthropic),
#     frob/deepseek-v4-flash-0731:284b-a13b-ud-q8_k_xl
# - Scope: Generated from a specification and iterative feedback in agentic
#   sessions. The deepseek model designed the conversion: the Pi session
#   schema, tree ordering, synthesized model_change nodes and server
#   fetching. claude-opus-5-5 reviewed it against Pi's and Open WebUI's
#   sources, rewrote most of the code to fix bugs and remove dead code, and
#   added clock-skew correction after the user questioned the timestamps.
# - References:
#   - https://github.com/earendil-works/pi (73f8de8)
#   - https://github.com/open-webui/open-webui (91ea4e3)
"""Convert an Open WebUI JSON export or a server-hosted chat to Pi sessions.

Pi reads its sessions as JSONL files where each line is a tree entry with an
``id`` and ``parentId`` (see docs/session-format.md in the pi repository). The
active branch ("leaf") is simply the last entry in file order, so preserving
the conversation trees means writing entries in topological order such that
every child follows its parent and, for any parent with several children, the
one on Open WebUI's *current* path comes last.

Open WebUI exports an array of chats. Each chat has a ``history`` whose
messages are keyed by id and linked via ``parentId``; ``history.currentId``
marks the active node (a leaf). Assistant answers live in each message's
``output`` list as blocks of type "message" (the rendered text) plus optional
"reasoning" blocks. All other Open WebUI metadata is dropped.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

PROVIDER = "open-webui-import"
API = "openai-completions"
# Smallest browser/server clock disagreement worth correcting.
CLOCK_SKEW_MIN_MS = 60_000

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _to_ms(value):
    """Normalize an Open WebUI epoch to Unix milliseconds (None if invalid).

    Open WebUI stores most timestamps in seconds (~1.7e9) but a few, such as
    the chat-level ``timestamp``, in milliseconds (~1.7e12). Anything below a
    plausible millisecond value is treated as seconds."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return int(f * 1000) if f < 3_000_000_000 else int(f)


def _iso(ms):
    """Unix milliseconds -> ISO 8601 UTC timestamp, like JS toISOString()."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _human_ts(ms):
    """Short human-readable UTC time for the listing table."""
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000,
                                  tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _first(value):
    """Open WebUI stores models as either a string or a list of strings."""
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if value else None


def encode_cwd_dir(cwd):
    """Mirror Pi's getDefaultSessionDirPath(): drop one leading separator,
    turn /, backslash and : into '-', then wrap in '--...--'."""
    return "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", cwd)) + "--"


def _session_id(chat):
    raw = str(chat.get("id") or "")
    if _UUID_RE.fullmatch(raw):
        return raw
    if not raw:
        raw = json.dumps(chat, sort_keys=True)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "open-webui:" + raw))


def fetch_json(url, token):
    """GET a JSON resource with a bearer token."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body[:300]}") \
            from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc.reason}") from None
    except ValueError as exc:
        raise RuntimeError(f"invalid JSON from {url}: {exc}") from None


def server_chats(base_url, token):
    """List available chats (metadata only) from an Open WebUI server.

    The endpoint hides pinned chats and chats inside folders unless asked."""
    url = (f"{base_url}/api/v1/chats/"
           "?include_pinned=true&include_folders=true")
    data = fetch_json(url, token)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected chats response: {type(data).__name__}")
    return data


def server_fetch_chat(base_url, chat_id, token):
    """Fetch the full JSON for one chat."""
    return fetch_json(f"{base_url}/api/v1/chats/{chat_id}", token)


def resolve_chat_ids(listing, tokens):
    """Turn user-supplied chat ids, unique id prefixes or title fragments into
    full chat ids. Full UUIDs absent from the listing (e.g. archived chats)
    are passed through so they can still be fetched directly."""
    known = {c.get("id") for c in listing}
    ids = []
    for tkn in tokens:
        if tkn in known or _UUID_RE.fullmatch(tkn):
            ids.append(tkn)
            continue
        cands = ([c for c in listing if str(c.get("id", "")).startswith(tkn)]
                 or [c for c in listing
                     if tkn.lower() in str(c.get("title") or "").lower()])
        if len(cands) > 1:
            raise RuntimeError(f"'{tkn}' is ambiguous ({len(cands)} chats); "
                               f"use a full id")
        if not cands:
            raise RuntimeError(f"no chat found for '{tkn}'")
        ids.append(cands[0]["id"])
    return ids


class OWUIConverter:
    def __init__(self, chat, cwd):
        # Exports and the server both wrap each chat as {'id', 'title', ...,
        # 'chat': {...history...}}; also accept a bare chat with 'history'.
        # Top-level fields win since the inner 'id' may be blank.
        if not isinstance(chat, dict):
            raise TypeError(
                f"expected a chat object, got {type(chat).__name__}")
        inner = chat.get("chat")
        if "history" not in chat and isinstance(inner, dict):
            chat = {**inner, **{k: v for k, v in chat.items() if k != "chat"}}
        self.chat = chat
        self.cwd = cwd
        history = chat.get("history") or {}
        self.messages = {mid: m for mid, m in
                         (history.get("messages") or {}).items()
                         if isinstance(m, dict)}
        self.initial_model = _first(chat.get("models"))

        # Every message's own timestamp, corrected for clock skew, falling
        # back to the chat's.
        chat_ms = (_to_ms(chat.get("timestamp"))
                   or _to_ms(chat.get("created_at"))
                   or int(datetime.now(timezone.utc).timestamp() * 1000))
        own = {mid: _to_ms(m.get("timestamp"))
               for mid, m in self.messages.items()}
        self.clock_skew = self._clock_skew(own)
        self.ms = {}
        for mid, m in self.messages.items():
            ms = own[mid]
            if ms is None:
                ms = chat_ms
            elif self.clock_skew and m.get("role") == "assistant":
                ms -= self.clock_skew
            self.ms[mid] = ms
        self.created_ms = min(self.ms.values(), default=chat_ms)

        active = set()
        node = history.get("currentId")
        while node in self.messages and node not in active:
            active.add(node)
            node = self.messages[node].get("parentId")

        # Messages whose parent is missing become extra roots (Pi allows
        # several) instead of being dropped. Siblings on the active path sort
        # last so currentId ends up as the final entry written.
        self.roots = []
        self.children = {}
        for mid, m in self.messages.items():
            parent = m.get("parentId")
            if parent in self.messages and parent != mid:
                self.children.setdefault(parent, []).append(mid)
            else:
                self.roots.append(mid)
        self.roots.sort(key=lambda k: k in active)
        for kids in self.children.values():
            kids.sort(key=lambda k: k in active)

        self._used_ids = set()
        self.id_map = {mid: self._new_id(mid) for mid in self.messages}

    def _clock_skew(self, own):
        """Milliseconds by which assistant timestamps run ahead of user ones.

        Open WebUI stamps user messages with the browser's clock but creates
        assistant messages server-side with the server's, so a wrong clock
        on either side puts replies before their prompts or follow-ups
        before the replies they answer. Every prompt -> reply edge bounds
        the skew from above and every reply -> follow-up edge from below.
        The server creates the reply the moment the prompt arrives, so the
        upper bound is the estimate. Returns 0 unless the timestamps are out
        of order by more than CLOCK_SKEW_MIN_MS, or None if no single offset
        explains them (e.g. the clock was fixed partway through the chat)."""
        upper = lower = None
        for mid, m in self.messages.items():
            pid = m.get("parentId")
            if own[mid] is None or own.get(pid) is None:
                continue
            is_reply = m.get("role") == "assistant"
            if is_reply == (self.messages[pid].get("role") == "assistant"):
                continue
            if is_reply:
                gap = own[mid] - own[pid]
                upper = gap if upper is None else min(upper, gap)
            else:
                gap = own[pid] - own[mid]
                lower = gap if lower is None else max(lower, gap)
        if (lower or 0) <= CLOCK_SKEW_MIN_MS and \
                (upper or 0) >= -CLOCK_SKEW_MIN_MS:
            return 0
        if upper is None:
            return lower
        if lower is not None and lower > upper:
            return None
        return upper

    def _new_id(self, seed):
        """Stable Pi entry id: 8 hex chars, lengthened only on collision."""
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        n = 8
        while digest[:n] in self._used_ids:
            n += 1
        self._used_ids.add(digest[:n])
        return digest[:n]

    def entries(self):
        """Tree entries in parent-before-child order whose final entry is Open
        WebUI's currentId (Pi treats the last written entry as its leaf).

        model_change entries are threaded like Pi's appendModelChange(): when
        an assistant message's model differs from the one in effect on its own
        branch, a model_change entry becomes that message's parent, so it lies
        on every descendant chain."""
        out = []
        # (message id, parent entry id, model in effect on this branch)
        stack = [(mid, None, None) for mid in reversed(self.roots)]
        while stack:
            mid, parent_id, model = stack.pop()
            msg = self._to_pi_message(mid)
            timestamp = _iso(msg["timestamp"])
            if msg["role"] == "assistant" and msg["model"] != model:
                model = msg["model"]
                mc_id = self._new_id("model_change:" + mid)
                out.append({
                    "type": "model_change",
                    "id": mc_id,
                    "parentId": parent_id,
                    "timestamp": timestamp,
                    "provider": PROVIDER,
                    "modelId": model,
                })
                parent_id = mc_id
            entry_id = self.id_map[mid]
            out.append({
                "type": "message",
                "id": entry_id,
                "parentId": parent_id,
                "timestamp": timestamp,
                "message": msg,
            })
            # Depth-first, so each sibling's whole subtree is written before
            # the next sibling; the active child is pushed first, popped last.
            for kid in reversed(self.children.get(mid, [])):
                stack.append((kid, entry_id, model))
        return out

    def header(self, session_id):
        # Pi treats the header timestamp as the creation time; last activity
        # is derived from message timestamps.
        return {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": _iso(self.created_ms),
            "cwd": self.cwd,
        }

    def _to_pi_message(self, mid):
        m = self.messages[mid]
        if m.get("role") != "assistant":
            return {"role": "user",
                    "content": self._content_to_text(m.get("content")),
                    "timestamp": self.ms[mid]}

        blocks = []
        for block in m.get("output") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            text = self._extract_output_text(block)
            if not text:
                continue
            if btype == "reasoning":
                blocks.append({"type": "thinking", "thinking": text})
            elif btype == "message":
                blocks.append({"type": "text", "text": text})

        if not any(b["type"] == "text" for b in blocks):
            fallback = self._content_to_text(m.get("content")).strip()
            if fallback:
                blocks.append({"type": "text", "text": fallback})
        # An assistant turn with no recorded output still gets a non-empty
        # content array; keep the tree node and mark it.
        if not blocks:
            blocks.append({"type": "text", "text": "(no response recorded)"})

        usage = m.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        info = m.get("info") if isinstance(m.get("info"), dict) else {}
        tokens_in = self._u_int(usage, info, "prompt_tokens", "input_tokens")
        tokens_out = self._u_int(usage, info, "completion_tokens",
                                 "output_tokens")
        total = (self._u_int(usage, info, "total_tokens")
                 or tokens_in + tokens_out)

        return {
            "role": "assistant",
            "content": blocks,
            "api": API,
            "provider": PROVIDER,
            "model": (_first(m.get("model")) or _first(m.get("modelName"))
                      or self.initial_model or "unknown"),
            # Pi's AssistantMessage requires a usage object.
            "usage": {
                "input": tokens_in,
                "output": tokens_out,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": total,
                "cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0,
                         "cacheWrite": 0.0, "total": 0.0},
            },
            "stopReason": "stop",
            "timestamp": self.ms[mid],
        }

    @staticmethod
    def _u_int(usage, info, *keys):
        """First usable integer among ``keys`` in usage, then in info."""
        for source in (usage, info):
            for k in keys:
                try:
                    return int(source[k])
                except (KeyError, TypeError, ValueError):
                    pass
        return 0

    @staticmethod
    def _extract_output_text(block):
        body = block.get("content")
        if isinstance(body, str):
            return body.strip()
        if not isinstance(body, list):
            return ""
        return "\n".join(
            p["text"] for p in body
            if isinstance(p, dict) and p.get("type") == "output_text"
            and isinstance(p.get("text"), str)).strip()

    @staticmethod
    def _content_to_text(content):
        """Normalize Open WebUI message 'content' (str or block list) to text,
        dropping image parts."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(p["text"] for p in content
                             if isinstance(p, dict)
                             and isinstance(p.get("text"), str))
        return ""

    def render(self, out_dir):
        """Write the session .jsonl. Returns (path, message_count,
        clock_skew), or None if the chat has no messages."""
        if not self.messages:
            return None
        session_id = _session_id(self.chat)
        entries = self.entries()
        subdir = os.path.join(out_dir, encode_cwd_dir(self.cwd))
        os.makedirs(subdir, exist_ok=True)
        # Same naming as Pi: creation time with ':' and '.' replaced by '-'.
        stamp = re.sub(r"[:.]", "-", _iso(self.created_ms))
        path = os.path.join(subdir, f"{stamp}_{session_id}.jsonl")

        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.header(session_id)) + "\n")
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        return path, len(self.messages), self.clock_skew


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", nargs="*",
                    help="Open WebUI JSON export file(s), OR (when --server "
                         "is given) chat ids/prefixes/title fragments.")
    ap.add_argument("-o", "--output-dir",
                    default=os.path.expanduser("~/.pi/agent/sessions"),
                    help="Directory under which session files are written "
                         "(default: Pi's sessions dir).")
    ap.add_argument("-C", "--cwd", default=os.getcwd(),
                    help="Working directory recorded in the sessions; Pi "
                         "lists them when started there (default: the "
                         "current directory).")
    ap.add_argument("--server", metavar="URL",
                    help="Open WebUI base URL (e.g. http://example.com) "
                         "to pull chats from instead of a local file.")
    ap.add_argument("-t", "--token",
                    default=os.environ.get("OPENWEBUI_TOKEN"),
                    help="Bearer token for the server (or $OPENWEBUI_TOKEN).")
    args = ap.parse_args()
    cwd = os.path.abspath(os.path.expanduser(args.cwd))

    if not args.server:
        if not args.export:
            ap.error("at least one export file is required without --server")
        return _convert_files(args.export, args.output_dir, cwd)

    base_url = args.server.rstrip("/")
    if not args.token:
        ap.error("a --token is required when using --server")

    try:
        listing = server_chats(base_url, args.token)
        if not args.export:
            _print_chat_table(listing)
            print(f"\n{len(listing)} chats available on {base_url}")
            return 0
        chat_ids = resolve_chat_ids(listing, args.export)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    results = []
    for chat_id in chat_ids:
        try:
            raw = server_fetch_chat(base_url, chat_id, args.token)
            result = OWUIConverter(raw, cwd).render(args.output_dir)
        except Exception as exc:     # one bad chat must not stop the rest
            print(f"SKIP {chat_id}: {exc}", file=sys.stderr)
            continue
        if result is None:
            print(f"SKIP {chat_id}: no messages to convert", file=sys.stderr)
        else:
            results.append(result)
    return _report(results)


def _print_chat_table(chats):
    """Render a readable table of chat metadata."""
    headers = ("ID", "TITLE", "UPDATED (UTC)", "CREATED (UTC)")
    rows = [(str(c.get("id", "")),
             str(c.get("title") or "(untitled)"),
             _human_ts(_to_ms(c.get("updated_at"))),
             _human_ts(_to_ms(c.get("created_at"))))
            for c in chats]
    widths = [max(len(r[i]) for r in [headers, *rows])
              for i in range(len(headers))]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(x.ljust(w) for x, w in zip(r, widths)))


def _convert_files(paths, out_dir, cwd):
    """Convert one or more local JSON export file(s)."""
    results = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"SKIP {p}: {exc}", file=sys.stderr)
            continue
        for chat in data if isinstance(data, list) else [data]:
            label = chat.get("id", "?") if isinstance(chat, dict) else "?"
            try:
                result = OWUIConverter(chat, cwd).render(out_dir)
            except Exception as exc:
                print(f"SKIP {label}: {exc}", file=sys.stderr)
                continue
            if result is None:
                print(f"SKIP {label}: no messages to convert", file=sys.stderr)
            else:
                results.append(result)
    return _report(results)


def _report(results):
    for path, count, skew in sorted(results, key=lambda r: r[0]):
        if skew is None:
            note = "; timestamps out of order, left as is"
        elif skew:
            shift = timedelta(seconds=round(abs(skew) / 1000))
            note = (f"; assistant timestamps shifted {'-+'[skew < 0]}{shift}"
                    " to undo clock skew")
        else:
            note = ""
        print(f"wrote {path} ({count} messages{note})")
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
