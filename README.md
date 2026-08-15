# Agentic AI + DevSecOps — LangGraph

An agentic PR review pipeline built on LangGraph + Claude: an orchestrator routes
a changed-files list to the relevant agents, which run real tools (Terraform,
Snyk, GitHub Actions, AWS) in parallel; Claude synthesizes their findings into a
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

There's a second way to run this: a real PR triggers it via GitHub Actions, no
human at a terminal at all — see "GitHub Actions PR trigger (CI path)" below.

## Structure

| Path | What it is |
|---|---|
| `main.py` | CLI entry point — loads the PR, runs the graph directly (no Temporal), formats/prints the PR review comment, handles the human-approval pause/resume loop via `input()` |
| `graph.py` | The LangGraph itself: state, orchestrator, conditional fan-out/fan-in to agents, Claude synthesis node, risk-based routing, `interrupt()`-based human approval. Used directly by `main.py` — plain LangGraph, no Temporal. Also the source of the node functions/state `ci_graph.py` reuses for the CI path |
| `temporal_worker.py` | Hosts `ci_graph.py`'s nodes as Temporal Activities (retries, timeouts) — the CI path only; `main.py`'s graph never goes through Temporal |
| `agents/` | One module per agent — `terraform_agent.py`, `security_agent.py`, `pipeline_agent.py`, `cost_agent.py`, `generic_agent.py` (fallback, no tool), `summary_agent.py` (`post_summary_node` — posts the review as a PR comment, CI path only). Unchanged by the Temporal integration — same functions run as plain Python calls (CLI) or Temporal Activities (CI) |
| `infra/terraform/` | Sample Terraform — S3 bucket + an intentionally overly-permissive IAM policy (for `terraform_agent`/`security_agent`) + a `t3.micro` EC2 instance (gives `cost_agent`'s pricing estimate something with real cost weight to price out) |
| `mock_data/sample_pr.json` | Stand-in for a real GitHub webhook payload for the CLI path — the CI path (below) reads a real PR instead |
| `Dockerfile` | Image for hosting `temporal_worker.py` — `terraform`/`snyk`/`uvx` baked in, no secrets. See "Hosting the worker in Docker" below |
| `ci_graph.py` | The CI graph: reuses `graph.py`'s orchestrator/agents/synthesis nodes, but ends at a single `post_summary` node instead of risk-routing to auto-merge/human-approval — no human sits at a UI waiting on a signal |
| `ci_workflow.py` | `PRReviewWorkflow` — the CI path's Temporal Workflow. No signals/queries/stream, since `ci_graph.py` never interrupts |
| `trigger_workflow.py` | Run as a GitHub Actions step: reads the triggering PR from the Actions event payload, connects to Temporal (local dev server by default, same as `temporal_worker.py`), starts `PRReviewWorkflow`, waits for it, writes the result to the job summary |
| `.github/workflows/pr-review.yml` | Triggers `trigger_workflow.py` on every `pull_request` event; runs on a self-hosted runner (registered on your own machine) so it can reach a local Temporal server — no cloud account, no credentials stored in GitHub at all |

## MCP integrations

Four agents call real, official MCP servers instead of hitting APIs/CLIs directly.
`pipeline_agent`/`post_summary_node` (see below) are the only ones that are genuinely
*remotely hosted* by their provider — `terraform_agent`'s and `cost_agent`'s MCP
servers both run as local subprocesses (Docker, `uvx`) even though they're calling
real remote APIs.

| Agent | MCP server | Requires |
|---|---|---|
| `terraform_agent` | [hashicorp/terraform-mcp-server](https://github.com/hashicorp/terraform-mcp-server) (Docker, stdio) — `create_run`/`get_run_details` against an HCP Terraform workspace | `TFE_TOKEN`, `TFE_ORG`, `TFE_WORKSPACE` in `.env`; Docker running locally |
| `pipeline_agent` | GitHub's official **remote** MCP server at `api.githubcopilot.com/mcp/` — `actions_list` (`list_workflow_runs`) | `GITHUB_TOKEN` (PAT, `repo` scope) and `GITHUB_REPO` in `.env` |
| `post_summary_node` (`agents/summary_agent.py`) | Same GitHub remote MCP server — `add_issue_comment` | Same as above |
| `cost_agent` | AWS Labs' [Billing and Cost Management MCP server](https://awslabs.github.io/mcp/servers/billing-cost-management-mcp-server) (`uvx awslabs.billing-cost-management-mcp-server`, local stdio subprocess) — `cost-explorer` (`getCostAndUsage`) + `cost-optimization` (`list_recommendations`) | Real AWS credentials (`~/.aws` or `AWS_PROFILE`/`AWS_ACCESS_KEY_ID`); `uv`/`uvx` installed |

Notes:
- The Terraform MCP server has no tool to upload a configuration version, so
  `terraform_agent` pushes `infra/terraform/*.tf` to the workspace via the HCP Terraform
  REST API directly before calling `create_run` — everything after that (triggering the
  run, polling status, pulling the plan log) goes through MCP. `create_run` uses
  `run_type: "plan_only"` — it never applies.
- `terraform_agent` (like `security_agent` below) pipes the plan log through Claude
  (`agents/llm_utils.py`'s `summarize_tool_output()`) rather than dumping it raw —
  same "purely descriptive, no risk verdicts" scope either way (local CLI or MCP), so
  switching between them doesn't change what the summary looks like, only where the
  plan data comes from.
- **`terraform_agent` needs Docker running on whatever host runs `temporal_worker.py`
  as a plain process.** If you instead run the worker *inside* its own container (see
  "Hosting the worker in Docker" below), that's Docker-in-Docker — `docker run` inside
  a container needs the host's Docker socket mounted in (`-v
  /var/run/docker.sock:/var/run/docker.sock`), which isn't done by default here. Leave
  `TFE_TOKEN`/`TFE_ORG`/`TFE_WORKSPACE` unset to skip MCP and fall back to local
  `terraform plan` instead — no Docker needed at all in that mode.
- GitHub's remote MCP server only enables a default toolset (repos, issues, pull
  requests, ...) — Actions tools aren't in it. `pipeline_agent` requests them via the
  `X-MCP-Toolsets: actions` header.
- The AWS billing MCP server's `cost-explorer` tool requires `metrics`/`group_by` as
  JSON-encoded strings (`json.dumps([...])`), not plain lists — the tool's own examples
  are misleading here. Its Cost Explorer endpoint only works from `us-east-1`, regardless
  of which region your resources are in.
- `cost_agent`'s account-wide spend and optimization recommendations are about *existing*
  infrastructure — Cost Explorer works on actual billing history, and Cost Optimization
  Hub's recommendations need real usage history to build confidence (AWS's own numbers:
  1-3 days after first enabling it, up to 14 days of data for idle-resource detection) —
  neither can say anything about resources that don't exist yet. `cost_agent` reports the
  Cost Optimization Hub gap instead of failing when it isn't enrolled.
- **Pre-deployment cost estimate** (`_estimate_deployment_cost` in `agents/cost_agent.py`)
  is the one part of `cost_agent` that *can* speak to what this PR is about to create —
  it's a live AWS Price List API lookup (the `aws-pricing` tool on the same MCP server),
  not a usage-history question, so it works instantly regardless of what's actually
  deployed. This needed a real agentic tool-use loop rather than the one-shot
  `summarize_tool_output()` pattern every other agent uses: Claude has to decide which
  AWS service, which pricing attributes matter, and what filter values to use before it
  can get an actual price — `langgraph.prebuilt.create_react_agent` bound to the
  `aws-pricing` tool, given the raw `infra/terraform/*.tf` text, driving AWS's own
  suggested lookup order (`get_service_codes` → `get_service_attributes` →
  `get_attribute_values` → `get_pricing_from_api`) on its own. Verified live: correctly
  prices the `t3.micro` instance (~$7.59/mo) and correctly treats the IAM policy and S3
  public-access-block resource as free, without being told which is which.
- `cost_agent` runs alongside `terraform_agent` (routed on `.tf` changes) since cost
  impact is naturally tied to infra changes.

## Local-only agent (no MCP)

`security_agent` deliberately doesn't use MCP at all — it originally called an
npx-spawned MCP server (Snyk's), which meant anywhere you host `temporal_worker.py`
needs Node available. That's real hosting friction this project doesn't need, so it
was simplified to a single local-CLI path — real tool, real findings, no MCP protocol
layer, no subprocess-spawned server:

| Agent | Tool | Requires |
|---|---|---|
| `security_agent` | `snyk iac test --json` (subprocess, standalone binary — see install below) against `infra/terraform/` | `SNYK_TOKEN` in `.env`; `snyk` CLI on `PATH` |

Not a "dumb" CLI wrapper, though: it pipes raw tool output through Claude
(`agents/llm_utils.py`'s `summarize_tool_output()`, same `ChatAnthropic` model
`synthesis_node` uses) to turn a mechanical issues list into an actual risk narrative
— genuinely a small agent, not just a subprocess call. `terraform_agent` uses the same
helper for its own summarization (see "MCP integrations" above), whichever of its two
paths (MCP or local) produced the plan.

**Installing the Snyk CLI** (no Node/npm — it's a standalone binary):
```bash
curl -Lo /usr/local/bin/snyk https://downloads.snyk.io/cli/latest/snyk-linux   # or snyk-macos-arm64, etc.
chmod +x /usr/local/bin/snyk
```
Bake that into whatever image/host runs `temporal_worker.py` — GitHub Actions runners
never need it, since `security_agent` runs as a Temporal Activity inside the worker
process, not inside `trigger_workflow.py`'s job.

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
- Both `terraform_agent` and `security_agent` still take `state` as their LangGraph
  node signature but don't use it — they always scan the same `infra/terraform/`
  directory rather than a PR-specific diff (a real version would `git diff` the PR's
  `.tf` files into a temp dir first).
- `terraform_agent` and `security_agent` intentionally have different scopes now:
  `terraform_agent` only describes what a plan changes (resources, config) —
  no risk assessment, severity ratings, or merge/block verdicts. All of that is
  `security_agent`'s job, so the same finding isn't independently (and sometimes
  inconsistently) re-judged by two agents.

## Hosting the worker in Docker

`temporal_worker.py` (not `main.py`'s CLI path — see `.dockerignore`) runs from a
single-purpose image: Python 3.13-slim + `terraform`, `snyk` (standalone binary,
no Node/npm), and `uvx` baked in — everything `terraform_agent`'s *local* fallback,
`security_agent`, and `cost_agent`'s MCP integration need.

**Not included: Docker-in-Docker for `terraform_agent`'s MCP path.** If
`TFE_TOKEN`/`TFE_ORG`/`TFE_WORKSPACE` are set, `terraform_agent` shells out to
`docker run hashicorp/terraform-mcp-server` — which doesn't work from inside this
image as-is, since there's no Docker daemon in it. Either leave those three env vars
unset (falls back to local `terraform plan`, no Docker needed at all) or mount the
host's Docker socket in at `docker run` time (`-v /var/run/docker.sock:/var/run/docker.sock`)
if you want the MCP path working from inside the container too.

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

The CI path (below) runs `ci_graph.py` through Temporal
([`temporalio[langgraph]`](https://docs.temporal.io/develop/python/integrations/langgraph))
instead of calling `graph.astream()` directly. `main.py`'s CLI path is untouched —
plain LangGraph, no Temporal involved, same node functions. Two problems Temporal
actually solves here, not just architecture for its own sake:

- **The whole run survives a worker crash or restart.** Temporal tracks workflow
  progress as durable history, not in-process state — kill and restart
  `temporal_worker.py` mid-run and it resumes from wherever it left off instead of
  starting over.
- **Flaky external calls get retried automatically.** Every agent (Docker/npx
  cold-starts, HCP Terraform polling, GitHub/Snyk/AWS MCP calls) runs as a Temporal
  Activity with `default_activity_options` in `temporal_worker.py` (150s timeout,
  3 attempts) — a transient failure retries on its own instead of surfacing
  immediately, the way it did calling these functions directly.

What actually changed to make this work:
- `determine_agents` (the conditional-edge router `ci_graph.py` reuses from
  `graph.py`) is `async def` — Temporal requires routers to run inline in the
  workflow and be async. This in turn required `main.py` to switch from
  `graph.invoke()` to `await graph.ainvoke()`: LangGraph's sync `.invoke()` cannot
  call an async router at all (`TypeError: No synchronous function provided`), so
  the CLI path had to become async too, even though it uses no Temporal itself.
- Every node function — all of `agents/*.py` plus `graph.py`'s
  `orchestrator_node`/`synthesis_node` and `agents/summary_agent.py`'s
  `post_summary_node` — needed **no changes** to run as Activities. They're all
  plain sync `def`s (some internally calling `asyncio.run(...)`, e.g.
  `terraform_agent`), and Temporal's plugin runs sync nodes via
  `asyncio.to_thread(...)`, not inline in the Activity's own event loop — so a node
  that does its own `asyncio.run()` doesn't collide with one already running.
- `ci_workflow.py` deliberately does not import anything from `graph.py` or
  `ci_graph.py`. Temporal's sandboxed workflow runner validates a workflow module's
  entire import chain for determinism, and both of those pull in `httpx` (via
  `agents/*`), which trips the sandbox's `urllib.request` restriction. The graph's
  registration name (`"devsecops-ci"`) is duplicated as a plain string in
  `ci_workflow.py` and `temporal_worker.py` rather than shared via import.
- An activity that exhausts its retries (e.g. `synthesis_node` failing without
  `ANTHROPIC_API_KEY`) fails the whole Temporal workflow — Temporal's own history
  correctly shows the run as `Failed` (see it at localhost:8233), and
  `trigger_workflow.py` catches `WorkflowFailureError` on the GitHub Actions side
  and reports it clearly instead of the job just silently failing.

## GitHub Actions PR trigger (CI path)

A second way to run the review, alongside the CLI: a real PR triggers it, with
no human watching at all — and entirely locally, no cloud account of any kind
required.

```
PR opened/updated
  -> .github/workflows/pr-review.yml (GitHub Actions, self-hosted runner)
  -> trigger_workflow.py starts ci_workflow.py's PRReviewWorkflow on Temporal
  -> PRReviewWorkflow runs ci_graph.py: orchestrator -> agents (parallel, each a
     Temporal Activity) -> Claude synthesis -> post_summary (posts the review as
     a PR comment via the GitHub MCP)
```

This reuses every agent unchanged — same `agents/*.py` functions used by the
CLI path, just running as Temporal Activities instead of plain calls. What's
different is the tail: `ci_graph.py` drops `route_by_risk`/`auto_merge`/
`human_approval` entirely (see its docstring) and ends at `post_summary_node`
instead — there's no human at a terminal to approve/reject from, so a human
just reviews and
merges normally via GitHub's own PR UI, informed by the posted comment.

**Why a separate graph/workflow instead of reusing `graph.py`'s risk-routing tail:**
an `interrupt()` with nothing subscribed to signal it back would leave the
workflow paused forever — there's no browser, no terminal, nothing watching in
this path. `ci_graph.py`/`ci_workflow.py` are a deliberately simpler pair for a
path that never pauses; `graph.py`'s own `graph` (used by `main.py`) is untouched.

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
3. Leave `./run.sh` running in its own terminal (alongside `temporal server
   start-dev` and `temporal_worker.py`) — that's the process GitHub Actions
   dispatches the `runs-on: self-hosted` job to.

That's the whole setup. No IAM roles, no secrets, no cloud account — opening a
real PR against this repo now runs the full pipeline on your own machine.

## How this maps to your diagram

| Diagram box | This project |
|---|---|
| Developer creates PR / PR triggers pipeline | CLI: `mock_data/sample_pr.json` + `load_pr()`. **CI path: real** — `.github/workflows/pr-review.yml` on `pull_request`, see above |
| Agentic AI Orchestrator reads PR, detects changed files | `orchestrator_node` in `graph.py` (shared by `ci_graph.py`) |
| Orchestrator selects relevant Agents | `determine_agents` conditional router |
| Agents run tools in parallel, collect results | `agents/` package — all four via real MCP servers (see below), most with a local/direct-API fallback |
| Results + RAG context sent to LLM | `synthesis_node` (RAG not implemented yet — see below) |
| LLM generates Root Cause, Risks, Recommendations | `ReviewOutput` structured output |
| AI posts Review in Pull Request | CLI: `format_pr_comment()` in `main.py` (prints only). **CI path: real** — `post_summary_node` in `agents/summary_agent.py` posts via the GitHub MCP's `add_issue_comment` |
| Risk Classification: Low/Medium/High/Critical | `route_by_risk` (currently binary: low vs. everything else — easy to extend to 4 branches). CI path skips routing/blocking entirely — see above |
| Human Approval / Auto Merge | CLI: `interrupt()` + `Command(resume=...)`, resolved via a terminal `input()` prompt — in-process only, not durable. CI path: no blocking gate at all — a human reviews/merges via GitHub's own PR UI, informed by the posted comment |
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
   a separate `interrupt()`-based node with different approvers (CLI path only —
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
