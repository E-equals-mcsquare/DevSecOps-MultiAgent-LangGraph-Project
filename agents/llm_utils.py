"""Shared helper for agents/*.py's local (non-MCP) tool integrations: pipes
raw CLI output through Claude for interpretation instead of regex/truncation
formatting — used by terraform_agent.py and security_agent.py, both of which
run their tool (`terraform`, `checkov`) as a plain subprocess with no MCP
server involved (see README.md's "Local-only agents" section)."""

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic

load_dotenv()

_llm = ChatAnthropic(model="claude-sonnet-4-5-20250929", temperature=0)


def summarize_tool_output(agent_label: str, instructions: str, raw_output: str) -> str:
    prompt = (
        f"You are the {agent_label} in a DevSecOps PR review pipeline.\n"
        f"{instructions}\n\n"
        f"Raw tool output:\n{raw_output[:8000]}"
    )
    response = _llm.invoke(prompt)
    text = response.content if isinstance(response.content, str) else str(response.content)
    return f"{agent_label}: {text.strip()}"
