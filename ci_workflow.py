"""Temporal Workflow wrapping ci_graph.py's CI DevSecOps LangGraph.

Much simpler than temporal_workflow.py's DevSecOpsWorkflow: this graph never
interrupts (see ci_graph.py — no human_approval node), so there's no
signal/query/resume machinery and no progress stream to consume — a GitHub
Actions job just starts this workflow and awaits the result. Every node
still runs as a durable, retryable Temporal Activity (same
execute_in="activity" metadata, same worker — see temporal_worker.py).

Deliberately does NOT import from ci_graph.py or graph.py, for the same
sandboxing reason temporal_workflow.py documents: Temporal's workflow runner
validates this module's whole import chain for determinism, and both of
those modules pull in httpx via agents/*. CI_GRAPH_NAME is duplicated here
and in temporal_worker.py — keep them in sync if you rename the graph.
"""

from langchain_core.runnables import RunnableConfig
from temporalio import workflow
from temporalio.contrib.langgraph import graph as temporal_graph

CI_GRAPH_NAME = "devsecops-ci"


@workflow.defn
class PRReviewWorkflow:
    @workflow.run
    async def run(self, inputs: dict) -> dict:
        app = temporal_graph(CI_GRAPH_NAME).compile()
        config = RunnableConfig(
            {"configurable": {"thread_id": workflow.info().workflow_id}}
        )
        return await app.ainvoke(inputs, config)
