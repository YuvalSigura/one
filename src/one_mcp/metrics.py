"""Read-only, honest workflow productivity measurements from existing ONE records.

No estimate of tokens saved is treated as measured provider usage. Never loads
raw provider transcripts, source files, or secrets into the report.
"""
from __future__ import annotations

from pathlib import Path

from .roleflow import RoleFlow


def flow_metrics(store, flow_id, config_path=None):
    flow = RoleFlow(store).status(flow_id)
    artifact_root = None
    if config_path:
        from .context import load_config
        cfg = load_config(config_path)
        if cfg.get("artifact_root"):
            artifact_root = Path(cfg["artifact_root"]).expanduser().resolve(strict=True)
    counts = {"queued": 0, "running": 0, "completed": 0, "blocked": 0,
              "failed": 0, "cancelled": 0}
    report = []
    total_brief_bytes = 0
    observed_briefs = 0
    for step in flow["steps"]:
        task = store.task(step["task_id"])
        counts[task["status"]] += 1
        artifacts = store.artifacts(task["id"])
        quality = [a for a in artifacts if a["kind"] == "engineering_quality"]
        usage = task.get("usage_json")
        brief_bytes = None
        if artifact_root:
            p = artifact_root / task["id"] / "task.md"
            if p.exists() and p.is_file() and not p.is_symlink():
                resolved = p.resolve(strict=True)
                if resolved.is_relative_to(artifact_root):
                    brief_bytes = resolved.stat().st_size
                    observed_briefs += 1
                    total_brief_bytes += brief_bytes
        report.append({
            "step": step["key"], "role": step["role"], "assignee": step["assignee"],
            "status": task["status"], "review_decision": (step["verdict"] or {}).get("decision"),
            "quality_receipts": len(quality), "elapsed_since_task_creation_seconds":
            max(0, round(task["updated"] - task["created"], 3)),
            "brief_bytes": brief_bytes,
            "provider_usage_reported": usage,  # None is UNKNOWN, never zero
        })
    return {
        "flow_id": flow_id, "status_counts": counts, "steps_total": len(report),
        "operator_accepted": sum(x["review_decision"] == "accepted" for x in report),
        "operator_rejected": sum(x["review_decision"] == "rejected" for x in report),
        "quality_receipts_total": sum(x["quality_receipts"] for x in report),
        "measured_brief_bytes": total_brief_bytes,
        "briefs_measured": observed_briefs,
        "provider_token_savings": None,
        "provider_token_savings_note": (
            "UNKNOWN: requires matched baseline experiments with provider-native usage. "
            "Brief bytes are not tokens, and short prompts do not prove saved model spend."),
        "steps": report
    }
