"""ONE local operator CLI. Approvals and worker runs are never exposed via MCP."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .store import Store
from .worker import execute_one, doctor
from .connections import emit
from .roleflow import RoleFlow, read_plan as read_role_plan
from .templates import from_engineering
from .engineering import read_plan as read_engineering_plan


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
    worker.add_argument("agent", choices=["claude", "codex", "grok-cli", "antigravity"])
    worker.add_argument("--config", required=True)
    worker.add_argument("--execute", action="store_true", help="Explicit operator execution; default dry-run")
    connection = sub.add_parser("connections", help="Print safe local MCP configuration snippets")
    connection.add_argument("--output-dir", help="Write new snippet files here, never overwrite")
    check = sub.add_parser("doctor")
    check.add_argument("--config", required=True)
    flow_create = sub.add_parser("flow-create", help="Create a durable role pipeline; no workers launched")
    flow_create.add_argument("plan", help="Path to reviewed JSON role-flow plan")
    flow_template = sub.add_parser("flow-template", help="Convert an Engineering Desk method to a runnable role DAG")
    flow_template.add_argument("plan", help="Path to an existing reviewed Engineering Desk JSON plan")
    flow_status = sub.add_parser("flow-status", help="Show role pipeline and dependency readiness")
    flow_status.add_argument("flow_id")
    flow_approve = sub.add_parser("flow-approve", help="Preauthorize frozen flow; approve each task only when dependency-ready")
    flow_approve.add_argument("flow_id")
    flow_approve.add_argument("--confirm", action="store_true", help="Explicitly authorize this reviewed workflow")
    flow_revoke = sub.add_parser("flow-revoke", help="Revoke future task launches; running work must be stopped separately")
    flow_revoke.add_argument("flow_id")
    flow_verdict = sub.add_parser("flow-verdict", help="Operator review gate; NOT scientific verification")
    flow_verdict.add_argument("task_id")
    flow_verdict.add_argument("decision", choices=["accepted", "rejected"])
    flow_verdict.add_argument("--reason", required=True)
    flow_run = sub.add_parser("flow-run", help="Dispatch only approved stages, with dependency handoffs")
    flow_run.add_argument("flow_id")
    flow_run.add_argument("--config", required=True)
    flow_run.add_argument("--max-tasks", type=int, default=8)
    flow_run.add_argument("--parallel", type=int, default=2)
    flow_run.add_argument("--execute", action="store_true", help="Actually invoke paid providers; default dry run")
    args = parser.parse_args(argv)
    try:
        if args.command == "connections":
            result = emit(args.output_dir)
        elif args.command == "doctor":
            result = doctor(args.config)
        else:
            with Store() as store:
                if args.command == "flow-create":
                    result = RoleFlow(store).create(read_role_plan(Path(args.plan)))
                elif args.command == "flow-template":
                    result = RoleFlow(store).create(from_engineering(read_engineering_plan(Path(args.plan))))
                elif args.command == "flow-status":
                    result = RoleFlow(store).status(args.flow_id)
                elif args.command == "flow-approve":
                    if not args.confirm:
                        raise ValueError("explicit --confirm required before authorizing flow")
                    result = RoleFlow(store).approve_all(args.flow_id)
                elif args.command == "flow-revoke":
                    result = RoleFlow(store).revoke(args.flow_id)
                elif args.command == "flow-verdict":
                    result = RoleFlow(store).verdict(args.task_id, args.decision, args.reason)
                elif args.command == "flow-run":
                    result = RoleFlow(store).execute_approved(args.flow_id, args.config,
                        max_tasks=args.max_tasks, parallel=args.parallel, execute=args.execute)
                elif args.command == "create":
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
