"""ONE local operator CLI. Approvals and worker runs are never exposed via MCP."""
from __future__ import annotations

import argparse
import json
import sys

from .store import Store
from .worker import execute_one, doctor
from .connections import emit


def main(argv=None):
    parser = argparse.ArgumentParser(prog="one", description="Local development coordination")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="Create a queued task; does not execute it")
    create.add_argument("title"); create.add_argument("goal"); create.add_argument("assignee")
    create.add_argument("--owner", default="grok-bot")
    create.add_argument("--base-commit", default="")
    create.add_argument("--allowed-path", action="append", default=[])
    create.add_argument("--acceptance", action="append", default=[])
    create.add_argument("--budget", type=int, default=0)
    create.add_argument("--request-key")
    listing = sub.add_parser("list")
    listing.add_argument("--assignee"); listing.add_argument("--status")
    get = sub.add_parser("get"); get.add_argument("task_id")
    approvals = sub.add_parser("approve"); approvals.add_argument("task_id")
    reject = sub.add_parser("reject"); reject.add_argument("task_id")
    cancel = sub.add_parser("cancel"); cancel.add_argument("task_id")
    message = sub.add_parser("message")
    for name in ("task_id", "sender", "recipient", "body"):
        message.add_argument(name)
    inbox = sub.add_parser("inbox"); inbox.add_argument("recipient")
    ack = sub.add_parser("ack"); ack.add_argument("message_id"); ack.add_argument("recipient")
    artifacts = sub.add_parser("artifacts"); artifacts.add_argument("task_id")
    events = sub.add_parser("events"); events.add_argument("task_id")
    recover = sub.add_parser("recover", help="Block expired worker leases (never auto-retry)")
    worker = sub.add_parser("worker", help="Run at most one approved CLI task")
    worker.add_argument("agent", choices=["claude", "codex", "grok-cli"])
    worker.add_argument("--config", required=True)
    worker.add_argument("--execute", action="store_true", help="Explicit operator execution; default dry-run")
    connection = sub.add_parser("connections", help="Print safe local MCP configuration snippets")
    connection.add_argument("--output-dir", help="Write new snippet files here, never overwrite")
    check = sub.add_parser("doctor")
    check.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "connections":
            result = emit(args.output_dir)
        elif args.command == "doctor":
            result = doctor(args.config)
        else:
            with Store() as store:
                if args.command == "create":
                    result = store.create_task(args.title, args.goal, args.assignee, args.owner,
                                               args.base_commit, args.allowed_path, args.acceptance,
                                               args.budget, args.request_key)
                elif args.command == "list": result = store.list_tasks(args.assignee, args.status)
                elif args.command == "get": result = store.task(args.task_id)
                elif args.command == "approve": result = store.approve(args.task_id, "approved")
                elif args.command == "reject": result = store.approve(args.task_id, "rejected")
                elif args.command == "cancel": result = store.cancel(args.task_id)
                elif args.command == "message": result = store.message(args.task_id, args.sender, args.recipient, args.body)
                elif args.command == "inbox": result = store.inbox(args.recipient)
                elif args.command == "ack": result = store.ack(args.message_id, args.recipient)
                elif args.command == "artifacts": result = store.artifacts(args.task_id)
                elif args.command == "events": result = store.events(args.task_id)
                elif args.command == "recover": result = {"blocked_tasks": store.recover_expired()}
                elif args.command == "worker": result = execute_one(store, args.agent, args.config, dry_run=not args.execute)
                else: raise ValueError("unknown command")
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"status": "error", "reason": type(exc).__name__,
                          "message": str(exc)[:250]}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
