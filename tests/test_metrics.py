"""Metrics are factual metadata; never invented provider token savings."""
import json
import tempfile
import unittest
from pathlib import Path

from one_mcp.store import Store
from one_mcp.roleflow import RoleFlow
from one_mcp.metrics import flow_metrics


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.db")
        self.flow = RoleFlow(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_reports_task_progress_without_mislabeling_prompt_bytes_as_tokens(self):
        self.flow.create({
            "id": "metric-flow", "description": "metric smoke",
            "project_context": "test scope", "steps": [{
                "key": "implement", "role": "implementer", "title": "a",
                "goal": "make a change", "assignee": "codex",
                "base_commit": "a" * 40, "allowed_paths": ["src"], "depends_on": []
            }]})
        artifacts = self.root / "artifacts"; artifacts.mkdir()
        config = self.root / "config.json"
        config.write_text(json.dumps({"agents": {}, "artifact_root": str(artifacts)}))
        snap = self.flow.status("metric-flow")["steps"][0]
        task_id = snap["task_id"]
        path = artifacts / task_id; path.mkdir()
        (path / "task.md").write_text("12345")
        output = flow_metrics(self.store, "metric-flow", config)
        self.assertEqual(output["steps_total"], 1)
        self.assertEqual(output["status_counts"]["queued"], 1)
        self.assertEqual(output["measured_brief_bytes"], 5)
        self.assertIsNone(output["provider_token_savings"])
        self.assertIsNone(output["steps"][0]["provider_usage_reported"])

    def test_unknown_flow_is_not_conflated_with_empty_metrics(self):
        with self.assertRaises(ValueError):
            flow_metrics(self.store, "never-created")


if __name__ == "__main__":
    unittest.main()
