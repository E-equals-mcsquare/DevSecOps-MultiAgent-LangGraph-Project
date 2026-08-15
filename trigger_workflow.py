"""Started as a step in .github/workflows/pr-review.yml on every PR event.

Reads the triggering PR's title/number/changed-files straight from the
GitHub Actions event payload + a `git diff` against the checkout (needs
`actions/checkout` with fetch-depth: 0, or at least both SHAs fetched), then
starts ci_workflow.py's PRReviewWorkflow on Temporal and waits for it to
finish. A worker (temporal_worker.py, pointed at the same Temporal server)
must already be running and polling TASK_QUEUE — this script only starts the
workflow, it doesn't execute any graph nodes itself.

Runs as a job on a self-hosted GitHub Actions runner (see pr-review.yml:
`runs-on: self-hosted`) registered on your own machine — not a GitHub-hosted
runner, which is an ephemeral cloud VM that can't reach a Temporal server on
your laptop at all. A self-hosted runner IS a process on your machine, so it
reaches `localhost:7233` the same way `temporal_worker.py` does. That also
means no Temporal credentials need to leave your machine or live in GitHub
secrets at all — connects straight to TEMPORAL_ADDRESS, same as
temporal_worker.py's own `_connect()` (kept in sync manually; duplicated
rather than imported to keep this script's import chain light).

Run (normally only from CI, but works locally against a real PR too):
    python3 trigger_workflow.py
"""

import asyncio
import json
import os
import subprocess
import sys

from temporalio.client import Client, WorkflowFailureError

from ci_workflow import PRReviewWorkflow

TASK_QUEUE = "devsecops-review"  # kept in sync with temporal_worker.py's own copy


def _load_pr_from_github_event() -> dict:
    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        event = json.load(f)
    pr = event["pull_request"]
    base_sha, head_sha = pr["base"]["sha"], pr["head"]["sha"]
    changed_files = subprocess.run(
        ["git", "diff", "--name-only", base_sha, head_sha],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    return {"title": pr["title"], "number": pr["number"], "changed_files": changed_files}


async def _connect() -> Client:
    api_key = os.environ.get("TEMPORAL_API_KEY")
    if api_key:
        return await Client.connect(
            os.environ["TEMPORAL_ADDRESS"],
            namespace=os.environ["TEMPORAL_NAMESPACE"],
            api_key=api_key,
            tls=True,
        )
    return await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))


async def main() -> None:
    pr = _load_pr_from_github_event()
    print(f"PR #{pr['number']} — {pr['title']}")
    print(f"Changed files: {pr['changed_files']}")

    client = await _connect()

    inputs = {
        "pr_title": pr["title"],
        "pr_number": pr["number"],
        "changed_files": pr["changed_files"],
        "agent_results": [],
        "root_cause": "",
        "risk_level": "",
        "recommendations": [],
        "final_decision": "",
    }

    handle = await client.start_workflow(
        PRReviewWorkflow.run,
        inputs,
        id=f"pr-review-{pr['number']}-{os.environ.get('GITHUB_SHA', 'local')[:7]}",
        task_queue=TASK_QUEUE,
    )
    print(f"Started Temporal workflow {handle.id!r} — waiting for it to finish...")

    try:
        result = await handle.result()
    except WorkflowFailureError as e:
        print(f"::error::Temporal workflow failed: {e}")
        sys.exit(1)

    summary = (
        f"## Agentic AI DevSecOps Review\n\n"
        f"**Risk:** `{result.get('risk_level', 'unknown').upper()}`\n\n"
        f"**Root cause:** {result.get('root_cause', '')}\n\n"
        f"**Recommendations:**\n" + "\n".join(f"- {r}" for r in result.get("recommendations", [])) + "\n\n"
        f"**Result:** {result.get('final_decision', '')}\n"
    )
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write(summary)


if __name__ == "__main__":
    asyncio.run(main())
