import asyncio
import json
import os

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv()

GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"


async def _fetch_workflow_runs(owner: str, repo: str, token: str) -> str:
    client = MultiServerMCPClient({
        "github": {
            "transport": "streamable_http",
            "url": GITHUB_MCP_URL,
            # "actions" isn't in the server's default toolset — has to be requested explicitly.
            "headers": {"Authorization": f"Bearer {token}", "X-MCP-Toolsets": "actions"},
        }
    })
    tools = {t.name: t for t in await client.get_tools()}
    result = await tools["actions_list"].ainvoke({
        "method": "list_workflow_runs",
        "owner": owner,
        "repo": repo,
        "per_page": 3,
    })
    return result[0]["text"] if isinstance(result, list) else str(result)


def pipeline_agent(state) -> dict:
    repo_full = os.environ.get("GITHUB_REPO")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo_full or not token:
        return {"agent_results": [
            "Pipeline Agent: GITHUB_REPO/GITHUB_TOKEN not configured — skipping GitHub MCP check."
        ]}
    owner, repo = repo_full.split("/", 1)

    print(f"[pipeline_agent] fetching recent workflow runs for {repo_full} via GitHub MCP...")

    try:
        raw = asyncio.run(_fetch_workflow_runs(owner, repo, token))
    except Exception as e:
        return {"agent_results": [f"Pipeline Agent: GitHub MCP call failed ({e})."]}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"agent_results": [f"Pipeline Agent: could not parse GitHub MCP response: {raw[:300]}"]}

    runs = data.get("workflow_runs", data if isinstance(data, list) else [])[:3]
    if not runs:
        return {"agent_results": [f"Pipeline Agent: no workflow runs found for {repo_full}."]}

    lines = [f"Pipeline Agent: last {len(runs)} run(s) for {repo_full} (via GitHub MCP):"]
    for run in runs:
        status = run.get("conclusion") or run.get("status")
        lines.append(f"  - {run.get('name')} #{run.get('run_number')}: {status}")
    return {"agent_results": ["\n".join(lines)]}
