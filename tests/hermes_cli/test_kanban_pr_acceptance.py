"""Exact-head lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import base64
import os
import sys
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            state.setdefault("identities", []).append(self.headers.get("X-Test-Gh-Token"))
            sha = state["head"]
            if self.path == "/graphql":
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                        {"context": "required", "app": {"databaseId": 1}}]}}}}}}
                if state.get("no_branch_checks"):
                    value["data"]["repository"]["pullRequest"]["baseRef"] = None
            elif "/rules/branches/" in self.path:
                if state.get("policy_error"):
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "403", "message": state["policy_error"]}).encode())
                    return
                value = [[]]
                if state.get("rules_checks"):
                    value = [[{"type": "required_status_checks", "parameters": {
                        "required_status_checks": [{"context": "required", "integration_id": 1}]}}]]
            elif "/contents/" in self.path:
                source = state.get("workflow", "jobs:\n  build:\n    name: Build\n    runs-on: ubuntu-latest\n    steps: []\n")
                value = {"path": ".github/workflows/ci.yml", "type": "file", "encoding": "base64",
                         "sha": "c" * 40, "content": base64.b64encode(source.encode()).decode()}
            elif "/actions/workflows/ci.yml" in self.path:
                value = {"id": 10, "path": ".github/workflows/ci.yml"}
            elif "/actions/" in self.path:
                run = {"id": 100, "workflow_id": 10, "head_sha": sha, "path": ".github/workflows/ci.yml",
                       "run_number": 1, "run_attempt": 1, "status": "completed", "conclusion": "success",
                       "html_url": "https://github.com/acme/repo/actions/runs/100"}
                if state.get("wrong_workflow_head"):
                    run["head_sha"] = "b" * 40
                if "/jobs?" in self.path:
                    job = {"id": 101, "run_id": 100, "head_sha": sha, "name": state.get("job_name", "Build"),
                           "status": state.get("job_status", "completed"), "conclusion": state.get("job_conclusion", "success")}
                    if state.get("wrong_job_head"):
                        job["head_sha"] = "b" * 40
                    jobs = [] if state.get("missing_job") else [job]
                    value = [{"total_count": len(jobs) + int(state.get("bad_job_pages", False)), "jobs": jobs}]
                    if state.get("workflow_head_race"):
                        state["head"] = "b" * 40
                elif "/runs?" in self.path:
                    runs = [] if state.get("missing_run") else [run]
                    if state.get("newer_failed"):
                        runs.append({**run, "id": 200, "run_number": 2, "conclusion": "failure"})
                    value = [{"total_count": len(runs) + int(state.get("bad_run_pages", False)), "workflow_runs": runs}]
                else:
                    value = {**run, "run_attempt": 2} if state.get("attempt_race") else run
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,os,urllib.request,json\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "if '--slurp' in sys.argv: sys.exit(2)\n"
                  "request=urllib.request.Request(u, headers={'X-Test-Gh-Token': os.getenv('GH_TOKEN', '')})\n"
                  "try:\n value=json.loads(urllib.request.urlopen(request).read())\n"
                  " if '--paginate' in sys.argv:\n  for page in value: print(json.dumps(page))\n"
                  " else: print(json.dumps(value))\n"
                  "except urllib.error.HTTPError as e:\n print(e.read().decode()); print('HTTP '+str(e.code), file=sys.stderr); sys.exit(1)\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.linux_only
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.linux_only
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


@pytest.mark.linux_only
def test_configured_workflow_gate_is_exact_complete_and_additive(github):
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text("kanban:\n  pr_workflow_gates:\n    acme/repo: [.github/workflows/ci.yml]\n")
    plan_error = "Upgrade to GitHub Pro or make this repository public to enable this feature."
    github.update(policy_error=plan_error, no_branch_checks=True)
    cases = [({}, True), ({"missing_job": True}, False),
             ({"job_status": "queued"}, False), ({"job_status": "in_progress"}, False),
             *[({"job_conclusion": c}, False) for c in ("failure", "skipped", "cancelled", None)],
             ({"wrong_workflow_head": True}, False), ({"wrong_job_head": True}, False),
             ({"missing_run": True}, False),
             ({"newer_failed": True}, False), ({"workflow_head_race": True}, False),
             ({"attempt_race": True}, False), ({"bad_job_pages": True}, False),
             ({"bad_run_pages": True}, False), ({"job_name": "Unrelated green job"}, False),
             ({"policy_error": "Resource not accessible by integration"}, False),
             ({"no_branch_checks": False, "conclusion": "failure"}, False),
             ({"no_branch_checks": False, "conclusion": "success"}, True),
             ({"policy_error": None, "rules_checks": True, "conclusion": "failure"}, False),
             ({"policy_error": None, "rules_checks": True}, True),
             *[({"workflow": source}, False) for source in (
                 "jobs:\n  build:\n    strategy: {matrix: {os: [linux, macos]}}\n",
                 "jobs:\n  build:\n    name: '${{ matrix.os }}'\n",
                 "jobs:\n  build:\n    uses: acme/shared/.github/workflows/build.yml@main\n",
                 "jobs: {}\n", "jobs: [\n")]]
    with connect() as conn:
        for faults, expected in cases:
            github.update(faults)
            tid = kb.create_task(conn, title="Gate", completion_contract="acme/repo")
            assert kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"}) is expected, faults
            assert (kb.get_task(conn, tid).status == "done") is expected
            receipt = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'",
                                             (tid,)).fetchone()[0])
            if expected:
                if github.get("policy_error"):
                    assert receipt["unavailable_policy"][0]["reason"] == "private-plan-not-supported"
                assert receipt["workflows"][0]["expected_jobs"] == [{"job_id": "build", "name": "Build"}]
                assert receipt["workflows"][0]["run_id"] == 100
            for key in faults:
                github.pop(key, None)
            github.update(head="a" * 40, conclusion="success", policy_error=plan_error, no_branch_checks=True)


@pytest.mark.linux_only
def test_workflow_policy_is_opt_in_and_scoped_to_current_home(github, tmp_path, monkeypatch):
    from hermes_cli.kanban_pr_acceptance import collect_acceptance
    plan_error = "Upgrade to GitHub Pro or make this repository public to enable this feature."
    github.update(policy_error=plan_error, no_branch_checks=True)
    homes = [tmp_path / "a", tmp_path / "b"]
    for home, repo in zip(homes, ("acme/repo", "other/repo")):
        home.mkdir()
        (home / "config.yaml").write_text(f"kanban:\n  pr_workflow_gates:\n    {repo}: [.github/workflows/ci.yml]\n")
    for home, expected in ((homes[0], True), (homes[1], False), (homes[0], True)):
        monkeypatch.setenv("HERMES_HOME", str(home))
        receipt = collect_acceptance("https://github.com/acme/repo/pull/7", None)
        assert receipt["ok"] is expected
    (homes[0] / "config.yaml").write_text("kanban:\n  pr_workflow_gates: {acme/repo: []}\n")
    assert not collect_acceptance("https://github.com/acme/repo/pull/7", None)["ok"]


@pytest.mark.linux_only
def test_workflow_pages_and_head_reread_keep_assignee_identity(github, monkeypatch):
    home = Path(os.environ["HERMES_HOME"])
    profile = home / "profiles" / "b"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("GH_TOKEN=b-token\n", encoding="utf-8")
    (home / "config.yaml").write_text("kanban:\n  pr_workflow_gates:\n    acme/repo: [.github/workflows/ci.yml]\n")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    github.update(policy_error="Upgrade to GitHub Pro or make this repository public to enable this feature.",
                  no_branch_checks=True)
    with connect() as conn:
        tid = kb.create_task(conn, title="Profile workflow", assignee="b", completion_contract="acme/repo")
        assert kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
    assert any("/contents/" in path for path in github["requests"])
    assert github["requests"][-1].endswith("/pulls/7")
    assert set(github["identities"]) == {"b-token"}


@pytest.mark.linux_only
def test_generic_rules_permission_denial_is_auth_even_with_workflow_opt_in(github):
    from hermes_cli.kanban_pr_acceptance import collect_acceptance
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text("kanban:\n  pr_workflow_gates:\n    acme/repo: [.github/workflows/ci.yml]\n")
    github.update(policy_error="Resource not accessible by integration", no_branch_checks=True)
    receipt = collect_acceptance("https://github.com/acme/repo/pull/7", None)
    assert not receipt["ok"] and receipt["classification"] == "auth"
    assert "HTTP 403" in receipt["detail"]
    assert not any("/actions/" in path for path in github["requests"])
