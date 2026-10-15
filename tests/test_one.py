import json
import tempfile
import unittest
from pathlib import Path
from one_mcp.store import Store
from one_mcp.server import handle, TOOLS

class OneTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/"one.db")
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def call(self,name,args):
        return handle(self.store,{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":name,"arguments":args}})["result"]
    def test_end_to_end_handoff(self):
        task=self.store.create_task("P0-1","Correct differential feedback","codex",base_commit="abc",acceptance=["pytest"])
        self.assertEqual(task["status"],"queued")
        self.assertTrue(self.call("one_claim_task",{"task_id":task["id"],"agent":"claude"})["isError"])
        self.assertFalse(self.call("one_claim_task",{"task_id":task["id"],"agent":"codex"})["isError"])
        self.assertTrue(self.call("one_send_message",{"task_id":task["id"],"sender":"codex","recipient":"claude","body":"Review the diff"})["content"])
        self.assertEqual(len(self.store.inbox("claude")),1)
        self.store.add_artifact(task["id"],"codex","diff","changes.patch","a"*64)
        self.assertEqual(len(self.store.artifacts(task["id"])),1)
        self.store.finish(task["id"],"codex","completed","offline tests passed")
        self.assertEqual(self.store.task(task["id"])["status"],"completed")
        with self.assertRaises(ValueError):
            self.store.finish(task["id"],"codex","completed","replay")
    def test_mcp_handshake_and_tools(self):
        x=handle(self.store,{"jsonrpc":"2.0","id":2,"method":"initialize","params":{}})
        self.assertEqual(x["result"]["serverInfo"]["name"],"one-mcp")
        x=handle(self.store,{"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}})
        self.assertEqual(len(x["result"]["tools"]),len(TOOLS))
        self.assertIsNone(handle(self.store,{"jsonrpc":"2.0","method":"notifications/initialized"}))
    def test_invalid_input(self):
        self.assertTrue(self.call("one_create_task",{"title":"x","goal":"y","assignee":"unknown"})["isError"])
        self.assertTrue(self.call("one_inbox",{"recipient":"claude","unexpected":"x"})["isError"])
        self.assertEqual(handle(self.store,{"jsonrpc":"2.0","id":4,"method":"does-not-exist"})["error"]["code"],-32601)

if __name__=="__main__":
    unittest.main()
