# Agentic AI + DevSecOps — LangGraph

An agentic PR review pipeline built on LangGraph + Claude: an orchestrator routes
a changed-files list to the relevant agents, which run real tools (Terraform,
Checkov, GitHub Actions) in parallel; Claude synthesizes their findings into a
root cause / risk level / recommendations review; low-risk PRs auto-merge,
everything else pauses for human approval.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY, GITHUB_REPO, etc.
```

Run it as a CLI:

```bash
python3 main.py
```

Or run the web UI — same graph, but durably: each agent runs as a retryable
Temporal Activity, progress streams live, and the human-approval pause
survives a worker restart instead of only living in one process's memory.
Needs three processes, each in its own terminal:

```bash
temporal server start-dev          # local Temporal server + UI at localhost:8233
python3 temporal_worker.py         # hosts the graph's activities
uvicorn web.server:app --reload    # the UI itself
```

Then open http://127.0.0.1:8000 and click **Run Review**.

There's a third way to run this: a real PR triggers it via GitHub Actions, no
human at a browser — see "GitHub Actions PR trigger (CI path)" below.

## Structure

| Path | What it is |
|---|---|
| `main.py` | CLI entry point — loads the PR, runs the graph directly (no Temporal), formats/prints the PR review comment, handles the human-approval pause/resume loop via `input()` |
| `web/` | Local web UI — `server.py` (FastAPI, a Temporal client: starts the workflow, streams its progress over SSE, signals it for the interrupt) + `static/` (vanilla HTML/CSS/JS, no build step) |
| `graph.py` | The LangGraph itself: state, orchestrator, conditional fan-out/fan-in to agents, Claude synthesis node, risk-based routing, `interrupt()`-based human approval. Every node is tagged `metadata={"execute_in": "activity"}`, read only by Temporal — plain LangGraph (`main.py`) ignores it |
| `temporal_workflow.py` | `DevSecOpsWorkflow` — drives `graph.py`'s graph through Temporal instead of `graph.astream()` directly; publishes live progress, exposes the pending interrupt via signal/query |
| `temporal_worker.py` | Hosts `graph.py`'s nodes as Temporal Activities (retries, timeouts) |
| `agents/` | One module per agent — `terraform_agent.py`, `security_agent.py`, `pipeline_agent.py`, `cost_agent.py`, `generic_agent.py` (fallback, no tool), `summary_agent.py` (`post_summary_node` — posts the review as a PR comment, CI path only). Unchanged by the Temporal integration — same functions run as plain Python calls (CLI) or Temporal Activities (web UI/CI) |
| `infra/terraform/` | Sample Terraform (S3 bucket + an intentionally overly-permissive IAM policy) that `terraform_agent`/`security_agent` run against |
| `mock_data/sample_pr.json` | Stand-in for a real GitHub webhook payload for the CLI/web-UI paths — the CI path (below) reads a real PR instead |
| `Dockerfile` | Image for hosting `temporal_worker.py` — `terraform`/`snyk`/`uvx` baked in, no secrets. See "Hosting the worker in Docker" below |
| `ci_graph.py` | The CI graph: `graph.py`'s flow with the interrupt/auto-merge tail replaced by a single `post_summary` node — no human sits at a UI waiting on a signal |
| `ci_workflow.py` | `PRReviewWorkflow` — the CI path's Temporal Workflow. Simpler than `DevSecOpsWorkflow`: no signals/queries/stream, since `ci_graph.py` never interrupts |
| `trigger_workflow.py` | Run as a GitHub Actions step: reads the triggering PR from the Actions event payload, connects to Temporal (local dev server by default, same as `temporal_worker.py`), starts `PRReviewWorkflow`, waits for it, writes the result to the job summary |
| `.github/workflows/pr-review.yml` | Triggers `trigger_workflow.py` on every `pull_request` event; runs on a self-hosted runner (registered on your own machine) so it can reach a local Temporal server — no cloud account, no credentials stored in GitHub at all |

## MCP integrations

Three agents call real, official MCP servers instead of hitting APIs/CLIs directly.
`pipeline_agent`/`post_summary_node` (see below) are the only ones that are genuinely
*remotely hosted* by their provider — `cost_agent`'s MCP server still runs as a local
subprocess (`uvx`) even though it's calling a real AWS API.

| Agent | MCP server | Requires |
|---|---|---|
| `pipeline_agent` | GitHub's official **remote** MCP server at `api.githubcopilot.com/mcp/` — `actions_list` (`list_workflow_runs`) | `GITHUB_TOKEN` (PAT, `repo` scope) and `GITHUB_REPO` in `.env` |
| `post_summary_node` (`agents/summary_agent.py`) | Same GitHub remote MCP server — `add_issue_comment` | Same as above |
| `cost_agent` | AWS Labs' [Billing and Cost Management MCP server](https://awslabs.github.io/mcp/servers/billing-cost-management-mcp-server) (`uvx awslabs.billing-cost-management-mcp-server`, local stdio subprocess) — `cost-explorer` (`getCostAndUsage`) + `cost-optimization` (`list_recommendations`) | Real AWS credentials (`~/.aws` or `AWS_PROFILE`/`AWS_ACCESS_KEY_ID`); `uv`/`uvx` installed |

Notes:
- GitHub's remote MCP server only enables a default toolset (repos, issues, pull
  requests, ...) — Actions tools aren't in it. `pipeline_agent` requests them via the
  `X-MCP-Toolsets: actions` header.
- The AWS billing MCP server's `cost-explorer` tool requires `metrics`/`group_by` as
  JSON-encoded strings (`json.dumps([...])`), not plain lists — the tool's own examples
  are misleading here. Its Cost Explorer endpoint only works from `us-east-1`, regardless
  of which region your resources are in.
- `cost_agent` reports real account-wide spend and optimization recommendations, not a
  cost estimate of the specific PR's Terraform diff — Cost Explorer works on actual
  billing history, not unapplied plans. Optimization recommendations require Cost
  Optimization Hub to be enrolled for the account (Console → Cost Optimization Hub);
  `cost_agent` reports that gap instead of failing when it isn't.
- `cost_agent` runs alongside `terraform_agent` (routed on `.tf` changes) since cost
  impact is naturally tied to infra changes.

## Local-only agents (no MCP)

`terraform_agent` and `security_agent` deliberately don't use MCP at all — both
originally called a Docker- or npx-spawned MCP server (HashiCorp's Terraform MCP
server, Snyk's MCP server), which meant anywhere you host `temporal_worker.py` needs
Docker/Node available. That's real hosting friction this project doesn't need, so
both were simplified to a single local-CLI path — real tools, real findings, no MCP
protocol layer, no subprocess-spawned server:

| Agent | Tool | Requires |
|---|---|---|
| `terraform_agent` | `terraform init/validate/plan` (subprocess) against `infra/terraform/` | `terraform` CLI on `PATH` |
| `security_agent` | `snyk iac test --json` (subprocess, standalone binary — see install below) against `infra/terraform/` | `SNYK_TOKEN` in `.env`; `snyk` CLI on `PATH` |

Neither is a "dumb" CLI wrapper, though: both pipe their raw tool output through
Claude (`agents/llm_utils.py`'s `summarize_tool_output()`, same `ChatAnthropic` model
`synthesis_node` uses) to turn a mechanical plan diff / issues list into an actual
risk narrative — genuinely a small agent, not just a subprocess call.

**Installing the Snyk CLI** (no Node/npm — it's a standalone binary, and with it gone
this project has no Node.js dependency anywhere):
```bash
curl -Lo /usr/local/bin/snyk https://downloads.snyk.io/cli/latest/snyk-linux   # or snyk-macos-arm64, etc.
chmod +x /usr/local/bin/snyk
```
Bake that into whatever image/host runs `temporal_worker.py` (same place `terraform`
needs to live) — GitHub Actions runners never need it, since `security_agent` runs as
a Temporal Activity inside the worker process, not inside `trigger_workflow.py`'s job.

Notes:
- `snyk iac test` scans locally — rule evaluation happens in the CLI itself, your
  `.tf` files are never uploaded anywhere. Confirmed against real output: scanning a
  directory returns a JSON **array**, one object per file, each with an
  `infrastructureAsCodeIssues` list (`severity`/`title`/`msg`/`lineNumber`/`resolve`
  per issue) — `security_agent` flattens all files' issues before summarizing.
  Exit code `1` means "issues found," not a CLI failure — only a genuinely
  unparseable/missing-binary/timeout result is treated as an error.
- No fallback if `SNYK_TOKEN`/`snyk` are missing — `security_agent` reports that
  plainly and skips the scan, rather than silently swapping in a different tool
  (which would make demo output inconsistent run to run).
- Both functions still take `state` as their LangGraph node signature but don't use
  it — they always scan the same `infra/terraform/` directory rather than a
  PR-specific diff (a real version would `git diff` the PR's `.tf` files into a temp
  dir first).
- Want the real HCP Terraform runs back (not just local `terraform plan`) without
  MCP? `agents/terraform_agent.py` used to call HCP's REST API directly via `httpx`
  for config-version upload — the same pattern extends to `create_run`/
  `get_run_details`.

## Hosting the worker in Docker

`temporal_worker.py` (not the CLI or web UI — see `.dockerignore`) runs from a
single-purpose image: Python 3.13-slim + `terraform`, `snyk` (standalone binary,
no Node/npm), and `uvx` baked in — the exact three tools "Local-only agents" and
`cost_agent`'s MCP integration need, nothing else.

```bash
make docker-build   # or: docker build -t agentic-devsecops-worker .
make docker-run     # or: docker run --rm --env-file .env agentic-devsecops-worker
```

Notes, all confirmed by actually building/running the image, not just written down:
- **Multi-arch.** `ARG TARGETARCH` (set automatically by Docker's builder) picks the
  right binary for both `linux/amd64` (most cloud targets — ECS/Fargate/EC2/most
  managed k8s node pools) and `linux/arm64` (Graviton, Apple Silicon dev machines).
  Verified both: `docker build .` (native arm64 on a Mac) and
  `docker buildx build --platform linux/amd64 .` (emulated) each produce a working
  image — confirmed `terraform`, `snyk`, `uvx`, and every `agents/*` import all work
  inside the container, not just that the binaries downloaded.
- **The AWS provider is baked in at build time** (`terraform init -backend=false`
  runs during the build, against the committed `.terraform.lock.hcl`) — only the
  image build needs network access for that; a cold container start doesn't hit
  `registry.terraform.io`.
- **No secrets in the image, ever.** `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`,
  `SNYK_TOKEN`, and (if you're pointed at Temporal Cloud rather than a local dev
  server) `TEMPORAL_ADDRESS`/`TEMPORAL_NAMESPACE`/`TEMPORAL_API_KEY` all come in at
  `docker run` time (`--env-file .env` here; your actual host's secrets mechanism —
  ECS task definition secrets, k8s `Secret`, etc. — in production).
- **This image needs a reachable Temporal server** — a local dev server on the same
  Docker host (`--network host` or `host.docker.internal:7233`, depending on your
  platform) for the fully-local setup this README defaults to, or a Temporal Cloud
  namespace if you've pointed `TEMPORAL_API_KEY` there instead; `docker run` on its
  own just gets you a worker polling nothing.

## Durable execution with Temporal

The web UI runs `graph.py`'s graph through Temporal
([`temporalio[langgraph]`](https://docs.temporal.io/develop/python/integrations/langgraph))
instead of calling `graph.astream()` directly. `main.py`'s CLI path is untouched —
same graph, same node functions, no Temporal involved. Two problems this actually
solves here, not just architecture for its own sake:

- **The human-approval pause is durable.** Before, it only survived as long as the
  Python process did (`MemorySaver` is in-memory). Now it's Temporal workflow state —
  kill and restart `temporal_worker.py` mid-approval and the pending decision is
  still there, because `human_approval_node`'s `interrupt()` resumes via a
  `submit_decision` signal, not an in-process call.
- **Flaky external calls get retried automatically.** Every agent (Docker/npx
  cold-starts, HCP Terraform polling, GitHub/Snyk/AWS MCP calls) runs as a Temporal
  Activity with `default_activity_options` in `temporal_worker.py` (150s timeout,
  3 attempts) — a transient failure retries on its own instead of surfacing
  immediately, the way it did calling these functions directly.

What actually changed to make this work:
- `determine_agents`/`route_by_risk` (the conditional-edge routers) became `async
  def` — Temporal requires routers to run inline in the workflow and be async. This
  in turn required `main.py` to switch from `graph.invoke()` to `await
  graph.ainvoke()`: LangGraph's sync `.invoke()` cannot call an async router at all
  (`TypeError: No synchronous function provided`), so the CLI path had to become
  async too, even though it uses no Temporal itself.
- Every other node function — all of `agents/*.py` plus `graph.py`'s own
  `synthesis_node`/`human_approval_node`/etc. — needed **no changes**. They're all
  plain sync `def`s (some internally calling `asyncio.run(...)`, e.g.
  `terraform_agent`), and Temporal's plugin runs sync nodes via
  `asyncio.to_thread(...)`, not inline in the Activity's own event loop — so a node
  that does its own `asyncio.run()` doesn't collide with one already running.
- `temporal_workflow.py` deliberately does not import anything from `graph.py`.
  Temporal's sandboxed workflow runner validates a workflow module's entire import
  chain for determinism, and `graph.py` pulls in `httpx` (via `agents/*`), which
  trips the sandbox's `urllib.request` restriction. The graph's registration name
  (`"devsecops"`) is duplicated as a plain string in both `temporal_workflow.py` and
  `temporal_worker.py` rather than shared via import.
- A raw `langgraph.types.Interrupt` object can't cross the wire to the browser as-is
  (Temporal's payload converter needs it serializable), so `temporal_workflow.py`
  unwraps it to a plain `{"interrupt": {...the actual dict...}}` before publishing.
- An activity that exhausts its retries (e.g. `synthesis_node` failing without
  `ANTHROPIC_API_KEY`) fails the whole Temporal workflow, not just that call — by
  default that exception would propagate silently past the client with no message.
  `temporal_workflow.py` publishes an `{"error": ...}` chunk before letting the
  workflow fail for real, so the UI still shows a clean error banner (Temporal's own
  history still correctly shows the run as `Failed`, which is useful to keep for
  real bugs — see it at localhost:8233).
- Resuming after approval re-subscribes to the same workflow's stream rather than
  replaying it from the start (which would re-render every earlier agent card and,
  worse, re-show the just-submitted approval prompt): the `interrupt` SSE event
  carries the stream's current `offset`, the frontend hands it back on `/api/approve`,
  and the server resumes from `offset + 1`.

## GitHub Actions PR trigger (CI path)

A third way to run the review, alongside the CLI and the web UI: a real PR
triggers it, with no human at a browser — and entirely locally, no cloud
account of any kind required.

```
PR opened/updated
  -> .github/workflows/pr-review.yml (GitHub Actions, self-hosted runner)
  -> trigger_workflow.py starts ci_workflow.py's PRReviewWorkflow on Temporal
  -> PRReviewWorkflow runs ci_graph.py: orchestrator -> agents (parallel, each a
     Temporal Activity) -> Claude synthesis -> post_summary (posts the review as
     a PR comment via the GitHub MCP)
```

This reuses every agent unchanged — same `agents/*.py` functions, same
Temporal-Activity durability/retry story as the web UI. What's different is
the tail: `ci_graph.py` drops `route_by_risk`/`auto_merge`/`human_approval`
entirely (see its docstring) and ends at `post_summary_node` instead — there's
no live UI for a human to approve/reject from, so a human just reviews and
merges normally via GitHub's own PR UI, informed by the posted comment.

**Why a separate graph/workflow instead of reusing `graph.py`/`DevSecOpsWorkflow`:**
an `interrupt()` with nothing subscribed to signal it back would leave the
workflow paused forever. `ci_graph.py`/`ci_workflow.py` are a deliberately
simpler pair for a path that never pauses; `graph.py`/`temporal_workflow.py`
are untouched.

**Why a self-hosted runner, not a GitHub-hosted one:** GitHub-hosted runners
are ephemeral cloud VMs — they can't reach a Temporal server on your laptop, or
anything else on your machine. A **self-hosted** runner is different: it's a
small agent process you register and run yourself, so the job it executes
literally runs as a process on your machine — right alongside
`temporal_worker.py` and `temporal server start-dev`. `trigger_workflow.py`
just connects to `localhost:7233` the same way `temporal_worker.py` does; no
Temporal credentials or cloud account needed anywhere in this path.

### Register a self-hosted runner

One-time setup, from your repo's GitHub page:

1. **Settings -> Actions -> Runners -> New self-hosted runner.** Pick your OS —
   GitHub shows you the exact download/config commands for your machine,
   including a short-lived registration token.
2. Run the commands it gives you, roughly:
   ```bash
   ./config.sh --url https://github.com/<owner>/<repo> --token <REGISTRATION_TOKEN>
   ./run.sh
   ```
3. Leave `./run.sh` running in its own terminal (terminal 4, alongside
   `temporal server start-dev`, `temporal_worker.py`, and the web UI if you're
   running that too) — that's the process GitHub Actions dispatches the
   `runs-on: self-hosted` job to.

That's the whole setup. No IAM roles, no secrets, no cloud account — opening a
real PR against this repo now runs the full pipeline on your own machine.

## How this maps to your diagram

| Diagram box | This project |
|---|---|
| Developer creates PR / PR triggers pipeline | CLI/web UI: `mock_data/sample_pr.json` + `load_pr()`. **CI path: real** — `.github/workflows/pr-review.yml` on `pull_request`, see above |
| Agentic AI Orchestrator reads PR, detects changed files | `orchestrator_node` in `graph.py` (shared by `ci_graph.py`) |
| Orchestrator selects relevant Agents | `determine_agents` conditional router |
| Agents run tools in parallel, collect results | `agents/` package — all four via real MCP servers (see below), most with a local/direct-API fallback |
| Results + RAG context sent to LLM | `synthesis_node` (RAG not implemented yet — see below) |
| LLM generates Root Cause, Risks, Recommendations | `ReviewOutput` structured output |
| AI posts Review in Pull Request | CLI: `format_pr_comment()` in `main.py` (prints only). **CI path: real** — `post_summary_node` in `agents/summary_agent.py` posts via the GitHub MCP's `add_issue_comment` |
| Risk Classification: Low/Medium/High/Critical | `route_by_risk` (currently binary: low vs. everything else — easy to extend to 4 branches). CI path skips routing/blocking entirely — see above |
| Human Approval / Auto Merge | CLI/web UI: `interrupt()` + `Command(resume=...)` — durable across a worker restart when run via Temporal. CI path: no blocking gate — a human reviews/merges via GitHub's own PR UI, informed by the posted comment |
| After Approval -> CI/CD -> Production | out of scope here; would be a real pipeline trigger after `final_decision` |

## Next steps toward the full diagram

Roughly in order of effort:

1. **More agents.** Add `kubernetes_agent`, `monitoring_agent` to `agents/` with real
   tool calls (kubectl, Azure Monitor, etc.) — MCP servers exist for several of these too.
2. **RAG knowledge base.** Add a node before `synthesis_node` that retrieves relevant
   docs (architecture standards, security policies) from a vector store — start with
   a local Chroma/FAISS index before paying for Azure AI Search.
3. **4-way risk routing.** Extend `route_by_risk` to return `auto_merge`,
   `team_lead_approval`, `security_approval`, or `cab_approval` per the diagram, each
   a separate `interrupt()`-based node with different approvers (web UI path only —
   see "GitHub Actions PR trigger" above for why the CI path deliberately skips this).
4. **Move off `temporal server start-dev` for anything beyond a demo.** It's
   SQLite-backed, single-process, no auth — fine for local dev/recording, but a real
   deployment needs either Temporal Cloud (`TEMPORAL_API_KEY` — see "Hosting the
   worker in Docker") or a genuinely self-hosted cluster (Postgres-backed
   `temporalio/docker-compose`, or the Helm chart for HA on k8s).

Done: real PR trigger (GitHub Actions, see above), real PR comment posting
(`post_summary_node`), fully local end-to-end path (CI included, via a
self-hosted runner — no cloud account required for any of it).

## Useful docs

- LangGraph concepts: https://langchain-ai.github.io/langgraph/concepts/
- Human-in-the-loop guide: https://langchain-ai.github.io/langgraph/how-tos/human_in_the_loop/
- `langchain-anthropic` structured output: https://python.langchain.com/docs/integrations/chat/anthropic/
- Temporal's LangGraph integration: https://docs.temporal.io/develop/python/integrations/langgraph
- Reference samples this project's Temporal code follows: [`langgraph_plugin/graph_api/human_in_the_loop`](https://github.com/temporalio/samples-python/tree/main/langgraph_plugin/graph_api/human_in_the_loop) (signals/queries for `interrupt()`), [`langgraph_plugin/graph_api/streaming`](https://github.com/temporalio/samples-python/tree/main/langgraph_plugin/graph_api/streaming) (Workflow Streams)
