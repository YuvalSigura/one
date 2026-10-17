import tempfile
import unittest
from pathlib import Path

from one_mcp.store import Store
from one_mcp.engineering import WORKFLOWS, make_plan
from one_mcp.roleflow import RoleFlow
from one_mcp.templates import from_engineering

SHA = "a" * 40


class EngineeringTemplateBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "one.db")
        self.flow = RoleFlow(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_all_templates_compile_to_one_roleflow_without_launch(self):
        for name in WORKFLOWS:
            plan = make_plan(plan_id="test-" + name, workflow=name,
                             objective="Develop an isolated fixture only",
                             repository="C:/tests/fixture", base_commit=SHA,
                             allowed_paths=["src", "tests"])
            role_plan = from_engineering(plan)
            self.assertEqual(len(role_plan["steps"]), len(WORKFLOWS[name]))
            self.assertTrue(all(s["base_commit"] == SHA for s in role_plan["steps"]))
            status = self.flow.create(role_plan)
            self.assertEqual(len(status["steps"]), len(WORKFLOWS[name]))
            self.assertTrue(all(s["approval"] == "pending" for s in status["steps"]))
        self.assertEqual(self.store.claim_next("codex"), None)
        self.assertEqual(self.store.claim_next("claude"), None)

    def test_review_roles_are_read_only_and_summary_is_bounded(self):
        original = make_plan(plan_id="review-case", workflow="defect",
                             objective="Fix local tests", repository="C:/fixture",
                             base_commit=SHA, allowed_paths=["src", "tests"])
        converted = from_engineering(original)
        reviewer = next(s for s in converted["steps"] if s["role"] == "reviewer")
        self.assertEqual(reviewer["allowed_paths"], [])
        self.assertTrue(all(d["gate"] == "completed" for s in converted["steps"] for d in s["depends_on"]))
        self.assertIn("never", converted["project_context"].lower())


if __name__ == "__main__":
    unittest.main()
