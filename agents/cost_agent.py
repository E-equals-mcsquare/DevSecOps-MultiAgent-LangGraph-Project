import asyncio
import json
import os
from datetime import date, timedelta

from langchain_mcp_adapters.client import MultiServerMCPClient

COST_LOOKBACK_DAYS = 30


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

    return {"agent_results": [f"{usage_summary}\n{rec_summary}"]}


def cost_agent(state) -> dict:
    try:
        return asyncio.run(_mcp_cost_agent())
    except Exception as e:
        return {"agent_results": [f"Cost Agent: AWS Billing MCP call failed ({e})."]}
