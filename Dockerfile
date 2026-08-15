# temporal_worker.py's image: hosts graph.py + ci_graph.py's nodes as Temporal
# Activities (see README.md "Durable execution with Temporal" / "GitHub Actions
# PR trigger"). Only what the worker needs — not the CLI (main.py) or web UI
# (web/), which would need their own image/target if containerized too.
FROM python:3.13-slim

ARG TARGETARCH
ARG TERRAFORM_VERSION=1.15.8

# terraform_agent needs `terraform`; security_agent needs `snyk` (standalone
# binary — no Node/npm anywhere in this image); cost_agent needs `uvx` (spawns
# the AWS Billing MCP server locally). pipeline_agent/post_summary_node need
# neither — GitHub's MCP server is remote (plain HTTPS, no extra binary).
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && curl -fsSL -o /tmp/terraform.zip \
       "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" \
    && unzip /tmp/terraform.zip -d /usr/local/bin && rm /tmp/terraform.zip \
    && curl -fsSL -o /usr/local/bin/snyk \
       "https://downloads.snyk.io/cli/latest/snyk-linux$( [ "$TARGETARCH" = "arm64" ] && echo -arm64 )" \
    && chmod +x /usr/local/bin/snyk \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && apt-get purge -y unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY graph.py ci_graph.py temporal_workflow.py ci_workflow.py temporal_worker.py ./
COPY agents/ agents/
COPY infra/ infra/

# Bakes the AWS provider into the image so terraform_agent's `terraform init`
# doesn't hit registry.terraform.io on every cold container start — only this
# build step needs network access for it.
RUN cd infra/terraform && terraform init -backend=false -input=false

# No secrets baked in here on purpose — ANTHROPIC_API_KEY, GITHUB_TOKEN,
# SNYK_TOKEN, TEMPORAL_ADDRESS/NAMESPACE/API_KEY, AWS credentials all come in
# at `docker run` time (--env-file .env, or your host's secrets mechanism).
CMD ["python3", "temporal_worker.py"]
