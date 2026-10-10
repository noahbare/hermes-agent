"""Opt-in workflow process gates; these supplement, never waive, branch policy."""
from __future__ import annotations

import base64
import re
from typing import Any, Callable
from urllib.parse import quote

from hermes_cli.config import yaml

from hermes_cli.config_effective import load_user_config_effective

_PATH = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def configured_workflows(repo: str) -> list[str]:
    config = load_user_config_effective(fail_closed=True)
    gates = config.get("kanban", {}).get("pr_workflow_gates", {})
    if not isinstance(gates, dict):
        raise ValueError("pr_workflow_gates must map repositories to workflow paths")
    for repository, paths in gates.items():
        if (not isinstance(repository, str) or not _REPO.fullmatch(repository)
                or not isinstance(paths, list) or not paths
                or any(not isinstance(p, str) or not _PATH.fullmatch(p) for p in paths)
                or len(set(paths)) != len(paths)):
            raise ValueError("Invalid repository workflow gate")
    return gates.get(repo, [])


def inventory(content: dict, path: str) -> list[dict]:
    if content["path"] != path or content["type"] != "file" or content["encoding"] != "base64":
        raise ValueError("Workflow content unavailable")
    source = base64.b64decode("".join(content["content"].split()), validate=True).decode("utf-8")
    workflow = yaml.safe_load(source)
    jobs = workflow["jobs"]
    if not isinstance(jobs, dict) or not jobs:
        raise ValueError("Workflow has no job inventory")
    expected = []
    for job_id, job in jobs.items():
        if not isinstance(job_id, str) or not isinstance(job, dict):
            raise ValueError("Invalid workflow job")
        # Expanding matrices/reusable workflows requires resolving more executable inputs.
        # Until supported, no observed green job may substitute for their unknown inventory.
        if "strategy" in job or "uses" in job:
            raise ValueError("Matrix/reusable workflow inventory is unsupported")
        name = job.get("name", job_id)
        if not isinstance(name, str) or not name.strip() or "${{" in name:
            raise ValueError("Dynamic workflow job name is unsupported")
        expected.append({"job_id": job_id, "name": name})
    if len({j["name"] for j in expected}) != len(expected):
        raise ValueError("Ambiguous workflow job names")
    return expected


def complete_pages(pages: Any, field: str) -> list[dict]:
    if not isinstance(pages, list) or not pages:
        raise ValueError("Missing pagination evidence")
    total = pages[0]["total_count"]
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError("Invalid pagination count")
    if any(not isinstance(page[field], list) for page in pages):
        raise ValueError("Invalid pagination rows")
    rows = [row for page in pages for row in page[field]]
    if (any(page["total_count"] != total for page in pages)
            or len(rows) != total or len({r["id"] for r in rows}) != total):
        raise ValueError("Incomplete pagination evidence")
    return rows


def collect_workflow(api: Callable[..., Any], repo: str, path: str, sha: str, receipt: dict) -> list[str]:
    prefix = f"repos/{repo}"
    content = api(f"{prefix}/contents/{quote(path, safe='/')}?ref={sha}")
    expected = inventory(content, path)
    evidence = {"policy_source": "config.yaml:kanban.pr_workflow_gates", "path": path,
                "head_sha": sha, "blob_sha": content["sha"], "expected_jobs": expected,
                "jobs": []}
    receipt.setdefault("workflows", []).append(evidence)
    workflow = api(f"{prefix}/actions/workflows/{quote(path.rsplit('/', 1)[1], safe='')}")
    if workflow["path"] != path:
        raise ValueError("Workflow identity mismatch")
    workflow_id = workflow["id"]
    runs = complete_pages(api(f"{prefix}/actions/workflows/{workflow_id}/runs?head_sha={sha}&per_page=100",
                              paginate=True), "workflow_runs")
    if any(r["head_sha"] != sha or r["workflow_id"] != workflow_id or r["path"] != path for r in runs):
        raise ValueError("Workflow run identity mismatch")
    if not runs:
        evidence["classification"] = "missing"
        return ["missing"]
    run = max(runs, key=lambda r: (r["run_number"], r["id"]))
    run_id, attempt = run["id"], run["run_attempt"]
    evidence.update(workflow_id=workflow_id, run_id=run_id, run_attempt=attempt,
                    run_status=run["status"], run_conclusion=run["conclusion"], url=run["html_url"])
    jobs = complete_pages(api(f"{prefix}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
                              paginate=True), "jobs")
    outcomes = []
    for job in expected:
        matches = [j for j in jobs if j["name"] == job["name"]]
        if len(matches) > 1:
            raise ValueError("Ambiguous workflow job evidence")
        selected = matches[0] if matches else None
        outcome = "missing"
        if selected:
            if selected["head_sha"] != sha or selected["run_id"] != run_id:
                outcome = "stale"
            elif selected["status"] != "completed":
                outcome = "pending"
            else:
                outcome = "success" if selected["conclusion"] == "success" else "failure"
        outcomes.append(outcome)
        evidence["jobs"].append({**job, "classification": outcome,
            "id": selected["id"] if selected else None,
            "head_sha": selected.get("head_sha") if selected else None,
            "status": selected.get("status") if selected else None,
            "conclusion": selected.get("conclusion") if selected else None})
    # Reruns can begin during pagination, even without changing the PR head.
    current = api(f"{prefix}/actions/runs/{run_id}")
    if any(current[k] != run[k] for k in ("id", "head_sha", "workflow_id", "run_attempt", "status", "conclusion")):
        outcomes.append("stale")
    if run["status"] != "completed":
        outcomes.append("pending")
    elif run["conclusion"] != "success":
        outcomes.append("failure")
    latest = complete_pages(api(f"{prefix}/actions/workflows/{workflow_id}/runs?head_sha={sha}&per_page=100",
                                paginate=True), "workflow_runs")
    if not latest or max(latest, key=lambda r: (r["run_number"], r["id"])) != run:
        outcomes.append("stale")
    evidence["classification"] = next((o for o in outcomes if o != "success"), "success")
    return outcomes
