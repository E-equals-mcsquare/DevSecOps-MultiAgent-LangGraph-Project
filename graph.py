"""
The DevSecOps LangGraph: orchestrator -> parallel agents -> Claude synthesis
-> risk-based routing -> auto-merge or human approval.

Flow:
  1. orchestrator_node reads the PR's changed files.
  2. determine_agents fans out to whichever of terraform_agent /
     security_agent / pipeline_agent / cost_agent / generic_agent are
     relevant (agents/ package — real MCP-backed tool calls, each with a
     local/direct-API fallback).
  3. synthesis_node sends the combined findings to Claude for a structured
     root cause / risk level / recommendations review.
  4. route_by_risk sends low-risk PRs to auto_merge_node; anything else
     pauses at human_approval_node via interrupt() until a human (or CI
     bot) calls graph.invoke(Command(resume=decision), ...).

Every node below is tagged metadata={"execute_in": "activity"}. Plain
LangGraph (main.py's CLI path) ignores that metadata entirely — it's only
read by temporalio's LangGraphPlugin (see temporal_worker.py), which runs
each node as a durable, retryable Temporal Activity instead. `builder` (the
uncompiled StateGraph, registered with the plugin under the "devsecops" name
— see GRAPH_NAME in temporal_worker.py/temporal_workflow.py, duplicated
there rather than imported from here) and `graph` (compiled here for
direct/non-Temporal use) both come from the same definition, so nothing here
is Temporal-specific except the metadata itself. determine_agents/
route_by_risk are conditional-edge routers, not nodes — Temporal requires
those to run inline in the workflow and be async.

NOTE: temporal_workflow.py deliberately does NOT import anything from this
file. Temporal's sandboxed workflow runner validates a workflow module's
entire import chain for determinism, and this module pulls in httpx (via
agents/*) — which trips the sandbox's urllib.request restriction. Keep it
that way; if this module needs sharing with the workflow, pass data through
function arguments, not imports.
"""

import json
import operator
from typing import TypedDict, Annotated, Literal
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver
from langchain_anthropic import ChatAnthropic

from agents import terraform_agent, security_agent, pipeline_agent, cost_agent, generic_agent

load_dotenv()

ACTIVITY = {"execute_in": "activity"}


class ReviewOutput(BaseModel):
    root_cause: str = Field(description="Root cause summary of the issues found")
    risk_level: Literal["low", "medium", "high", "critical"] = Field(
        description="Overall risk level of this PR"
    )
    recommendations: list[str] = Field(description="Concrete fix recommendations")


class DevSecOpsState(TypedDict):
    pr_title: str
    pr_number: int
    changed_files: list[str]
    agent_results: Annotated[list[str], operator.add]
    root_cause: str
    risk_level: str
    recommendations: list[str]
    final_decision: str


def load_pr(path: str = "mock_data/sample_pr.json") -> dict:
    with open(path) as f:
        return json.load(f)


def orchestrator_node(state: DevSecOpsState) -> dict:
    return {}


async def determine_agents(state: DevSecOpsState) -> list[str]:
    files = state["changed_files"]
    agents = []
    if any(f.endswith(".tf") for f in files):
        agents.append("terraform_agent")
        agents.append("cost_agent")
    if any(f.endswith(".tf") or "iam" in f.lower() for f in files):
        agents.append("security_agent")
    if any(".github/workflows" in f or f.endswith(".yml") for f in files):
        agents.append("pipeline_agent")
    return agents or ["generic_agent"]


llm = ChatAnthropic(model="claude-sonnet-4-5-20250929", temperature=0)
structured_llm = llm.with_structured_output(ReviewOutput)


def synthesis_node(state: DevSecOpsState) -> dict:
    findings = "\n".join(f"- {r}" for r in state["agent_results"])
    prompt = (
        f"You are a DevSecOps review AI. PR '{state['pr_title']}' changed: "
        f"{state['changed_files']}.\n\nAutomated agent findings:\n{findings}\n\n"
        "Summarize root cause, classify overall risk (low/medium/high/critical), "
        "and give concrete fix recommendations."
    )
    result: ReviewOutput = structured_llm.invoke(prompt)
    return {
        "root_cause": result.root_cause,
        "risk_level": result.risk_level,
        "recommendations": result.recommendations,
    }


# --- Risk-based router ---
async def route_by_risk(state: DevSecOpsState) -> Literal["auto_merge", "human_approval"]:
    return "auto_merge" if state["risk_level"] == "low" else "human_approval"


def auto_merge_node(state: DevSecOpsState) -> dict:
    print("[auto_merge] risk=low -> merging automatically")
    return {"final_decision": "auto-merged"}


def human_approval_node(state: DevSecOpsState) -> dict:
    # PAUSES here. Whatever we pass to interrupt() is surfaced to the caller.
    decision = interrupt({
        "message": "Human approval required before merge",
        "risk_level": state["risk_level"],
        "root_cause": state["root_cause"],
        "recommendations": state["recommendations"],
    })
    # Execution resumes HERE when someone calls graph.invoke(Command(resume=decision), ...)
    print(f"[human_approval] received decision: {decision!r}")
    return {"final_decision": decision}


builder = StateGraph(DevSecOpsState)
builder.add_node("orchestrator", orchestrator_node, metadata=ACTIVITY)
builder.add_node("terraform_agent", terraform_agent, metadata=ACTIVITY)
builder.add_node("security_agent", security_agent, metadata=ACTIVITY)
builder.add_node("pipeline_agent", pipeline_agent, metadata=ACTIVITY)
builder.add_node("cost_agent", cost_agent, metadata=ACTIVITY)
builder.add_node("generic_agent", generic_agent, metadata=ACTIVITY)
builder.add_node("synthesis", synthesis_node, metadata=ACTIVITY)
builder.add_node("auto_merge", auto_merge_node, metadata=ACTIVITY)
builder.add_node("human_approval", human_approval_node, metadata=ACTIVITY)

builder.add_edge(START, "orchestrator")
builder.add_conditional_edges("orchestrator", determine_agents)
for agent in ["terraform_agent", "security_agent", "pipeline_agent", "cost_agent", "generic_agent"]:
    builder.add_edge(agent, "synthesis")
builder.add_conditional_edges("synthesis", route_by_risk)
builder.add_edge("auto_merge", END)
builder.add_edge("human_approval", END)

checkpointer = MemorySaver()
graph = builder.compile(checkpointer=checkpointer)
