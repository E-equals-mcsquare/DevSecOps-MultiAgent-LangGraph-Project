const AGENT_LABELS = {
  terraform_agent: "Terraform Agent",
  security_agent: "Security Agent",
  pipeline_agent: "Pipeline Agent",
  cost_agent: "Cost Agent",
  generic_agent: "Generic Agent",
};

const el = (id) => document.getElementById(id);
const runBtn = el("run-btn");
const btnLabel = runBtn.querySelector(".btn-label");
const btnSpinner = runBtn.querySelector(".btn-spinner");

let currentThreadId = null;
let currentOffset = 0;

async function fetchPr() {
  const res = await fetch("/api/pr");
  const pr = await res.json();
  renderPr(pr);
}

function renderPr(pr) {
  el("pr-number").textContent = `#${pr.pr_number}`;
  el("pr-title").textContent = pr.pr_title;
  el("pr-author").textContent = pr.author;
  const filesEl = el("pr-files");
  filesEl.innerHTML = "";
  for (const f of pr.changed_files) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = f;
    filesEl.appendChild(chip);
  }
  el("pr-card").hidden = false;
}

function setDot(stageId, status) {
  const dot = document.querySelector(`#${stageId} .dot`);
  dot.classList.remove("running", "done", "error");
  if (status) dot.classList.add(status);
}

function resetPipeline() {
  setDot("stage-orchestrator", null);
  setDot("stage-agents", null);
  setDot("stage-synthesis", null);
  setDot("stage-decision", null);
  el("agent-grid").innerHTML = "";
  el("synthesis-card").hidden = true;
  el("synthesis-placeholder").hidden = false;
  el("synthesis-placeholder").textContent = "Waiting for agent findings…";
  el("approval-card").hidden = true;
  el("decision-banner").hidden = true;
  el("decision-input").value = "";
}

function upsertAgentCard(name, resultLines) {
  const grid = el("agent-grid");
  let card = grid.querySelector(`[data-agent="${name}"]`);
  if (!card) {
    card = document.createElement("div");
    card.className = "agent-card";
    card.dataset.agent = name;
    card.innerHTML = `
      <div class="agent-card-head">
        <span class="agent-status-dot"></span>
        <span class="agent-name">${AGENT_LABELS[name] || name}</span>
      </div>
      <div class="agent-output"></div>
    `;
    grid.appendChild(card);
  }
  card.querySelector(".agent-status-dot").classList.add("done");
  card.querySelector(".agent-output").textContent = (resultLines || []).join("\n\n");
}

function renderSynthesis(update) {
  el("synthesis-placeholder").hidden = true;
  const card = el("synthesis-card");
  card.hidden = false;
  const badge = el("risk-badge");
  const risk = (update.risk_level || "unknown").toLowerCase();
  badge.textContent = risk;
  badge.className = `risk-badge ${risk}`;
  el("root-cause").textContent = update.root_cause || "";
  const list = el("recommendations");
  list.innerHTML = "";
  for (const r of update.recommendations || []) {
    const li = document.createElement("li");
    li.textContent = r;
    list.appendChild(li);
  }
}

function showDecisionBanner(text, kind) {
  const banner = el("decision-banner");
  banner.hidden = false;
  banner.className = `decision-banner ${kind}`;
  banner.textContent = text;
  el("approval-card").hidden = true;
}

function showApprovalCard(payload) {
  currentThreadId = payload.thread_id || currentThreadId;
  currentOffset = payload.offset ?? currentOffset;
  el("approval-card").hidden = false;
  setDot("stage-decision", "running");
}

function showErrorBanner(message) {
  // Fan-in means every agent already finished by the time anything downstream can error.
  if (el("agent-grid").children.length) setDot("stage-agents", "done");
  const failedStage = el("synthesis-card").hidden ? "stage-synthesis" : "stage-decision";
  setDot(failedStage, "error");
  const banner = el("decision-banner");
  banner.hidden = false;
  banner.className = "decision-banner error";
  banner.textContent = `⚠ ${message}`;
}

function handleNodeEvent(node, update) {
  if (node === "orchestrator") {
    setDot("stage-orchestrator", "done");
    setDot("stage-agents", "running");
  } else if (node in AGENT_LABELS) {
    upsertAgentCard(node, update.agent_results);
  } else if (node === "synthesis") {
    setDot("stage-agents", "done");
    setDot("stage-synthesis", "done");
    renderSynthesis(update);
  } else if (node === "auto_merge") {
    setDot("stage-decision", "done");
    showDecisionBanner("✔ Risk = low → auto-merged.", "merged");
  } else if (node === "human_approval") {
    setDot("stage-decision", "done");
    showDecisionBanner(`Final decision: ${update.final_decision}`, "other");
  }
}

async function streamSse(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const eventLine = raw.split("\n").find((l) => l.startsWith("event: "));
      const dataLine = raw.split("\n").find((l) => l.startsWith("data: "));
      if (!eventLine || !dataLine) continue;
      const event = eventLine.slice(7);
      const data = JSON.parse(dataLine.slice(6));
      onEvent(event, data);
    }
  }
}

function setRunning(isRunning) {
  runBtn.disabled = isRunning;
  btnSpinner.hidden = !isRunning;
  btnLabel.textContent = isRunning ? "Running…" : "Run Review";
}

async function runReview() {
  setRunning(true);
  resetPipeline();
  currentOffset = 0;
  setDot("stage-orchestrator", "running");

  try {
    const res = await fetch("/api/run", { method: "POST" });
    await streamSse(res, (event, data) => {
      if (event === "start") {
        currentThreadId = data.thread_id;
        renderPr(data.pr);
      } else if (event === "node") {
        handleNodeEvent(data.node, data.update);
      } else if (event === "interrupt") {
        showApprovalCard(data);
      } else if (event === "error") {
        showErrorBanner(data.message);
      }
    });
  } catch (err) {
    console.error(err);
    showErrorBanner("Lost connection to the server mid-run — check the server log.");
  } finally {
    setRunning(false);
  }
}

async function submitDecision(decision) {
  const buttons = document.querySelectorAll("#approve-btn, #reject-btn");
  buttons.forEach((b) => (b.disabled = true));
  try {
    const res = await fetch("/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: currentThreadId, decision, offset: currentOffset }),
    });
    await streamSse(res, (event, data) => {
      if (event === "node") handleNodeEvent(data.node, data.update);
      else if (event === "error") showErrorBanner(data.message);
    });
  } catch (err) {
    console.error(err);
    showErrorBanner("Lost connection to the server mid-run — check the server log.");
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

runBtn.addEventListener("click", runReview);
el("approve-btn").addEventListener("click", () => {
  submitDecision(el("decision-input").value.trim() || "approved");
});
el("reject-btn").addEventListener("click", () => {
  submitDecision(el("decision-input").value.trim() || "rejected");
});

fetchPr();
