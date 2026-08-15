"""The CI DevSecOps LangGraph: orchestrator -> parallel agents -> Claude
synthesis -> post_summary. This is graph.py's flow with the risk-routing
tail (auto_merge / human_approval interrupt) replaced by a single
non-blocking terminal node.

Used by the GitHub Actions path (see .github/workflows/pr-review.yml,
trigger_workflow.py, ci_workflow.py): a real PR triggers the workflow, every
agent runs as a Temporal Activity, and the last activity posts the review as
a PR comment — no human sits at a UI waiting on an interrupt, so there's
nothing here to pause on. A human still makes the merge decision, just via
GitHub's normal PR review UI instead of an Approve/Reject button wired to a
Temporal signal (that flow still exists for the web UI — see graph.py /
temporal_workflow.py — untouched by this file).

Reuses orchestrator_node/determine_agents/synthesis_node/ACTIVITY/
DevSecOpsState from graph.py directly: this module is only ever imported by
temporal_worker.py (not sandboxed), never by ci_workflow.py (sandboxed) —
same constraint graph.py documents for the web-UI path.
"""

from langgraph.graph import StateGraph, START, END

from graph import (
    ACTIVITY,
    DevSecOpsState,
    orchestrator_node,
    determine_agents,
    synthesis_node,
)
from agents import terraform_agent, security_agent, pipeline_agent, cost_agent, generic_agent, post_summary_node

ci_builder = StateGraph(DevSecOpsState)
ci_builder.add_node("orchestrator", orchestrator_node, metadata=ACTIVITY)
ci_builder.add_node("terraform_agent", terraform_agent, metadata=ACTIVITY)
ci_builder.add_node("security_agent", security_agent, metadata=ACTIVITY)
ci_builder.add_node("pipeline_agent", pipeline_agent, metadata=ACTIVITY)
ci_builder.add_node("cost_agent", cost_agent, metadata=ACTIVITY)
ci_builder.add_node("generic_agent", generic_agent, metadata=ACTIVITY)
ci_builder.add_node("synthesis", synthesis_node, metadata=ACTIVITY)
ci_builder.add_node("post_summary", post_summary_node, metadata=ACTIVITY)

ci_builder.add_edge(START, "orchestrator")
ci_builder.add_conditional_edges("orchestrator", determine_agents)
for agent in ["terraform_agent", "security_agent", "pipeline_agent", "cost_agent", "generic_agent"]:
    ci_builder.add_edge(agent, "synthesis")
ci_builder.add_edge("synthesis", "post_summary")
ci_builder.add_edge("post_summary", END)

# No checkpointer: nothing in this graph pauses, so there's no state to resume.
ci_graph = ci_builder.compile()
