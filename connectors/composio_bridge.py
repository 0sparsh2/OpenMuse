"""
App connectors via Composio (issue #7).

Composio hosts OAuth and maintained toolkits. We keep identity, policy and
presentation on our side:

- Identity: Composio's `user_id` is OUR user id (usr_…), so every person's
  connected Google account is theirs alone. Tools execute with the calling
  run's user id; there is no shared/default account.
- Tools: a curated subset per toolkit is bridged into our ToolRegistry as a
  normal namespace (GMAIL_FETCH_EMAILS -> gmail.fetch_emails). Schemas are
  trimmed (long descriptions/examples dropped) to keep model context small.
- Policy: each bridged tool has an explicit risk class in
  policies/tool-capabilities.yaml. Reads are R1; reversible drafts/labels
  are R2; anything that sends, creates, changes or deletes on the user's
  behalf is R3 external_write and ALWAYS asks, even with autonomy on.
- Results: returned to the model as untrusted data (the turn engine labels
  every tool result); a small `display` card (email / event) goes to the UI.
- Secrets: only COMPOSIO_API_KEY lives here. OAuth tokens stay at Composio
  and never enter prompts, logs, memory or our database.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from tools.registry import ToolDefinition, ToolRegistry

# toolkit -> presentation + curated tools {slug: risk}
TOOLKITS: dict[str, dict[str, Any]] = {
    "gmail": {
        "slug": "GMAIL", "name": "Gmail", "namespace": "gmail",
        "description": "Search and read your email, draft replies, and send with your OK.",
        "can": ["Search and read email threads", "Draft replies", "Send or reply — only after you approve"],
        "tools": {
            "GMAIL_FETCH_EMAILS": "R1",
            "GMAIL_FETCH_MESSAGE_BY_THREAD_ID": "R1",
            "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID": "R1",
            "GMAIL_LIST_LABELS": "R1",
            "GMAIL_GET_PROFILE": "R1",
            "GMAIL_CREATE_EMAIL_DRAFT": "R2",
            "GMAIL_ADD_LABEL_TO_EMAIL": "R2",
            "GMAIL_SEND_EMAIL": "R3",
            "GMAIL_REPLY_TO_THREAD": "R3",
            "GMAIL_SEND_DRAFT": "R3",
            "GMAIL_FORWARD_MESSAGE": "R3",
        },
        "defaults": {"GMAIL_FETCH_EMAILS": {"max_results": 5, "verbose": False, "include_payload": False}},
    },
    "googlecalendar": {
        "slug": "GOOGLECALENDAR", "name": "Google Calendar", "namespace": "calendar",
        "description": "See your agenda, find free time, and add or change events with your OK.",
        "can": ["Read your agenda and find free time", "Create, move or delete events — only after you approve"],
        "tools": {
            "GOOGLECALENDAR_EVENTS_LIST": "R1",
            "GOOGLECALENDAR_FIND_EVENT": "R1",
            "GOOGLECALENDAR_FIND_FREE_SLOTS": "R1",
            "GOOGLECALENDAR_LIST_CALENDARS": "R1",
            "GOOGLECALENDAR_CREATE_EVENT": "R3",
            "GOOGLECALENDAR_QUICK_ADD": "R3",
            "GOOGLECALENDAR_UPDATE_EVENT": "R3",
            "GOOGLECALENDAR_DELETE_EVENT": "R3",
        },
        "defaults": {"GOOGLECALENDAR_EVENTS_LIST": {"maxResults": 15, "singleEvents": True,
                                                    "orderBy": "startTime"}},
    },
}
# Listed in the Apps page as "coming soon" (issue #10).
UPCOMING = [("googledrive", "Google Drive"), ("outlook", "Outlook"), ("slack", "Slack"),
            ("notion", "Notion"), ("todoist", "Todoist"), ("spotify", "Spotify")]

SIDE_EFFECT = {"R1": "none", "R2": "external_write", "R3": "external_write"}


def tool_name(namespace: str, slug: str) -> str:
    prefix = TOOLKITS_BY_NS[namespace]["slug"] + "_"
    return namespace + "." + slug[len(prefix):].lower()


TOOLKITS_BY_NS = {v["namespace"]: v for v in TOOLKITS.values()}


def policy_entries() -> dict[str, dict]:
    """Risk table for every bridged tool (mirrored in tool-capabilities.yaml)."""
    out = {}
    for tk in TOOLKITS.values():
        for slug, risk in tk["tools"].items():
            out[tool_name(tk["namespace"], slug)] = {
                "risk": risk, "capabilities": [f"connector.{tk['namespace']}"],
                "side_effect": SIDE_EFFECT[risk]}
    return out


def _trim_schema(schema: dict, max_desc: int = 280) -> dict:
    """Drop presentation-only keys and cap descriptions (context budget)."""
    drop = {"examples", "human_parameter_name", "human_parameter_description", "title", "file_uploadable"}
    if isinstance(schema, dict):
        out = {}
        for k, v in schema.items():
            if k in drop:
                continue
            if k == "description" and isinstance(v, str):
                out[k] = v[:max_desc]
            else:
                out[k] = _trim_schema(v, max_desc)
        return out
    if isinstance(schema, list):
        return [_trim_schema(x, max_desc) for x in schema]
    return schema


class NotConfigured(RuntimeError):
    pass


class ComposioBridge:
    def __init__(self, api_key: str, *, cache_dir: str, public_url: str = "http://127.0.0.1:8080",
                 client=None, log=lambda m: None):
        if not api_key and client is None:
            raise NotConfigured("COMPOSIO_API_KEY is not set")
        self._client = client
        self._api_key = api_key
        self.cache_dir = cache_dir
        self.public_url = public_url.rstrip("/")
        self.log = log
        self._lock = threading.Lock()
        self._status: dict[str, tuple[float, dict]] = {}   # user_id -> (at, {toolkit: account})
        os.makedirs(cache_dir, exist_ok=True)

    @property
    def client(self):
        if self._client is None:
            from composio import Composio
            self._client = Composio(api_key=self._api_key)
        return self._client

    # -- tool schemas (cached on disk; fetched once) -----------------------------
    def _schemas(self, toolkit: str) -> dict[str, dict]:
        path = os.path.join(self.cache_dir, f"composio_{toolkit}.json")
        try:
            with open(path, encoding="utf-8") as fh:
                cached = json.load(fh)
            if time.time() - cached.get("at", 0) < 7 * 86400:
                return cached["tools"]
        except (OSError, ValueError, KeyError):
            pass
        tk = TOOLKITS[toolkit]
        tools = {}
        for t in self.client.tools.get_raw_composio_tools(tools=list(tk["tools"])):
            tools[t.slug] = {"description": (t.description or t.name or "")[:600],
                             "input_schema": _trim_schema(t.input_parameters or {"type": "object"})}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"at": time.time(), "tools": tools}, fh)
        return tools

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        names = []
        for key, tk in TOOLKITS.items():
            try:
                schemas = self._schemas(key)
            except Exception as exc:
                self.log(f"composio: couldn't load {key} tools: {exc}")
                continue
            registry.register_namespace(tk["namespace"], f"{tk['name']} (connected per user). {tk['description']}")
            for slug, risk in tk["tools"].items():
                spec = schemas.get(slug)
                if spec is None:
                    continue
                name = tool_name(tk["namespace"], slug)
                schema = dict(spec["input_schema"] or {"type": "object"})
                schema.setdefault("type", "object")
                schema.setdefault("properties", {})
                schema.get("properties", {}).pop("user_id", None)  # identity is ours, never the model's
                if isinstance(schema.get("required"), list):
                    schema["required"] = [r for r in schema["required"] if r != "user_id"]
                verb = "Asks the user first. " if risk == "R3" else ""
                registry.register(ToolDefinition(
                    name=name, version="1.0.0",
                    description=(verb + spec["description"])[:700],
                    input_schema=schema, output_schema={"type": "object"},
                    capabilities=[f"connector.{tk['namespace']}"],
                    side_effect=SIDE_EFFECT[risk],
                    idempotency="pure" if risk == "R1" else "unsafe_retry",
                    default_timeout_ms=60_000,
                    execute=self._executor(key, slug),
                    display=_display_for(key, slug),
                ))
                names.append(name)
        return names

    def _executor(self, toolkit: str, slug: str):
        defaults = TOOLKITS[toolkit].get("defaults", {}).get(slug, {})
        app = TOOLKITS[toolkit]["name"]

        def execute(ctx, args):
            uid = getattr(ctx, "user_id", "") or "user_api"
            arguments = {**defaults, **{k: v for k, v in args.items() if k != "user_id"}}
            try:
                resp = self.client.tools.execute(slug, arguments=arguments, user_id=uid,
                                                 dangerously_skip_version_check=True)
            except Exception as exc:
                msg = str(exc)
                if "No connected account" in msg or "404" in msg[:40]:
                    return {"successful": False, "not_connected": True,
                            "error": f"{app} isn't connected for this user. Ask them to connect it "
                                     f"in Apps (bottom bar), then try again."}
                return {"successful": False, "error": f"{app} request failed: {msg[:300]}"}
            data = resp if isinstance(resp, dict) else getattr(resp, "model_dump", lambda: {})()
            if not data.get("successful", True):
                return {"successful": False, "error": str(data.get("error") or "request failed")[:500]}
            return {"successful": True, "data": data.get("data")}
        return execute

    # -- connections -------------------------------------------------------------------
    def connections(self, user_id: str, *, fresh: bool = False) -> dict[str, dict]:
        """{toolkit: {"id", "status"}} for ACTIVE connections (cached 30s)."""
        hit = self._status.get(user_id)
        if hit and not fresh and time.time() - hit[0] < 30:
            return hit[1]
        out: dict[str, dict] = {}
        try:
            res = self.client.connected_accounts.list(user_ids=[user_id], statuses=["ACTIVE"],
                                                      toolkit_slugs=list(TOOLKITS))
            for acc in getattr(res, "items", []) or []:
                slug = getattr(getattr(acc, "toolkit", None), "slug", "") or ""
                if slug in TOOLKITS:
                    out[slug] = {"id": acc.id, "status": acc.status}
        except Exception as exc:
            self.log(f"composio: list connections failed: {exc}")
            return hit[1] if hit else {}
        self._status[user_id] = (time.time(), out)
        return out

    def connected_namespaces(self, user_id: str) -> set[str]:
        return {TOOLKITS[k]["namespace"] for k in self.connections(user_id)}

    def catalog(self, user_id: str) -> list[dict]:
        conns = self.connections(user_id, fresh=True)
        apps = [{"toolkit": k, "name": v["name"], "description": v["description"], "can": v["can"],
                 "namespace": v["namespace"], "connected": k in conns, "available": True}
                for k, v in TOOLKITS.items()]
        apps += [{"toolkit": k, "name": n, "description": "", "can": [],
                  "connected": False, "available": False} for k, n in UPCOMING]
        return apps

    def connect(self, user_id: str, toolkit: str, callback_url: str = "") -> dict:
        if toolkit not in TOOLKITS:
            raise ValueError(f"unknown app {toolkit!r}")
        auth_config_id = self.client.toolkits._get_auth_config_id(toolkit=toolkit)
        # Composio Connect Link (the hosted flow for Composio-managed OAuth).
        req = self.client.connected_accounts.link(
            user_id=user_id, auth_config_id=auth_config_id,
            callback_url=callback_url or f"{self.public_url}/?apps=connected")
        self._status.pop(user_id, None)
        return {"redirect_url": req.redirect_url, "connection_id": getattr(req, "id", "")}

    def disconnect(self, user_id: str, toolkit: str) -> int:
        n = 0
        res = self.client.connected_accounts.list(user_ids=[user_id], toolkit_slugs=[toolkit])
        for acc in getattr(res, "items", []) or []:
            self.client.connected_accounts.delete(acc.id)
            n += 1
        self._status.pop(user_id, None)
        return n


# -- result cards ------------------------------------------------------------------------
def _display_for(toolkit: str, slug: str):
    if slug in ("GMAIL_FETCH_EMAILS", "GMAIL_FETCH_MESSAGE_BY_THREAD_ID", "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"):
        return _email_card
    if slug in ("GOOGLECALENDAR_EVENTS_LIST", "GOOGLECALENDAR_FIND_EVENT",
                "GOOGLECALENDAR_CREATE_EVENT", "GOOGLECALENDAR_UPDATE_EVENT", "GOOGLECALENDAR_QUICK_ADD"):
        return _event_card
    return None


def _email_card(out: dict) -> dict | None:
    data = (out or {}).get("data") or {}
    msgs = data.get("messages") if isinstance(data, dict) else None
    if not msgs and isinstance(data, dict) and data.get("messageId"):
        msgs = [data]
    if not msgs:
        return None
    m = msgs[0]
    sender = str(m.get("sender") or "")
    name = re.sub(r"\s*<[^>]+>", "", sender).strip().strip('"') or sender
    text = str(m.get("messageText") or (m.get("preview") or {}).get("body") or "")
    return {"type": "email", "from": name[:80], "subject": str(m.get("subject") or "")[:160],
            "snippet": re.sub(r"\s+", " ", text).strip()[:240], "count": len(msgs),
            "url": m.get("display_url") or (f"https://mail.google.com/mail/u/0/#all/{m['threadId']}"
                                            if m.get("threadId") else "")}


def _event_card(out: dict) -> dict | None:
    data = (out or {}).get("data") or {}
    items = []
    if isinstance(data, dict):
        items = data.get("items") or data.get("events") or ([data] if data.get("summary") else [])
        if not items and isinstance(data.get("response_data"), dict):
            rd = data["response_data"]
            items = rd.get("items") or ([rd] if rd.get("summary") else [])
    if not items:
        return None
    e = items[0]
    start = (e.get("start") or {})
    when = start.get("dateTime") or start.get("date") or ""
    return {"type": "event", "title": str(e.get("summary") or "(no title)")[:120],
            "when": _pretty_when(when), "location": str(e.get("location") or "")[:80],
            "count": len(items), "url": e.get("htmlLink") or ""}


def _pretty_when(iso: str) -> str:
    if not iso:
        return ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%a %b %-d, %-I:%M %p") if "T" in iso else dt.strftime("%a %b %-d (all day)")
    except ValueError:
        return iso
