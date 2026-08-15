"""
Local web UI for the DevSecOps LangGraph, run as a durable Temporal Workflow:
streams each agent's status live as the workflow runs (each agent is a
retryable Temporal Activity — see temporal_worker.py), and surfaces the
human-approval interrupt as Approve/Reject buttons via a Temporal signal
instead of a terminal input() prompt.

Requires (each in its own terminal, alongside this one):
    temporal server start-dev
    python3 temporal_worker.py

Run:
    uvicorn web.server:app --reload
Then open http://127.0.0.1:8000
"""

import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from temporalio.client import Client
from temporalio.contrib.workflow_streams import WorkflowStreamClient

from graph import load_pr
from temporal_worker import TASK_QUEUE
from temporal_workflow import DevSecOpsWorkflow


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.temporal = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    )
    yield


app = FastAPI(lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/pr")
def get_pr():
    return load_pr()


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream_workflow(client: Client, handle, from_offset: int):
    """Consume the workflow's "progress" topic and translate it to our SSE
    events, mirroring the {node_name: {state updates}} chunk shape
    graph.astream(stream_mode="updates") already produces (see
    temporal_workflow.py). Stops (without acking) at an interrupt pause;
    acks and stops at real completion."""
    ws = WorkflowStreamClient.create(client, handle.id)
    try:
        async for item in ws.subscribe(
            topics="progress",
            from_offset=from_offset,
            result_type=dict,
            poll_cooldown=timedelta(milliseconds=200),
        ):
            chunk = item.data
            if "interrupt" in chunk:
                yield _sse("interrupt", {**chunk["interrupt"], "offset": item.offset})
                return
            if "error" in chunk:
                # The workflow publishes {"done": True} right after this and is
                # about to fail for real; keep draining so we still ack (below)
                # instead of leaving it hung on wait_condition(stream_acked).
                yield _sse("error", {"message": chunk["error"]})
                continue
            if chunk.get("done"):
                await handle.signal(DevSecOpsWorkflow.ack_stream)
                yield _sse("done", {})
                return
            for node, update in chunk.items():
                yield _sse("node", {"node": node, "update": update})
    except Exception as e:
        yield _sse("error", {"message": str(e)})


@app.post("/api/run")
async def run_review():
    pr = load_pr()
    client: Client = app.state.temporal
    workflow_id = f"devsecops-{uuid.uuid4()}"
    inputs = {
        "pr_title": pr["pr_title"],
        "pr_number": pr["pr_number"],
        "changed_files": pr["changed_files"],
        "agent_results": [],
        "root_cause": "",
        "risk_level": "",
        "recommendations": [],
        "final_decision": "",
    }

    handle = await client.start_workflow(
        DevSecOpsWorkflow.run,
        inputs,
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )

    async def event_stream():
        yield _sse("start", {"thread_id": workflow_id, "pr": pr})
        async for event in _stream_workflow(client, handle, from_offset=0):
            yield event

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class ApproveBody(BaseModel):
    thread_id: str
    decision: str
    offset: int = 0


@app.post("/api/approve")
async def approve(body: ApproveBody):
    client: Client = app.state.temporal
    handle = client.get_workflow_handle_for(DevSecOpsWorkflow.run, body.thread_id)
    await handle.signal(DevSecOpsWorkflow.submit_decision, body.decision)

    async def event_stream():
        async for event in _stream_workflow(client, handle, from_offset=body.offset + 1):
            yield event

    return StreamingResponse(event_stream(), media_type="text/event-stream")
