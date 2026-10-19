#!/usr/bin/env python3

# AI Disclosure:
# - Authorship: AI-generated
# - Models: frob/deepseek-v4-flash-0731:284b-a13b-ud-q8_k_xl
# - Scope: Generated from a specification and iterative feedback in an agentic
#   session. The model reverse-engineered the Pi session JSONL schema, converted
#   Open WebUI chat exports (and later live server chats) into that format,
#   preserved conversation trees and synthesized model_change nodes.
"""Convert an Open WebUI JSON export or a server-hosted chat to Pi sessions.

Pi reads its sessions as JSONL files where each line is a tree entry with an
``id`` and ``parentId`` (see docs/session-format.md in the pi install). The
active branch ("leaf") is simply the last entry in file order, so preserving the
conversation trees means writing entries in topological order such that every
child follows its parent and, for any parent with several children, the one on
Open WebUI's *current* path comes last.

Open WebUI exports an array of chats. Each chat has a ``history`` whose messages
are keyed by id and linked via ``parentId``/``childrenIds``;
``history.currentId`` marks the active node (a leaf). Assistant answers live in
each message's ``output`` list as one block of type "message" (the rendered
text) plus an optional "reasoning" block. All other Open WebUI metadata is
dropped.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

_HEX_RE = re.compile(r"[0-9a-fA-F]{7,}")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _short_id(raw):
    """Shorten an Open WebUI id to a stable 8-char hex Pi entry id."""
    if isinstance(raw, str) and len(raw) >= 7:
        # Prefer the printable prefix when it is already short-ish/hex.
        return re.sub(r"[^0-9a-fA-F]", "", raw)[:8] or _sha_hex(str(raw))
    return _sha_hex(str(raw))


def _sha_hex(s):
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()
    # Make it vary from other derived ids (bit-borrow a byte).
    out = list(h[1:9])
    first = ord(h[0]) % 16
    out[-1] = "%x" % ((int(out[-1], 16) + first) % 15)
    return "".join(out)


def encode_cwd_dir(cwd):
    """Mirror Pi's session-directory encoding: drop the leading '/', turn
    /,: and backslash into '-', then wrap in '--...--'."""
    safe = cwd.replace("/", "-").replace(chr(92), "-").replace(":", "-")
    safe = safe.lstrip("-\"") or ""
    return f"--{safe}--"


def _session_id(chat):
    raw = str(chat.get("id") or hashlib.sha256(
        json.dumps(chat, sort_keys=True).encode()).hexdigest())
    if _UUID_RE.fullmatch(raw):
        return raw
    digest = bytearray(hashlib.md5(raw.encode()).digest())
    digest[6] = (digest[6] & 0x0F) | 0x40   # version 4
    digest[8] = (digest[8] & 0x3F) | 0x80   # variant
    h = bytes(digest).hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _iso(ms):
    """Unix milliseconds -> ISO 8601 timestamp (or now if unset/invalid)."""
    try:
        f = float(ms)
    except (TypeError, ValueError):
        f = None
    if not isinstance(f, (int, float)) or f <= 0:
        return datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    dt = datetime.fromtimestamp(f / 1000.0, tz=timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{int((f % 1) * 999):03d}Z"


def _human_ts(ms):
    """Short human-readable UTC time for the listing table."""
    try:
        f = float(ms)
    except (TypeError, ValueError):
        return "-"
    if not isinstance(f, (int, float)) or f <= 0:
        return "-"
    return datetime.fromtimestamp(f / 1000.0,
                                  tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def fetch_json(url, token=None):
    """GET a JSON resource with an optional bearer token."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body[:300]}") \
            from None


def server_chats(base_url, token=None):
    """List available chats (metadata only) from an Open WebUI server."""
    url = base_url.rstrip("/") + "/api/v1/chats/"
    data = fetch_json(url, token)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected chats response: {type(data).__name__}")
    return data


def server_fetch_chat(base_url, chat_id, token=None):
    """Fetch the full JSON for one chat."""
    url = f"{base_url.rstrip('/')}/api/v1/chats/{chat_id}"
    return fetch_json(url, token)


