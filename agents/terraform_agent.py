import asyncio
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

from .llm_utils import summarize_tool_output

load_dotenv()

TERRAFORM_DIR = Path(__file__).resolve().parent.parent / "infra" / "terraform"
TFE_ADDRESS = "https://app.terraform.io"
TERRAFORM_MCP_IMAGE = "hashicorp/terraform-mcp-server:1.2.0"
_TERMINAL_RUN_STATUSES = {"planned", "planned_and_finished", "errored", "canceled", "discarded"}

_SUMMARY_INSTRUCTIONS = (
    "Factually summarize what infrastructure changes this Terraform plan makes: which resources "
    "are added/changed/destroyed, and their key configuration (names, types, notable attributes). "
    "Purely descriptive — no risk assessment, severity ratings, or merge/block recommendations; "
    "that's Security Agent's job, not yours. 3-5 sentences."
)


def _run_terraform(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["terraform", *args],
        cwd=TERRAFORM_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _local_terraform_agent() -> dict:
    print(f"[terraform_agent] plan / validate / drift-check on {TERRAFORM_DIR} (local CLI)...")

    try:
        init = _run_terraform("init", "-backend=false", "-input=false")
    except FileNotFoundError:
        return {"agent_results": ["Terraform Agent: `terraform` CLI not found on PATH."]}
    except subprocess.TimeoutExpired:
        return {"agent_results": ["Terraform Agent: `terraform init` timed out."]}
    if init.returncode != 0:
        return {"agent_results": [f"Terraform Agent: init failed:\n{init.stderr.strip()}"]}

    validate = _run_terraform("validate", "-no-color")
    if validate.returncode != 0:
        return {"agent_results": [
            f"Terraform Agent: validate failed:\n{(validate.stdout + validate.stderr).strip()}"
        ]}

    try:
        plan = _run_terraform("plan", "-no-color", "-input=false")
    except subprocess.TimeoutExpired:
        return {"agent_results": ["Terraform Agent: validate passed. `terraform plan` timed out."]}
    if plan.returncode != 0:
        last_line = plan.stderr.strip().splitlines()[-1] if plan.stderr.strip() else "unknown error"
        return {"agent_results": [
            f"Terraform Agent: validate passed. Plan could not run ({last_line}) "
            "— likely no AWS credentials configured in this environment."
        ]}

    summary = summarize_tool_output("Terraform Agent", _SUMMARY_INSTRUCTIONS, plan.stdout)
    return {"agent_results": [summary]}


def _upload_configuration_version(org: str, workspace: str, headers: dict) -> str:
    """The terraform-mcp-server has no tool for this, so it's done directly against
    the HCP Terraform API: create_run (via MCP) needs a workspace that already has a
    configuration version, so we push the current infra/terraform/*.tf files first."""
    ws = httpx.get(
        f"{TFE_ADDRESS}/api/v2/organizations/{org}/workspaces/{workspace}",
        headers=headers, timeout=30,
    )
    ws.raise_for_status()
    workspace_id = ws.json()["data"]["id"]

    cv = httpx.post(
        f"{TFE_ADDRESS}/api/v2/workspaces/{workspace_id}/configuration-versions",
        headers=headers,
        json={"data": {"type": "configuration-versions", "attributes": {"auto-queue-runs": False}}},
        timeout=30,
    )
    cv.raise_for_status()
    cv_data = cv.json()["data"]
    upload_url = cv_data["attributes"]["upload-url"]
    cv_id = cv_data["id"]

    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        with tarfile.open(tmp.name, "w:gz") as tar:
            for path in TERRAFORM_DIR.glob("*.tf"):
                tar.add(path, arcname=path.name)
        tmp.seek(0)
        httpx.put(upload_url, content=tmp.read(), timeout=30).raise_for_status()

    for _ in range(15):
        status = httpx.get(
            f"{TFE_ADDRESS}/api/v2/configuration-versions/{cv_id}", headers=headers, timeout=30
        ).json()["data"]["attributes"]["status"]
        if status == "uploaded":
            return cv_id
        if status == "errored":
            raise RuntimeError("configuration version upload errored")
        time.sleep(2)
    raise TimeoutError("configuration version never reached 'uploaded'")


def _fetch_plan_log(plan_id: str, headers: dict) -> str:
    plan = httpx.get(f"{TFE_ADDRESS}/api/v2/plans/{plan_id}", headers=headers, timeout=30)
    plan.raise_for_status()
    attrs = plan.json()["data"]["attributes"]
    log_url = attrs.get("log-read-url")
    if not log_url:
        return ""
    log = httpx.get(log_url, timeout=30)
    return log.text if log.status_code == 200 else ""


async def _mcp_terraform_agent(org: str, workspace: str, token: str) -> dict:
    print(f"[terraform_agent] plan via HCP Terraform MCP (workspace {org}/{workspace})...")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/vnd.api+json"}

    try:
        _upload_configuration_version(org, workspace, headers)
    except (httpx.HTTPError, RuntimeError, TimeoutError) as e:
        return {"agent_results": [f"Terraform Agent: could not upload config to HCP Terraform ({e})."]}

    client = MultiServerMCPClient({
        "terraform": {
            "transport": "stdio",
            "command": "docker",
            "args": [
                "run", "-i", "--rm",
                "-e", "TFE_TOKEN", "-e", "TFE_ADDRESS", "-e", "ENABLE_TF_OPERATIONS",
                TERRAFORM_MCP_IMAGE, "stdio",
            ],
            "env": {"TFE_TOKEN": token, "TFE_ADDRESS": TFE_ADDRESS, "ENABLE_TF_OPERATIONS": "true"},
        }
    })
    tools = {t.name: t for t in await client.get_tools()}

    run_result = await tools["create_run"].ainvoke({
        "terraform_org_name": org,
        "workspace_name": workspace,
        "run_type": "plan_only",
        "message": "agentic-devsecops terraform_agent",
    })
    run_text = run_result[0]["text"] if isinstance(run_result, list) else str(run_result)
    match = re.search(r'"id":"(run-[A-Za-z0-9]+)"', run_text)
    if not match:
        return {"agent_results": [f"Terraform Agent: could not parse run ID from MCP response: {run_text[:300]}"]}
    run_id = match.group(1)

    status = None
    plan_id = None
    for _ in range(30):
        details_result = await tools["get_run_details"].ainvoke({"run_id": run_id})
        details_text = details_result[0]["text"] if isinstance(details_result, list) else str(details_result)
        details = json.loads(details_text)["data"]
        status = details["attributes"]["status"]
        plan_id = details["relationships"].get("plan", {}).get("data", {}).get("id")
        if status in _TERMINAL_RUN_STATUSES:
            break
        await asyncio.sleep(4)
    else:
        return {"agent_results": [f"Terraform Agent: HCP Terraform run {run_id} did not finish planning in time."]}

    base = f"HCP Terraform run {run_id} (workspace {org}/{workspace}) — status: {status}."
    log_text = ""
    if plan_id:
        try:
            log_text = _fetch_plan_log(plan_id, headers)
        except httpx.HTTPError:
            log_text = ""

    if not log_text:
        return {"agent_results": [f"Terraform Agent: {base}"]}

    summary = summarize_tool_output("Terraform Agent", f"{_SUMMARY_INSTRUCTIONS}\n\n({base})", log_text)
    return {"agent_results": [summary]}


def terraform_agent(state) -> dict:
    org = os.environ.get("TFE_ORG")
    workspace = os.environ.get("TFE_WORKSPACE")
    token = os.environ.get("TFE_TOKEN")
    if org and workspace and token:
        return asyncio.run(_mcp_terraform_agent(org, workspace, token))
    return _local_terraform_agent()
