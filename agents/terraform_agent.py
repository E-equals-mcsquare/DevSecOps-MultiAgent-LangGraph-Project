import subprocess
from pathlib import Path

from .llm_utils import summarize_tool_output

TERRAFORM_DIR = Path(__file__).resolve().parent.parent / "infra" / "terraform"


def _run_terraform(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["terraform", *args],
        cwd=TERRAFORM_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )


def terraform_agent(state) -> dict:
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

    summary = summarize_tool_output(
        "Terraform Agent",
        "Summarize what infrastructure changes this Terraform plan makes (resources added/changed/"
        "destroyed), and call out anything risky — public exposure, loosened permissions, destructive "
        "changes. 3-5 sentences.",
        plan.stdout,
    )
    return {"agent_results": [summary]}