def resolve_chat_ids(base_url, tokens, token=None):
    """Turn user-supplied chat ids (or unique id prefixes) into full chats."""
    listing = server_chats(base_url, token)
    by_full = {c.get("id"): c for c in listing}
    matches = []
    for tkn in tokens:
        if tkn in by_full:
            matches.append(by_full[tkn])
            continue
        prefix_matches = [c for c in listing
                          if str(c.get("id", "")).startswith(tkn)]
        title_matches = [c for c in listing if tkn.lower() in
                         (str(c.get("title")) or "").lower()]
        cands = prefix_matches or title_matches
        if len(cands) == 1:
            matches.append(cands[0])
        elif len(cands) > 1:
            raise RuntimeError(f"'{tkn}' is ambiguous ({len(cands)} chats); "
                               f"use a full id")
        else:
            raise RuntimeError(f"no chat found for '{tkn}'")
    return matches



def _filename_ts(ms):
    """ISO-ish timestamp with colons replaced for safe filenames."""
    iso = _iso(ms)
    return (iso.replace(":", "-")
                .replace(".", "-", 1))   # only the ms separator


def _title_for_name(title):
    cleaned = re.sub(r"[^\w\- ]+", "", title or "").strip().replace(" ", "_")
    if not cleaned:
        return None
    suffix = "…" if len(cleaned) > 48 else ""
    return cleaned[:48] + suffix


def _to_ms(value):
    """Normalize an Open WebUI epoch to Pi's Unix-milliseconds convention.

    Exports can store timestamps in seconds (~1.7e9, 10 digits) or milliseconds
    (~1.7e12). Treat anything well below a plausible millisecond value as
    seconds so both load correctly; otherwise keep it as ms (and floor the
    fraction to an int)."""
    f = float(value)
    if abs(f) < 3_000_000_000:               # looks like epoch seconds
        return int(round(f * 1000))
    return int(f)


def _message_ms(m):
    ts = m.get("timestamp")
    if isinstance(ts, dict):                 # Open WebUI can nest it
        ts = ts.get("start", ts.get("createdAt"))
    try:
        f = float(ts)
    except (TypeError, ValueError):
        return None
    return _to_ms(f)


