"""
Temporal Worker for the DevSecOps LangGraph — hosts ci_graph.py's nodes as
Temporal Activities (one per node tagged execute_in="activity"), for the
GitHub Actions PR-trigger path (see ci_workflow.py / trigger_workflow.py).

`main.py`'s CLI path runs graph.py's graph directly, in-process, with no
Temporal involved at all — this worker has nothing to do with it.

Runs entirely locally by default — no cloud dependency. GitHub Actions
reaches this via a self-hosted runner registered on this same machine (not a
GitHub-hosted one, which is an ephemeral cloud VM and genuinely cannot reach
localhost), so trigger_workflow.py can connect to the same Temporal dev
server this worker does:
    temporal server start-dev          # terminal 1 — local Temporal server + UI (localhost:8233)
    python3 temporal_worker.py         # terminal 2 — this
    ./run.sh                           # terminal 3 — self-hosted GH Actions runner

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

from ci_graph import ci_builder
from ci_workflow import PRReviewWorkflow

TASK_QUEUE = "devsecops-review"
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
        graphs={CI_GRAPH_NAME: ci_builder},
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
        workflows=[PRReviewWorkflow],
        plugins=[plugin],
    )
    print(f"Temporal worker started on task queue {TASK_QUEUE!r} ({client.namespace}). Ctrl+C to exit.")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
