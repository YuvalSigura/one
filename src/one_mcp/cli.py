"""Operator CLI for task coordination. No subprocess launches by default."""
import argparse
import json
from .store import Store

def main():
    p=argparse.ArgumentParser(prog="one")
    sub=p.add_subparsers(dest="command",required=True)
    t=sub.add_parser("create")
    t.add_argument("title");t.add_argument("goal");t.add_argument("assignee")
    t.add_argument("--owner",default="grok-bot")
    t.add_argument("--base-commit",default="")
    t.add_argument("--allowed-path",action="append",default=[])
    t.add_argument("--acceptance",action="append",default=[])
    t.add_argument("--budget",type=int,default=0)
    l=sub.add_parser("list");l.add_argument("--assignee");l.add_argument("--status")
    g=sub.add_parser("get");g.add_argument("task_id")
    c=sub.add_parser("claim");c.add_argument("task_id");c.add_argument("agent")
    f=sub.add_parser("finish");f.add_argument("task_id");f.add_argument("agent");f.add_argument("status",choices=["completed","failed","blocked"]);f.add_argument("result")
    m=sub.add_parser("message");m.add_argument("task_id");m.add_argument("sender");m.add_argument("recipient");m.add_argument("body")
    i=sub.add_parser("inbox");i.add_argument("recipient")
    a=sub.add_parser("artifact");a.add_argument("task_id");a.add_argument("producer");a.add_argument("kind");a.add_argument("path");a.add_argument("digest")
    al=sub.add_parser("artifacts");al.add_argument("task_id")
    args=p.parse_args()
    with_store=Store()
    try:
        if args.command=="create":
            result=with_store.create_task(args.title,args.goal,args.assignee,args.owner,args.base_commit,args.allowed_path,args.acceptance,args.budget)
        elif args.command=="list":result=with_store.list_tasks(args.assignee,args.status)
        elif args.command=="get":result=with_store.task(args.task_id)
        elif args.command=="claim":result=with_store.claim(args.task_id,args.agent)
        elif args.command=="finish":result=with_store.finish(args.task_id,args.agent,args.status,args.result)
        elif args.command=="message":result=with_store.message(args.task_id,args.sender,args.recipient,args.body)
        elif args.command=="inbox":result=with_store.inbox(args.recipient)
        elif args.command=="artifact":result=with_store.add_artifact(args.task_id,args.producer,args.kind,args.path,args.digest)
        else:result=with_store.artifacts(args.task_id)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        with_store.close()

if __name__=="__main__":
    main()
