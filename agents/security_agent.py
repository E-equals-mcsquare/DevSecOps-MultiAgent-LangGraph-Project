import json
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

from .llm_utils import summarize_tool_output

load_dotenv()

TERRAFORM_DIR = Path(__file__).resolve().parent.parent / "infra" / "terraform"


def _run_snyk() -> subprocess.CompletedProcess:
    # snyk exits 1 when issues are found (not an error) and 0 when clean —
    # only FileNotFoundError/TimeoutExpired below are treated as real failures.
    return subprocess.run(
        ["snyk", "iac", "test", str(TERRAFORM_DIR), "--json"],
        capture_output=True,
        text=True,
        timeout=180,
    )


def security_agent(state) -> dict:
    if not os.environ.get("SNYK_TOKEN"):
        return {"agent_results": [
            "Security Agent: SNYK_TOKEN not configured — skipping Snyk IaC scan."
        ]}

    print(f"[security_agent] snyk iac test on {TERRAFORM_DIR} (local CLI)...")

    try:
        result = _run_snyk()
    except FileNotFoundError:
        return {"agent_results": [
            "Security Agent: `snyk` CLI not found — see README's \"Local-only agents\" section for install."
        ]}
    except subprocess.TimeoutExpired:
        return {"agent_results": ["Security Agent: snyk iac test timed out."]}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        tail = (result.stdout + result.stderr).strip()[-500:]
        return {"agent_results": [f"Security Agent: could not parse snyk output:\n{tail}"]}

    reports = data if isinstance(data, list) else [data]
    failed = [
        {**issue, "file": r.get("targetFile", "?")}
        for r in reports
        for issue in r.get("infrastructureAsCodeIssues", [])
    ]

    if not failed:
        return {"agent_results": ["Security Agent: Snyk IaC scan clean — no issues found."]}

    findings = "\n".join(
        f"[{i['severity']}] {i['title']} — {i['msg']} ({i['file']}:{i.get('lineNumber', '?')})"
        for i in failed
    )
    summary = summarize_tool_output(
        "Security Agent",
        f"A Snyk IaC scan found {len(failed)} issue(s) below. Summarize the security risk in plain "
        "language, highlighting the most severe issues and why they matter.",
        findings,
    )
    return {"agent_results": [summary]}
