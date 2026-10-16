"""Optional outbound webhook notifications for task status changes and messages.

Disabled unless ONE_WEBHOOK_URL is set. Stdlib only, fire-and-forget:
delivery runs on a daemon thread with a short timeout, never raises into the
caller, and only ever logs to stderr (stdout carries the MCP protocol).

Environment:
  ONE_WEBHOOK_URL          http(s) endpoint; unset/empty -> feature fully disabled
  ONE_WEBHOOK_SECRET       sender key; never logged or included in the payload
  ONE_WEBHOOK_AUTH_HEADER  header name (default "Authorization"); "Bearer <secret>"
                           for Authorization, the raw secret for any other header
  ONE_WEBHOOK_EVENTS       optional comma list: "status", "message", "all", or
                           "status:<new status>" (e.g. "status:completed,status:failed")
"""
from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import __version__

STATUS_CHANGED = "task.status_changed"
MESSAGE = "task.message"
ALL_EVENTS = frozenset({STATUS_CHANGED, MESSAGE})
MAX_TEXT = 2000
TIMEOUT_SECONDS = 3.0
_ALIASES = {
    "status": STATUS_CHANGED, "statuses": STATUS_CHANGED, "status_changed": STATUS_CHANGED,
    "status_change": STATUS_CHANGED, STATUS_CHANGED: STATUS_CHANGED,
    "message": MESSAGE, "messages": MESSAGE, MESSAGE: MESSAGE,
}

_pending: set[threading.Thread] = set()
_pending_lock = threading.Lock()


def _log(text: str) -> None:
    try:
        sys.stderr.write("one-mcp webhook: " + text + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _filters():
    """Return (events, statuses) where statuses=None means every status."""
    raw = os.environ.get("ONE_WEBHOOK_EVENTS", "").strip()
    if not raw:
        return ALL_EVENTS, None
    events, statuses = set(), set()
    for token in (t.strip().lower() for t in raw.split(",")):
        if not token:
            continue
        if token in ("all", "*"):
            events |= ALL_EVENTS
        elif token.startswith(("status:", STATUS_CHANGED + ":")):
            events.add(STATUS_CHANGED)
            statuses.add(token.rsplit(":", 1)[1].strip())
        elif token in _ALIASES:
            events.add(_ALIASES[token])
        else:
            _log(f"ignoring unknown ONE_WEBHOOK_EVENTS entry {token[:40]!r}")
    return frozenset(events), (frozenset(statuses) if statuses else None)


def _config():
    url = os.environ.get("ONE_WEBHOOK_URL", "").strip()
    if not url:
        return None
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        _log("ONE_WEBHOOK_URL must be http(s); webhook disabled")
        return None
    return url


def enabled() -> bool:
    return _config() is not None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_payload(event: str, task: dict, *, old_status=None, new_status=None, message=None) -> dict:
    payload = {
        "event": event,
        "source": "one-mcp",
        "version": __version__,
        "timestamp": _utc_now(),
        "task_id": task.get("id"),
        "title": task.get("title"),
        "assignee": task.get("assignee"),
        "owner": task.get("owner"),
        "status": task.get("status"),
    }
    if event == STATUS_CHANGED:
        payload["old_status"] = old_status
        payload["new_status"] = new_status
    if message is not None:
        text = message.get("body") or ""
        payload["message"] = {
            "id": message.get("id"),
            "sender": message.get("sender"),
            "recipient": message.get("recipient"),
            "text": text[:MAX_TEXT],
            "truncated": len(text) > MAX_TEXT,
        }
    return payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Never forward the sender key to a redirect target.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _deliver(url: str, body: bytes, event: str) -> None:
    try:
        headers = {"Content-Type": "application/json", "User-Agent": f"one-mcp/{__version__}",
                   "X-ONE-Event": event}
        secret = os.environ.get("ONE_WEBHOOK_SECRET", "")
        if secret:
            name = os.environ.get("ONE_WEBHOOK_AUTH_HEADER", "").strip() or "Authorization"
            headers[name] = f"Bearer {secret}" if name.lower() == "authorization" else secret
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with _opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            response.read(4096)
    except urllib.error.HTTPError as exc:
        _log(f"delivery of {event} failed: HTTP {exc.code}")
    except Exception as exc:  # never propagate; never include URL or secret
        _log(f"delivery of {event} failed: {type(exc).__name__}")
    finally:
        with _pending_lock:
            _pending.discard(threading.current_thread())


def notify(event: str, task: dict | None, **fields):
    """Queue one delivery for an already-committed write. Never raises."""
    try:
        url = _config()
        if url is None or not task:
            return None
        events, statuses = _filters()
        if event not in events:
            return None
        if event == STATUS_CHANGED and statuses is not None and fields.get("new_status") not in statuses:
            return None
        body = json.dumps(build_payload(event, task, **fields), ensure_ascii=False,
                          default=str).encode("utf-8")
        thread = threading.Thread(target=_deliver, args=(url, body, event),
                                  name="one-webhook", daemon=True)
        with _pending_lock:
            _pending.add(thread)
        thread.start()
        return thread
    except Exception as exc:
        _log(f"could not queue {event}: {type(exc).__name__}")
        return None


def flush(timeout: float = TIMEOUT_SECONDS + 0.5) -> None:
    """Wait (bounded) for in-flight deliveries; lets short-lived CLI runs deliver."""
    deadline = time.monotonic() + timeout
    with _pending_lock:
        threads = list(_pending)
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(remaining)


atexit.register(flush)
