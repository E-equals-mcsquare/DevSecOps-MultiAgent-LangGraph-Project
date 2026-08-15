"""
Temporal Worker for the DevSecOps LangGraph — hosts both graphs' nodes as
Temporal activities (one per node tagged execute_in="activity"):
  - graph.py's `builder`      (web UI path: human-approval interrupt/signal)
  - ci_graph.py's `ci_builder` (GitHub Actions path: no interrupt, ends by
    posting a PR comment — see ci_workflow.py)

Both paths run against a single local Temporal dev server by default — no
cloud dependency needed for either. The GitHub Actions path works entirely
locally too: pr-review.yml runs on a self-hosted runner registered on this
same machine (not a GitHub-hosted one, which is an ephemeral cloud VM and
genuinely cannot reach localhost), so trigger_workflow.py can connect to the
same Temporal dev server this worker does:
    temporal server start-dev          # terminal 1 — local Temporal server + UI (localhost:8233)
    python3 temporal_worker.py         # terminal 2 — this
    uvicorn web.server:app --reload    # terminal 3 — web UI (localhost:8000)
    ./run.sh                           # terminal 4 — self-hosted GH Actions runner (GHAC path only)

Set TEMPORAL_API_KEY in .env to instead point this at Temporal Cloud (e.g.
TEMPORAL_ADDRESS=<namespace>.<account>.tmprl.cloud:7233, TEMPORAL_NAMESPACE) —
`_connect()` below picks whichever mode your env is configured for.
"""

import asyncio
import os
from datetime import timedelta

from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import LangGraphPlugin
from temporalio.worker import Worker

from graph import builder
from ci_graph import ci_builder
from temporal_workflow import DevSecOpsWorkflow
from ci_workflow import PRReviewWorkflow

TASK_QUEUE = "devsecops-review"
GRAPH_NAME = "devsecops"  # kept in sync with temporal_workflow.py's own copy
CI_GRAPH_NAME = "devsecops-ci"  # kept in sync with ci_workflow.py's own copy


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
    client = await _connect()

    plugin = LangGraphPlugin(
        graphs={GRAPH_NAME: builder, CI_GRAPH_NAME: ci_builder},
        default_activity_options={
            # terraform_agent alone can take ~2min (HCP Terraform run polling);
            # generous enough to cover that plus Docker/npx cold starts.
            "start_to_close_timeout": timedelta(seconds=150),
            "retry_policy": RetryPolicy(maximum_attempts=3),
        },
    )

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[DevSecOpsWorkflow, PRReviewWorkflow],
        plugins=[plugin],
    )
    print(f"Temporal worker started on task queue {TASK_QUEUE!r} ({client.namespace}). Ctrl+C to exit.")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
