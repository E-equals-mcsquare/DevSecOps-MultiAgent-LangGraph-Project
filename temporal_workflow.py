"""
Temporal Workflow wrapping graph.py's DevSecOps LangGraph.

Drives the same graph as graph.py's `graph` (compiled fresh here via
temporalio's `graph(GRAPH_NAME)` helper, against the `builder` registered in
temporal_worker.py's LangGraphPlugin) — but every node tagged
metadata={"execute_in": "activity"} now runs as a durable, retryable Temporal
Activity instead of an inline Python call. Two things graph.py's own
compiled `graph` can't give you on its own:

  - Durability: MemorySaver only lives as long as the process. Here,
    Temporal's own history is the durability layer — the human-approval
    pause survives a worker restart, not just a paused Python call stack.
  - Retries: transient failures in an agent's MCP/Docker/npx call are
    retried automatically per the worker's RetryPolicy, instead of
    surfacing as-is on the first flake.

Progress streams to callers via a WorkflowStream "progress" topic, publishing
the same {node_name: {state updates}} chunks graph.astream(stream_mode=
"updates") already produces — see web/server.py for the consuming side.
Interrupt payloads can't cross the wire as raw langgraph.types.Interrupt
objects, so they're unwrapped to a plain dict before publishing.

Human approval: interrupt() (in human_approval_node) pauses the graph.
run() surfaces the interrupt's payload as {"interrupt": {...}} on the
progress topic and via the get_pending_interrupt query, then waits for the
submit_decision signal before resuming with Command(resume=decision).

Deliberately does NOT import from graph.py: Temporal's sandboxed workflow
runner validates this module's entire import chain for determinism, and
graph.py pulls in httpx (via agents/*), which trips the sandbox's
urllib.request restriction. GRAPH_NAME is duplicated here and in
temporal_worker.py (which isn't sandboxed and could import it, but keeping
both literal keeps the "why does one import and one not" question from
coming up) — keep the two in sync if you rename the graph.
"""

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from temporalio import workflow
from temporalio.contrib.langgraph import graph as temporal_graph
from temporalio.contrib.workflow_streams import WorkflowStream

GRAPH_NAME = "devsecops"


@workflow.defn
class DevSecOpsWorkflow:
    def __init__(self) -> None:
        # Must be constructed here (workflow init) so activity-side stream
        # writes have somewhere to publish to as soon as the workflow starts.
        self.stream = WorkflowStream()
        self._decision: str | None = None
        self._pending_interrupt: dict | None = None
        self._stream_acked = False

    @workflow.signal
    def submit_decision(self, decision: str) -> None:
        """Signalled by the client with the human's approve/reject decision."""
        self._decision = decision

    @workflow.signal
    def ack_stream(self) -> None:
        """Signalled by the client once it has finished consuming the stream."""
        self._stream_acked = True

    @workflow.query
    def get_pending_interrupt(self) -> dict | None:
        """The pending human-approval payload, or None if not currently paused."""
        return self._pending_interrupt

    @workflow.run
    async def run(self, inputs: dict) -> dict:
        app = temporal_graph(GRAPH_NAME).compile(checkpointer=InMemorySaver())
        config = RunnableConfig(
            {"configurable": {"thread_id": workflow.info().workflow_id}}
        )
        progress = self.stream.topic("progress")

        run_input = inputs
        final_state: dict = {}
        while True:
            self._pending_interrupt = None
            try:
                async for chunk in app.astream(run_input, config, stream_mode="updates"):
                    if "__interrupt__" in chunk:
                        payload = chunk["__interrupt__"][0].value
                        self._pending_interrupt = payload
                        progress.publish({"interrupt": payload})
                    else:
                        progress.publish(chunk)
                        for update in chunk.values():
                            if update:
                                final_state.update(update)
            except Exception as e:
                # An activity (e.g. synthesis_node's Claude call) failed all its
                # retries. Tell the client before letting the workflow fail for
                # real — a failed workflow is still the right terminal state
                # (shows up in Temporal's UI), but the client needs a clean
                # message instead of its stream just going silent.
                progress.publish({"error": str(e)})
                progress.publish({"done": True})
                await workflow.wait_condition(lambda: self._stream_acked)
                raise

            if self._pending_interrupt is None:
                break  # graph reached END on its own — not an interrupt pause

            self._decision = None
            await workflow.wait_condition(lambda: self._decision is not None)
            run_input = Command(resume=self._decision)

        progress.publish({"done": True})
        # The stream disappears once the workflow completes, so wait for the
        # client's ack that it has finished consuming before returning.
        await workflow.wait_condition(lambda: self._stream_acked)
        return final_state
