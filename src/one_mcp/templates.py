"""Adapt ECC-inspired Engineering Desk templates to ONE's single roleflow executor.

This is a conversion of declarative templates into regular ONE tasks, NOT a
second scheduler or a copy of third-party ECC code. Engineering Desk's legacy
manual queue mode is separate; do not queue the same plan through both paths.
"""
from __future__ import annotations

from .engineering import ROLE_INSTRUCTIONS, validate_plan as validate_engineering
from .roleflow import validate_plan as validate_roleflow

ROLE_MAP = {
    "architect": "planner",
    "investigator": "researcher",
    "security": "security-reviewer",
    "skeptic": "reviewer",
}


def from_engineering(plan):
    """Convert one validated Engineering Desk template into an executable role DAG."""
    source = validate_engineering(plan)
    result = []
    for stage in source["stages"]:
        original = stage["role"]
        role = ROLE_MAP.get(original, original)
        if role not in {"planner", "researcher", "security-reviewer", "reviewer",
                        "implementer", "tester"}:
            raise ValueError("unsupported template role")
        readonly = role in {"planner", "researcher", "reviewer", "security-reviewer"}
        goal = (source["objective"] + "\nThis stage: " + stage["id"] +
                "\nSpecific role: " + ROLE_INSTRUCTIONS[original] +
                "\nProduce these evidence categories: " + ", ".join(stage["evidence"]) +
                "\nEnd with a single ONE_HANDOFF_JSON packet.")
        result.append({
            "key": stage["id"],
            "role": role,
            "title": source["id"] + " / " + stage["id"],
            "goal": goal,
            "assignee": source["assignees"][original],
            "base_commit": source["base_commit"],
            "allowed_paths": [] if readonly else source["allowed_paths"],
            "acceptance": ["Actual commands, evidence and exact file:line anchors",
                           "Never treat exit code 0 as verified",
                           "Deliver: " + ", ".join(stage["evidence"])[:180]],
            "context": "Stage " + stage["id"] + ". Only read predecessor artifact worktrees; each role has its own pinned checkout.",
            "depends_on": [{"step": parent, "gate": "completed"} for parent in stage["needs"]],
        })
    converted = {
        "id": "ecc-" + source["id"],
        "description": "ONE Engineering Desk template: " + source["workflow"],
        "project_context": (
            "Repository: " + source["repository"] + "\nObjective: " + source["objective"] +
            "\nNever touch security targets; preserve unrelated worktrees and untracked files. "
            "HOMELAB proof and RDL scientific truth authorities are separate from ONE task success. "
            "No auto push, merge, reset, clean, secret access or unapproved paid invocations.")[:2400],
        "steps": result,
    }
    return validate_roleflow(converted)
