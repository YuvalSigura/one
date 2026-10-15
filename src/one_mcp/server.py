"""MCP 2024-11-05 stdio JSON-RPC server (one JSON object per line).

No shell, target access, model invocation, or secret-resolution tools are exposed.
Compatible with MCP stdio clients using newline-delimited JSON messages.
"""
import json
import logging
import sys
from .store import Store

PROTOCOL = "2024-11-05"
def spec(name, description, properties, required=()):
    return {"name":name,"description":description,"inputSchema":{"type":"object","properties":properties,"required":list(required),"additionalProperties":False}}
def field(description, typ="string"):
    return {"type":typ,"description":description}
TOOLS = [
 spec("one_create_task","Create a queued development task (does not launch a process).",{
  "title":field("Short title"),"goal":field("Development objective"),"assignee":field("grok-bot|grok-cli|claude|codex|operator"),
  "owner":field("Requesting agent"),"base_commit":field("Git base commit"),
  "allowed_paths":{"type":"array","items":{"type":"string"}},"acceptance":{"type":"array","items":{"type":"string"}},
  "budget":field("Maximum budget units (advisory; no process runs)", "integer")},("title","goal","assignee")),
 spec("one_list_tasks","List bounded task summaries.",{"assignee":field("Agent filter"),"status":field("State filter"),"limit":field("Maximum 100","integer")}),
 spec("one_get_task","Get a task by ID.",{"task_id":field("Task ID")},("task_id",)),
 spec("one_claim_task","Claim a queued task for its assigned agent.",{"task_id":field("Task ID"),"agent":field("Assigned agent")},("task_id","agent")),
 spec("one_finish_task","Submit a task outcome. Only the assignee may finish a claimed task.",{"task_id":field("Task ID"),"agent":field("Assigned agent"),"status":field("completed|failed|blocked"),"result":field("Bounded outcome summary")},("task_id","agent","status","result")),
 spec("one_send_message","Send a bounded message associated with an existing task.",{"task_id":field("Task ID"),"sender":field("Sender"),"recipient":field("Recipient"),"body":field("Message")},("task_id","sender","recipient","body")),
 spec("one_inbox","Read messages for an agent.",{"recipient":field("Recipient"),"limit":field("Maximum 100","integer")},("recipient",)),
 spec("one_register_artifact","Register a SHA256 digest and path (metadata only, does not read file).",{"task_id":field("Task ID"),"producer":field("Agent"),"kind":field("diff|test|review|other"),"path":field("Local artifact path"),"digest":field("Lowercase SHA256")},("task_id","producer","kind","path","digest")),
 spec("one_list_artifacts","List artifact references for a task.",{"task_id":field("Task ID")},("task_id",))
]
TOOL_BY_NAME={t["name"]:t for t in TOOLS}

def dispatch(store, method, params):
    if method=="initialize":
        return {"protocolVersion":PROTOCOL,"capabilities":{"tools":{"listChanged":False}},"serverInfo":{"name":"one-mcp","version":"0.1.0"}}
    if method=="ping":
        return {}
    if method=="tools/list":
        return {"tools":TOOLS}
    if method=="tools/call":
        name=params.get("name")
        if name not in TOOL_BY_NAME:
            raise ValueError("unknown tool")
        args=params.get("arguments") or {}
        if not isinstance(args,dict):
            raise ValueError("arguments must be object")
        schema=TOOL_BY_NAME[name]["inputSchema"]
        if set(args)-set(schema["properties"]):
            raise ValueError("unknown tool arguments")
        if set(schema["required"])-set(args):
            raise ValueError("missing required tool arguments")
        actions={
         "one_create_task":lambda:store.create_task(**args),
         "one_list_tasks":lambda:store.list_tasks(**args),
         "one_get_task":lambda:store.task(**args),
         "one_claim_task":lambda:store.claim(args["task_id"],args["agent"]),
         "one_finish_task":lambda:store.finish(args["task_id"],args["agent"],args["status"],args["result"]),
         "one_send_message":lambda:store.message(**args),
         "one_inbox":lambda:store.inbox(**args),
         "one_register_artifact":lambda:store.add_artifact(**args),
         "one_list_artifacts":lambda:store.artifacts(**args)
        }
        result=actions[name]()
        return {"content":[{"type":"text","text":json.dumps(result,ensure_ascii=False)}],"isError":False}
    raise KeyError(method)

def handle(store, req):
    if not isinstance(req,dict) or req.get("jsonrpc")!="2.0" or not isinstance(req.get("method"),str):
        return {"jsonrpc":"2.0","id":req.get("id") if isinstance(req,dict) else None,"error":{"code":-32600,"message":"Invalid Request"}}
    if "id" not in req:
        return None
    rid=req["id"]
    try:
        return {"jsonrpc":"2.0","id":rid,"result":dispatch(store,req["method"],req.get("params") or {})}
    except KeyError:
        return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":"Method not found"}}
    except (ValueError,TypeError) as exc:
        if req["method"]=="tools/call":
            return {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":str(exc)}],"isError":True}}
        return {"jsonrpc":"2.0","id":rid,"error":{"code":-32602,"message":str(exc)}}
    except Exception:
        logging.exception("MCP tool failure")
        return {"jsonrpc":"2.0","id":rid,"error":{"code":-32603,"message":"Internal error"}}

def main():
    logging.basicConfig(stream=sys.stderr,level=logging.WARNING)
    store=Store()
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                response=handle(store,json.loads(line))
            except json.JSONDecodeError:
                response={"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"Parse error"}}
            if response is not None:
                sys.stdout.write(json.dumps(response,ensure_ascii=False,separators=(",",":"))+"\n")
                sys.stdout.flush()
    finally:
        store.close()

if __name__=="__main__":
    main()