class OWUIConverter:
    @staticmethod
    def _extract_chat(chat):
        """Normalize a chat record. Exports and the server both wrap each chat
        under {'id','user_id','title',...,'chat':{...history...}}. Accept either
        that wrapper or a bare chat dict with 'history' directly."""
        if not isinstance(chat, dict):
            raise TypeError(f"expected a chat object, got {type(chat).__name__}")
        out = dict(chat)
        inner = chat.get("chat")
        if "history" not in chat and isinstance(inner, dict):
            merged = dict(inner)
            for k, v in chat.items():
                merged.setdefault(k, v)     # keep top-level title/id
            out = merged
        return out

    def __init__(self, chat):
        self.chat = OWUIConverter._extract_chat(chat)
        history = self.chat.get("history") or {}
        self.messages = history.get("messages") or {}
        self.id_map = {mid: _short_id(mid) for mid in self.messages}
        # children per parent id; the *active* child is pushed last.
        self._children = {}
        seen_roots = []
        cur_path_ids = set()
        node = history.get("currentId")
        while node and node in self.messages:
            cur_path_ids.add(node)
            node = self.messages[node].get("parentId")
        for mid, m in self.messages.items():
            p = m.get("parentId")
            if p:
                self._children.setdefault(p, []).append(mid)
            else:
                seen_roots.append(mid)

        # Choose the root: prefer currentId's ancestor; otherwise single root.
        roots = list(seen_roots) or [None]
        cur_id = history.get("currentId")
        if len(roots) > 1 and cur_id in self.messages:
            while self.messages[cur_id].get("parentId"):
                cur_id = self.messages[cur_id]["parentId"]
            roots = [cur_id] if cur_id in self.messages else roots
        self.root = roots[0]

        for parent, kids in self._children.items():
            on_active = [k for k in kids if k in cur_path_ids]
            active = on_active[-1] if on_active else None
            ordered = list(kids)
            if active is not None and active != ordered[-1]:
                ordered.remove(active)
                ordered.append(active)       # current child last => wins leaf order
            self._children[parent] = ordered
        # Flat message id order (root -> leaves, parent before child). Kept so we
        # can insert synthetic model_change nodes between runs of the same model.
        self._ordered_mids = []
        stack = [self.root]
        while stack:
            mid = stack.pop()
            if mid is None or mid not in self.messages:
                continue
            self._ordered_mids.append(mid)
            for child in reversed(self._children.get(mid, [])):
                stack.append(child)
        # The model selected when the conversation started (Open WebUI chat-level).
        models = self.chat.get("models")
        if isinstance(models, list):
            models = models[0] if models else None
        self._initial_model = models or None
        # Where synthetic model_change nodes go: before the first assistant message
        # produced under each newly-seen model on the active path. Each maps a mid
        # to (provider, modelId).
        self._switch_points = self._compute_switches()

    def _active_path(self):
        """Message ids along Open WebUI's current path, root -> leaf."""
        node = self.chat.get("history", {}).get("currentId")
        up = []
        while node and node in self.messages:
            up.append(node)
            node = self.messages[node].get("parentId") or None
        return list(reversed(up))

    def _assistant_model(self, mid):
        m = self.messages[mid]
        if m.get("role") != "assistant":
            return None
        model = (m.get("model") or m.get("modelName")
                 or self._initial_model) or None
        if isinstance(model, list):
            model = model[0] if model else None
        return str(model) if model else None

    def _compute_switches(self):
        """Return {mid: (provider, modelId)} for each point where the selected
        model changes on the active path. Timestamps are fabricated later from the
        first message produced under that new model."""
        switches = {}
        prev_model = self._initial_model
        started_with = False      # did we emit an opening switch already?
        for mid in self._active_path():
            model = self._assistant_model(mid)
            if model is None:
                continue
            if not started_with or (prev_model != model):
                switches[mid] = ("open-webui-import", model)
                prev_model = model
                started_with = True
        return switches

    def entries(self):
        """Tree entries, in a valid parent-before-child order whose final entry is
        Open WebUI's currentId (Pi treats the last written node as its leaf).

        model_change nodes are threaded exactly like Pi's appendModelChange(): when
        an assistant message announces a newly-seen model, an mc node becomes that
        message's parent, so it lies on every descendant chain and is picked up by
        getSessionContextSettings(). The announcing message keeps its own id (so all
        original descendants still link to it) and the conversation tree itself.
        """
        sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
        on_path = set(self._active_path())
        out = []

        def visit(mid):
            m = self.messages[mid]
            msg_id = self.id_map[mid]
            real_parent = (None if not m.get("parentId")
                           else self.id_map[m["parentId"]])
            # Thread a model_change node just above the announcing message.
            if mid in self._switch_points:
                provider, model = self._switch_points[mid]
                out.append({
                    "type": "model_change",
                    "id": f"mc_{msg_id}",
                    "parentId": real_parent,
                    "timestamp": _iso(_message_ms(m)),
                    "provider": provider,
                    "modelId": model,
                })
            out.append({
                "type": "message",
                "id": msg_id,
                "parentId": (f"mc_{msg_id}" if mid in self._switch_points
                              else real_parent),
                "timestamp": _iso(_message_ms(m)),
                "message": self._to_pi_message(m, mid),
            })
            # Visit all off-path subtrees first; descend the active path last so
            # currentId (the final node) is written last and becomes Pi's leaf.
            kids = list(self._children.get(mid, []))
            others = [k for k in kids if k not in on_path]
            continuation = [k for k in kids if k in on_path]
            for k in others:
                visit(k)
            for k in continuation:
                visit(k)

        if self.root is not None and self.root in self.messages:
            visit(self.root)
        return out

    def header(self, session_id):
        ts = None
        if self.messages:
            tss = [_message_ms(m) for m in self.messages.values()]
            tss = [t for t in tss if t is not None]
            if tss:
                # Use the newest timestamp: Pi orders sessions by activity.
                ts = max(tss)
        return {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": _iso(ts),
            "cwd": self.chat.get("path") or ".",
        }

    def _to_pi_message(self, m, entry_id):
        role = m.get("role", "user")
        pi_msg = {"role": role}
        ts_ms = _message_ms(m)

        if role == "assistant":
            blocks = []
            for block in m.get("output") or []:
                if not isinstance(block, dict):
                    continue
                text = self._extract_output_text(block)
                if not text:
                    continue
                btype = block.get("type")
                if btype == "reasoning":
                    blocks.append({"type": "thinking", "thinking": text})
                else:                            # "message" or anything else -> answer
                    blocks.append({"type": "text", "text": text})

            if not any(b["type"] == "text" for b in blocks):
                fallback = self._content_to_text(m.get("content"))
                if fallback:
                    blocks.append({"type": "text", "text": fallback})
            # An assistant turn with no recorded output must still carry a
            # non-empty content array for Pi; keep the tree node and mark it.
            if not blocks:
                blocks.append({"type": "text",
                               "text": "(no response recorded)"})
            pi_msg["content"] = blocks

            model_name = (m.get("model") or m.get("modelName")
                          or None) or self.chat.get("models")
            if isinstance(model_name, list):
                model_name = model_name[0] if model_name else None
            if model_name:
                pi_msg["api"] = "openai-completions"
                pi_msg["provider"] = "open-webui-import"
                pi_msg["model"] = str(model_name)
            # Pi's AssistantMessage requires a usage object.
            u = m.get("usage") or {}
            tokens = 0
            if isinstance(u, dict):
                raw_total = (u.get("total_tokens")
                             or (m.get("info") or {}).get("total_tokens")
                             or 0)
                try:
                    tokens = int(raw_total)
                except (TypeError, ValueError):
                    tokens = 0
            pi_msg["usage"] = {
                "input": self._u_int(u, ["prompt_tokens", "input_tokens"], m),
                "output": self._u_int(
                    u, ["completion_tokens", "output_tokens"], m),
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": tokens,
                "cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0,
                         "cacheWrite": 0.0, "total": 0.0},
            }
            # The export holds only finished assistant turns; nothing is a tool
            # call in this data set (no output_text + tool payloads), so stop.
            pi_msg["stopReason"] = "stop"
        else:                                    # user / unknown role -> text
            pi_msg["content"] = self._content_to_text(m.get("content"))

        if ts_ms is not None:
            pi_msg["timestamp"] = ts_ms
        return pi_msg

    @staticmethod
    def _u_int(u, keys, m):
        for k in keys:
            v = u.get(k)
            if v is None and isinstance(m.get("info"), dict):
                v = m["info"].get(k)
            try:
                return int(v) if v else 0
            except (TypeError, ValueError):
                pass
        return 0

    @staticmethod
    def _extract_output_text(block):
        body = block.get("content")
        if isinstance(body, str):
            return body.strip()
        parts = []
        for part in (body or []) if isinstance(body, list) else []:
            if isinstance(part, dict) and \
                    part.get("type") == "output_text":
                t = part.get("text")
                if t:
                    parts.append(t)
        return "\n".join(parts).strip()

    def _content_to_text(self, content):
        """Normalize Open WebUI message 'content' (str or block list) to text."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                t = p.get("text")
                if not t and "data" in p:       # an image reference; drop it
                    continue
                if t:
                    parts.append(t)
            return "\n".join(parts)
        return str(content) if content else ""

    def render(self, out_dir):
        """Write the session .jsonl. Returns (path, message_count)."""
        session_id = _session_id(self.chat)
        entries = self.entries()
        count = len(entries)
        if not count:
            return None

        ts_ms = max((_message_ms(m) for m in self.messages.values()
                     if _message_ms(m) is not None), default=0)
        cwd = (self.chat.get("path"))
        subdir = os.path.join(out_dir, encode_cwd_dir(cwd or "imported"))
        os.makedirs(subdir, exist_ok=True)
        base = f"{_filename_ts(ts_ms)}_{session_id}.jsonl"
        path = os.path.join(subdir, base)

        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.header(session_id)) + "\n")
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        return path, count


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", nargs="?",
                    help="Path to an Open WebUI JSON export file, OR (when "
                         "--server is given) a chat id/prefix/title fragment.")
    ap.add_argument("-o", "--output-dir",
                    default=os.path.expanduser("~/.pi/agent/sessions"),
                    help="Directory under which session files are written "
                         "(default: Pi's sessions dir).")
    ap.add_argument("--server", metavar="URL",
                    help="Open WebUI base URL (e.g. http://example.com) to "
                         "pull chats from instead of a local file.")
    ap.add_argument("-t", "--token",
                    default=os.environ.get("OPENWEBUI_TOKEN"),
                    help="Bearer token for the server (or $OPENWEBUI_TOKEN).")
    args = ap.parse_args()

    if not args.server:
        return _convert_files(args.export, args.output_dir)

    base_url = args.server.rstrip("/")
    if not args.token:
        print("error: a --token is required when using --server", file=sys.stderr)
        return 2

    # No chat specified -> just list what's available.
    listing = server_chats(base_url, args.token)
    if not args.export:
        _print_chat_table(listing)
        print(f"\n{len(listing)} chats available on {base_url}")
        return 0

    matches = resolve_chat_ids(base_url, [args.export], args.token)
    results = []
    for meta in matches:
        chat_id = meta["id"]
        try:
            raw = server_fetch_chat(base_url, chat_id, args.token)
            result = OWUIConverter(raw).render(args.output_dir)
            if result is not None:
                results.append(result)
            else:
                print(f"SKIP {chat_id}: no messages to convert")
        except Exception as exc:
            print(f"SKIP {chat_id}: {exc}", file=sys.stderr)

    for path, count in sorted(results):
        print(f"wrote {path} ({count} messages)")
    return 0 if results else 1


def _print_chat_table(chats):
    """Render a readable table of chat metadata."""
    rows = []
    for c in chats or []:
        updated = c.get("updated_at")
        created = c.get("created_at")
        rows.append({
            "ID": str(c.get("id", "")),
            "TITLE": str(c.get("title") or "(untitled)"),
            "UPDATED (UTC)": _human_ts(_to_ms_or_none(updated)),
            "CREATED (UTC)": _human_ts(_to_ms_or_none(created)),
        })
    # Column widths
    widths = {}
    for r in rows:
        for k, v in r.items():
            widths[k] = max(widths.get(k, len(k)), len(v))
    def line(cells):
        headers = list(rows[0].keys()) if rows else []
        return "  ".join(
            str(x).ljust(widths[h]) for h, x in zip(headers, cells))

    headers = list(rows[0].keys()) if rows else []
    print(line(headers) if headers else "")
    print("  ".join("-" * widths[k] for k in headers) if headers else "")
    for r in rows:
        print(line(r.values()))


def _to_ms_or_none(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(f, (int, float)) or f <= 0:
        return None
    # seconds vs ms heuristic
    if abs(f) < 3_000_000_000:
        return int(round(f * 1000))
    return int(f)


def _convert_files(pathspec, out_dir):
    """Convert one or more local JSON export file(s)."""
    paths = [pathspec] if not isinstance(pathspec, list) else pathspec
    results = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        chats = data if isinstance(data, list) else [data]
        for chat in chats or []:
            try:
                result = OWUIConverter(chat).render(out_dir)
                if result is not None:
                    results.append(result)
                elif (chat.get("history", {}).get("messages")):
                    print(f"SKIP {_session_id(chat)}: no messages to convert")
            except Exception as exc:
                print(f"SKIP {chat.get('id', '?')}: {exc!r}")

    if not results:
        return 1
    for path, count in sorted(results):
        print(f"wrote {path} ({count} messages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
