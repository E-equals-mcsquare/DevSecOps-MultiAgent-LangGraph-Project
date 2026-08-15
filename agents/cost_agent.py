import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

COST_LOOKBACK_DAYS = 30
TERRAFORM_DIR = Path(__file__).resolve().parent.parent / "infra" / "terraform"

_pricing_llm = ChatAnthropic(model="claude-sonnet-4-5-20250929", temperature=0)


def _mcp_env() -> dict:
    # Cost Explorer's API is only served from us-east-1, regardless of where resources live.
    env = {"AWS_REGION": "us-east-1"}
    profile = os.environ.get("AWS_PROFILE")
    if profile:
        env["AWS_PROFILE"] = profile
    return env


def _extract_text(result) -> str:
    return result[0]["text"] if isinstance(result, list) else str(result)


def _summarize_usage(text: str, start: date, end: date) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return f"Cost Agent: could not parse cost-and-usage response: {text[:300]}"

    if payload.get("status") != "success":
        return f"Cost Agent: cost-and-usage lookup failed — {payload.get('message', payload)}"

    totals: dict[str, float] = {}
    for period in payload["data"].get("ResultsByTime", []):
        for group in period.get("Groups", []):
            service = group["Keys"][0] if group.get("Keys") else "Unknown"
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            totals[service] = totals.get(service, 0.0) + amount

    grand_total = sum(totals.values())
    top = [(s, a) for s, a in sorted(totals.items(), key=lambda kv: kv[1], reverse=True) if a > 0][:5]

    lines = [f"Cost Agent: ${grand_total:.4f} total spend, {start.isoformat()} to {end.isoformat()}."]
    for service, amount in top:
        lines.append(f"  - {service}: ${amount:.4f}")
    return "\n".join(lines)


def _summarize_recommendations(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return f"Optimization: could not parse response: {text[:300]}"

    if payload.get("status") != "success":
        return f"Optimization: unavailable — {payload.get('message', 'unknown error')}"

    recs = payload["data"].get("items") or payload["data"].get("recommendations") or []
    if not recs:
        return "Optimization: no cost-saving recommendations found."

    lines = [f"Optimization: {len(recs)} recommendation(s) from Cost Optimization Hub:"]
    for rec in recs[:5]:
        savings = rec.get("estimatedMonthlySavings", rec.get("estimatedSavingsAmount", "?"))
        lines.append(f"  - {rec.get('resourceType', '?')} ({rec.get('actionType', '?')}): ~${savings}/mo")
    return "\n".join(lines)


async def _estimate_deployment_cost(pricing_tool) -> str:
    """Pre-deployment cost estimate — how much would *this PR's* Terraform resources
    cost monthly, before anything is actually created. Unlike Cost Explorer/Cost
    Optimization Hub above (real billing history, needs days of usage data), this is
    a catalog lookup via AWS's Price List API, so it works instantly regardless of
    what's actually deployed. Needs a real agentic tool-use loop, not a single
    summarize_tool_output() call: the model has to decide which service, which
    pricing attributes matter, and what filter values to use before it can call
    get_pricing_from_api — that's what aws-pricing's own tool description says to do,
    in that order (get_service_codes -> get_service_attributes -> get_attribute_values
    -> get_pricing_from_api)."""
    tf_text = "\n\n".join(
        f"# {path.name}\n{path.read_text()}" for path in sorted(TERRAFORM_DIR.glob("*.tf"))
    )
    agent = create_react_agent(_pricing_llm, tools=[pricing_tool])
    prompt = (
        "Estimate the monthly AWS cost (region us-east-1, on-demand pricing) of deploying "
        "the Terraform resources below. Use the aws-pricing tool in this order: "
        "get_service_codes, then get_service_attributes, then get_attribute_values, then "
        "get_pricing_from_api. Skip that lookup entirely for resources with no direct cost "
        "(IAM policies, S3 public-access-block settings, data sources, etc). Give a short "
        "per-resource line and a total estimated monthly cost. Be concise — a few sentences, "
        "not a report. Your final reply must start directly with the estimate itself — no "
        "conversational preamble like \"I have the information I need\" or \"let me calculate\".\n\n"
        f"{tf_text}"
    )
    result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]})
    content = result["messages"][-1].content
    return content if isinstance(content, str) else str(content)


async def _mcp_cost_agent() -> dict:
    end = date.today()
    start = end - timedelta(days=COST_LOOKBACK_DAYS)

    print(f"[cost_agent] cost-and-usage + optimization recommendations via AWS Billing MCP "
          f"({start.isoformat()} to {end.isoformat()})...")

    client = MultiServerMCPClient({
        "cost": {
            "transport": "stdio",
            "command": "uvx",
            "args": ["awslabs.billing-cost-management-mcp-server@latest"],
            "env": _mcp_env(),
        }
    })
    tools = {t.name: t for t in await client.get_tools()}

    usage_result = await tools["cost-explorer"].ainvoke({
        "operation": "getCostAndUsage",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "granularity": "DAILY",
        "metrics": json.dumps(["UnblendedCost"]),
        "group_by": json.dumps([{"Type": "DIMENSION", "Key": "SERVICE"}]),
    })
    usage_summary = _summarize_usage(_extract_text(usage_result), start, end)

    rec_result = await tools["cost-optimization"].ainvoke({
        "operation": "list_recommendations",
        "max_results": 5,
    })
    rec_summary = _summarize_recommendations(_extract_text(rec_result))

    print("[cost_agent] estimating monthly cost of infra/terraform/*.tf via aws-pricing + Claude...")
    try:
        estimate = await _estimate_deployment_cost(tools["aws-pricing"])
        pricing_summary = f"Pre-deployment cost estimate:\n{estimate}"
    except Exception as e:
        pricing_summary = f"Pre-deployment cost estimate: unavailable ({e})."

    return {"agent_results": [f"{usage_summary}\n{rec_summary}\n\n{pricing_summary}"]}


def cost_agent(state) -> dict:
    try:
        return asyncio.run(_mcp_cost_agent())
    except Exception as e:
        return {"agent_results": [f"Cost Agent: AWS Billing MCP call failed ({e})."]}
