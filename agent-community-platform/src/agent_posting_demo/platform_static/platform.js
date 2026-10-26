const main = document.querySelector("#main");
const detailDialog = document.querySelector("#detailDialog");
const dialogTitle = document.querySelector("#dialogTitle");
const dialogBody = document.querySelector("#dialogBody");
const toast = document.querySelector("#toast");
const i18n = window.AgentCommunityI18n;
const {t} = i18n;
let pollTimer = null;
let toastTimer = null;
let platformSettingsCache = null;
function configuredOutputRoot(experimentId) {
  return (platformSettingsCache?.output?.root_dir_template || "output/experiments/{experiment_id}")
    .replace("{experiment_id}", experimentId || "experiment");
}
const pendingToastKey = "agent-community-pending-toast";

i18n.applyDocument();
document.addEventListener("wheel", preventNumberInputWheelChange, {capture: true, passive: true});
document.querySelector("#closeDialog").addEventListener("click", () => detailDialog.close());
detailDialog.addEventListener("click", (event) => {
  if (event.target === detailDialog) detailDialog.close();
});

showPendingToast();
boot();

async function boot() {
  markActiveNavigation();
  healthCheck();
  try {
    const path = window.location.pathname.replace(/\/$/, "") || "/";
    if (path === "/") return await renderDashboard();
    if (path === "/experiment-groups") return await renderExperimentGroups();
    if (path === "/experiment-groups/new") return await renderExperimentGroupEditor();
    if (/^\/experiment-groups\/[^/]+\/quick$/.test(path)) {
      return await renderQuickExperiment(decodeURIComponent(path.split("/")[2]));
    }
    if (path.startsWith("/experiment-groups/")) {
      return await renderExperimentGroupDetail(decodeURIComponent(path.split("/")[2]));
    }
    if (path === "/experiments") return await renderExperiments();
    if (path === "/settings") return await renderPlatformSettings();
    if (path === "/resources") return await renderResources();
    if (path === "/memories") return await renderMemoryWorkspace();
    if (path === "/memory-accounts") return await renderMemoryAccounts();
    if (path === "/comparisons") return await renderValidationComparison();
    if (path === "/experiments/new") return await renderExperimentEditor(null);
    if (path.startsWith("/experiments/")) {
      const experimentId = decodeURIComponent(path.split("/")[2]);
      if (new URLSearchParams(location.search).get("edit") === "1") {
        return await renderExperimentEditor(experimentId);
      }
      return await renderExperimentDetail(experimentId);
    }
    if (path === "/runs") return await renderRuns();
    if (path.startsWith("/runs/")) {
      return await renderRun(decodeURIComponent(path.split("/")[2]));
    }
    renderNotFound();
  } catch (error) {
    renderError(error);
  }
}

const EXPERIMENT_MODELS = Object.freeze([
  "gpt-5.6-luna",
  "gpt-5.6-terra",
  "deepseek-v4-flash",
  "deepseek-v4-pro",
  "qwen3.8-max",
  "qwen3.8-flash",
]);

const MEMORY_MODEL_STRATEGIES = new Set(["langmem_official", "a_mem_official", "agent_memory_preview"]);
const FIXED_LOCAL_MEMORY_MODEL = "deepseek-v4-flash";
const FIXED_LOCAL_MEMORY_STRATEGIES = new Set(["langmem_official", "a_mem_official"]);

async function healthCheck() {
  try {
    await apiGet("/api/platform/health");
    document.querySelector("#healthDot").classList.add("is-ok");
    document.querySelector("#healthText").textContent = t("health.online");
  } catch (_) {
    document.querySelector("#healthText").textContent = t("health.offline");
  }
}


function preventNumberInputWheelChange(event) {
  const target = event.target;
  if (!(target instanceof HTMLInputElement) || target.type !== "number") return;
  if (document.activeElement === target) target.blur();
}

function markActiveNavigation() {
  const path = location.pathname;
  let active = "dashboard";
  if (path.startsWith("/experiments") || path.startsWith("/experiment-groups")) active = "experiments";
  if (path === "/settings") active = "settings";
  else if (path.startsWith("/resources")) active = "resources";
  else if (path.startsWith("/memories")) active = "memories";
  else if (path.startsWith("/memory-accounts")) active = "memory-accounts";
  else if (path.startsWith("/comparisons")) active = "comparisons";
  else if (path.startsWith("/runs")) active = "runs";
  document.querySelector(`[data-nav="${active}"]`)?.classList.add("is-active");
}

async function renderDashboard() {
  const data = await apiGet("/api/platform/dashboard");
  const counts = data.counts;
  main.innerHTML = `
    <div class="page">
      ${pageHeader(t("dashboard.title"), "", `
        <a class="button" href="/experiments/new">${t("dashboard.newExperiment")}</a>
      `)}
      <div class="stat-grid dashboard-stat-grid">
        ${statItem(t("dashboard.experimentDefinitions"), formatNumber(counts.experiments), "exp/*/config.yaml")}
        ${statItem(t("dashboard.totalRuns"), formatNumber(counts.runs), t("dashboard.completedRuns", {count: formatNumber(counts.completed_runs)}), "dashboardRunCount", "dashboardCompletedCount")}
        ${statItem(t("dashboard.activeRuns"), formatNumber(counts.active_runs), t("dashboard.workers"), "dashboardActiveRunCount")}
      </div>
      <section class="section">
        ${sectionHeading(t("dashboard.recentRuns"), `<a href="/runs">${t("dashboard.allRuns")}</a>`)}
        <div id="dashboardRuns">${runsTable(data.recent_runs)}</div>
      </section>
      <section class="section">
        ${sectionHeading(t("experiment.title"), `<a href="/experiments">${t("dashboard.manageExperiments")}</a>`)}
        ${experimentsTable(data.experiments)}
      </section>
    </div>`;
  bindRunActions(main);
  startRunTablePolling(
    "#dashboardRuns",
    data.recent_runs,
    async (signal) => {
      const latest = await apiGet("/api/platform/dashboard", {signal});
      document.querySelector("#dashboardRunCount").textContent = formatNumber(latest.counts.runs);
      document.querySelector("#dashboardCompletedCount").textContent = t("dashboard.completedRuns", {count: formatNumber(latest.counts.completed_runs)});
      document.querySelector("#dashboardActiveRunCount").textContent = formatNumber(latest.counts.active_runs);
      return latest.recent_runs;
    },
  );
}

function resourceRevisionOptions(resources, selected = "") {
  return resources.flatMap((resource) => (resource.revisions || []).map((revision) => ({
    ...revision,
    resource_id: resource.resource_id,
    resource_name: resource.name,
    kind: resource.kind,
  }))).map((revision) => {
    const summary = Object.entries(revision.summary || {})
      .map(([key, value]) => `${key.replace(/_/g, " ")}: ${value}`).join(" · ");
    const label = `${revision.resource_name} · r${revision.revision_number} · ${revision.filename}${summary ? ` · ${summary}` : ""}`;
    return `<option value="${escapeHtml(revision.revision_id)}" ${revision.revision_id === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
  }).join("");
}

function findRegisteredResourceRevision(resources, revisionId) {
  if (!revisionId) return null;
  for (const resource of resources || []) {
    const revision = (resource.revisions || []).find((item) => item.revision_id === revisionId);
    if (revision) return {
      ...clone(revision.content || {}),
      resource_revision_id: revision.revision_id,
      resource_id: resource.resource_id,
      resource_name: resource.name,
      environment_type: revision.environment_type,
      summary: clone(revision.summary || {}),
    };
  }
  return null;
}

function registeredResourceDataset(revision) {
  const dataset = revision?.content?.dataset;
  return dataset && typeof dataset === "object" ? dataset : {};
}

function registeredResourceSummaryEntries(summary) {
  const labelKeys = {
    agent_count: "resource.summaryAgents",
    comment_count: "common.comments",
    identity_agent_count: "resource.summaryIdentityAgents",
    post_count: "resource.posts",
    query_count: "resource.summaryQueries",
    server_count: "resource.summaryServers",
    tool_count: "resource.summaryTools",
    category_count: "resource.queryCategory",
  };
  return Object.entries(summary || {}).map(([key, value]) => ({
    key,
    value,
    label: labelKeys[key] ? t(labelKeys[key]) : key.replace(/_/g, " "),
  }));
}

function registeredResourceCard(resource) {
  const latest = resource.latest_revision || resource.revisions?.[0] || {};
  const summary = registeredResourceSummaryEntries(latest.summary)
    .map((item) => `<div class="resource-stat"><strong>${formatNumber(item.value)}</strong><span>${escapeHtml(item.label)}</span></div>`)
    .join("");
  const description = String(resource.description || "").trim();
  const kind = String(resource.kind || "").toLowerCase();
  return `<article class="resource-card resource-kind-${escapeHtml(kind)} ${resource.archived ? "is-archived" : ""}">
    <header class="resource-card-header">
      <div class="resource-card-topline">
        <span class="resource-kind-badge">${escapeHtml(resource.kind)}</span>
        <span class="resource-revision-badge">${t("resource.revisionCount", {count: resource.revision_count})}</span>
      </div>
      <h2>${escapeHtml(resource.name)}</h2>
      <code class="resource-card-id">${escapeHtml(resource.resource_id)}</code>
      ${description ? `<p class="resource-card-description">${escapeHtml(description)}</p>` : ""}
    </header>
    <div class="resource-card-body">
      <div class="resource-file-line">
        <span>${t("resource.file")}</span>
        <strong title="${escapeHtml(latest.filename || "-")}">${escapeHtml(latest.filename || "-")}</strong>
      </div>
      <div class="resource-stats">${summary}</div>
    </div>
    <footer class="resource-actions">
      ${latest.revision_id ? `<button type="button" class="resource-preview-action" data-resource-preview="${escapeHtml(resource.resource_id)}" data-resource-preview-revision="${escapeHtml(latest.revision_id)}">${t("resource.preview")}</button>` : ""}
      <button type="button" class="secondary compact resource-edit-action" data-resource-edit="${escapeHtml(resource.resource_id)}">${t("resource.edit")}</button>
      ${kind !== "posts" || ["moltnet_thread", "moltnet_forum"].includes(latest.environment_type) ? `<label class="button secondary compact">${t("resource.addRevision")}<input type="file" hidden data-resource-revision="${escapeHtml(resource.resource_id)}" data-resource-kind="${escapeHtml(resource.kind)}" data-environment-type="${escapeHtml(latest.environment_type || "")}" accept=".json,.jsonl,application/json" /></label>` : ""}
      <button type="button" class="secondary compact" data-resource-archive="${escapeHtml(resource.resource_id)}" data-archived="${resource.archived ? "true" : "false"}">${resource.archived ? t("resource.restore") : t("resource.archive")}</button>
    </footer>
  </article>`;
}

function registeredResourceRevisionOptions(resource, selectedRevisionId) {
  return (resource.revisions || []).map((revision) => `<option value="${escapeHtml(revision.revision_id)}" ${revision.revision_id === selectedRevisionId ? "selected" : ""}>
    r${formatNumber(revision.revision_number)} · ${escapeHtml(revision.filename || revision.revision_id)}
  </option>`).join("");
}

function registeredQueryTarget(query) {
  const evaluation = query?.evaluation || {};
  return evaluation.target_tool_fqn
    || [evaluation.target_server, evaluation.target_tool].filter(Boolean).join(".")
    || "-";
}

function renderRegisteredQueryPreview(dataset) {
  const queries = Array.isArray(dataset?.queries) ? dataset.queries : [];
  if (!queries.length) return emptyState(t("resource.previewEmpty"));
  return `<div class="table-wrap"><table class="resource-preview-query-table">
    <thead><tr>
      <th>${t("resource.queryId")}</th>
      <th>${t("resource.queryCategory")}</th>
      <th>${t("resource.queryText")}</th>
      <th>${t("resource.queryScope")}</th>
      <th>${t("resource.queryTarget")}</th>
    </tr></thead>
    <tbody>${queries.map((query) => {
      const scope = Array.isArray(query.server_pool) && query.server_pool.length
        ? query.server_pool.join(", ")
        : query.functional_group || t("resource.queryAllServers");
      return `<tr>
        <td><strong>${escapeHtml(query.query_id || "-")}</strong></td>
        <td>${escapeHtml(query.functional_group || "-")}</td>
        <td class="resource-preview-query-text">${escapeHtml(query.query || "-")}</td>
        <td>${escapeHtml(scope)}</td>
        <td><code>${escapeHtml(registeredQueryTarget(query))}</code></td>
      </tr>`;
    }).join("")}</tbody>
  </table></div>`;
}

function registeredToolParameters(tool) {
  const schema = tool?.inputSchema || {};
  const properties = schema.properties && typeof schema.properties === "object"
    ? schema.properties : {};
  const required = new Set(Array.isArray(schema.required) ? schema.required : []);
  const entries = Object.entries(properties);
  if (!entries.length) return `<p class="muted">${t("resource.noParameters")}</p>`;
  return `<div class="table-wrap"><table class="resource-preview-parameter-table">
    <thead><tr>
      <th>${t("resource.parameter")}</th>
      <th>${t("resource.requirement")}</th>
      <th>${t("resource.parameterType")}</th>
      <th>${t("resource.parameterDescription")}</th>
    </tr></thead>
    <tbody>${entries.map(([name, specification]) => {
      const type = Array.isArray(specification?.type)
        ? specification.type.join(" | ")
        : specification?.type || (Array.isArray(specification?.enum) ? "enum" : "-");
      return `<tr>
        <td><code>${escapeHtml(name)}</code></td>
        <td>${t(required.has(name) ? "resource.required" : "resource.optional")}</td>
        <td>${escapeHtml(type)}</td>
        <td>${escapeHtml(specification?.description || t("resource.noDescription"))}</td>
      </tr>`;
    }).join("")}</tbody>
  </table></div>`;
}

function renderRegisteredMcpPreview(dataset) {
  const servers = Array.isArray(dataset?.servers) ? dataset.servers : [];
  if (!servers.length) return emptyState(t("resource.previewEmpty"));
  return `<div class="resource-preview-servers">${servers.map((server) => `
    <article class="resource-preview-server">
      <header>
        <div><h3>${escapeHtml(server.name || server.slug || "-")}</h3><code>${escapeHtml(server.slug || "-")}</code></div>
        ${server.functional_group ? `<span class="tag">${escapeHtml(server.functional_group)}</span>` : ""}
      </header>
      <p class="resource-preview-description">${escapeHtml(server.description || t("resource.noDescription"))}</p>
      <div class="resource-preview-tools">${(server.tools || []).map((tool) => `
        <section class="resource-preview-tool">
          <header>
            <div><strong>${escapeHtml(tool.title || tool.name || "-")}</strong><code>${escapeHtml(`${server.slug || "?"}.${tool.name || "?"}`)}</code></div>
            <span class="tag">${t("resource.tool")}</span>
          </header>
          <p class="resource-preview-description">${escapeHtml(tool.description || t("resource.noDescription"))}</p>
          <details>
            <summary>${t("resource.parameters")}</summary>
            ${registeredToolParameters(tool)}
          </details>
        </section>`).join("") || emptyState(t("resource.previewEmpty"))}</div>
    </article>`).join("")}</div>`;
}

function registeredPostThread(post, agentNames) {
  return {
    available: true,
    thread: {
      post: {
        title: post.title || "",
        body: post.body || "",
        agent_id: post.author_id || "",
        display_name: agentNames.get(post.author_id) || post.author_id || "-",
        created_at: post.created_at || "",
      },
      comments: (post.comments || []).map((comment) => ({
        comment_id: comment.id,
        parent_comment_id: comment.parent_id,
        agent_id: comment.author_id || "",
        display_name: agentNames.get(comment.author_id) || comment.author_id || "-",
        body: comment.body || "",
        created_at: comment.created_at || "",
        source: "moltnet",
      })),
    },
    truncated_comments: 0,
  };
}

function renderRegisteredPostPreview(dataset) {
  const posts = Array.isArray(dataset?.posts) ? dataset.posts : [];
  if (!posts.length) return emptyState(t("resource.previewEmpty"));
  const agentNames = new Map(
    (dataset.agents || []).map((agent) => [agent.id, agent.name || agent.id]),
  );
  return `<div class="resource-preview-posts">${posts.map((post, index) => `
    <details class="resource-preview-post" ${index === 0 ? "open" : ""}>
      <summary>
        <span><strong>${escapeHtml(post.title || t("common.untitled"))}</strong><small>${escapeHtml(agentNames.get(post.author_id) || post.author_id || "-")} · ${escapeHtml(post.community || "-")}</small></span>
        <span class="tag">${t("common.comments")}: ${formatNumber((post.comments || []).length)}</span>
      </summary>
      ${renderThread(registeredPostThread(post, agentNames))}
    </details>`).join("")}</div>`;
}

function renderRegisteredResourceContent(kind, dataset) {
  if (kind === "posts") return renderRegisteredPostPreview(dataset);
  if (kind === "queries") return renderRegisteredQueryPreview(dataset);
  if (kind === "mcp") return renderRegisteredMcpPreview(dataset);
  return emptyState(t("resource.previewEmpty"));
}

function drawRegisteredResourcePreview(resource, requestedRevisionId) {
  const revisions = resource.revisions || [];
  const revision = revisions.find((item) => item.revision_id === requestedRevisionId)
    || revisions[0];
  dialogTitle.innerHTML = `<h2>${escapeHtml(resource.name || resource.resource_id)}</h2><small>${escapeHtml(resource.kind)} · ${escapeHtml(resource.resource_id)}</small>`;
  if (!revision) {
    dialogBody.innerHTML = emptyState(t("resource.previewEmpty"));
    return;
  }
  const summary = registeredResourceSummaryEntries(revision.summary)
    .map((item) => `<span>${escapeHtml(item.label)}: <strong>${formatNumber(item.value)}</strong></span>`)
    .join("");
  dialogBody.innerHTML = `
    <div class="resource-preview-toolbar">
      <label for="resourcePreviewRevision">${t("resource.previewRevision")}</label>
      <select id="resourcePreviewRevision">${registeredResourceRevisionOptions(resource, revision.revision_id)}</select>
    </div>
    <div class="detail-meta">
      ${detailMeta(t("resource.previewFile"), revision.filename || "-")}
      ${detailMeta(t("resource.previewRevisionId"), revision.revision_id)}
      ${detailMeta(t("resource.previewHash"), revision.content_sha256 || "-")}
    </div>
    <div class="resource-meta resource-preview-counts">${summary}</div>
    <section class="resource-preview-content">
      <h3>${t(resource.kind === "posts" ? "resource.previewPosts" : resource.kind === "queries" ? "resource.previewQueries" : "resource.previewMcp")}</h3>
      ${renderRegisteredResourceContent(resource.kind, registeredResourceDataset(revision))}
    </section>`;
  dialogBody.querySelector("#resourcePreviewRevision")?.addEventListener("change", (event) => {
    drawRegisteredResourcePreview(resource, event.currentTarget.value);
  });
}

async function openRegisteredResourcePreview(resourceId, revisionId = "") {
  dialogTitle.innerHTML = `<h2>${t("resource.preview")}</h2><small>${escapeHtml(resourceId)}</small>`;
  dialogBody.innerHTML = `<div class="loading-state"><strong>${t("common.loading")}</strong></div>`;
  if (!detailDialog.open) detailDialog.showModal();
  try {
    const resource = await apiGet(
      `/api/platform/resources/${encodeURIComponent(resourceId)}?include_content=true`,
    );
    drawRegisteredResourcePreview(resource, revisionId);
  } catch (error) {
    dialogBody.innerHTML = `<div class="error-state"><strong>${escapeHtml(error.message)}</strong></div>`;
  }
}

function registeredResourceSections(resources) {
  return ["posts", "queries", "mcp"].map((kind) => {
    const items = resources.filter((resource) => resource.kind === kind);
    const label = kind === "posts" ? t("resource.posts") : kind === "queries" ? "Query" : "MCP";
    return `<section class="section resource-type-section" data-resource-section="${kind}">
      ${sectionHeading(label, `<span class="resource-section-count">${items.length}</span>`)}
      <div class="resource-grid">${items.map(registeredResourceCard).join("") || emptyState(t("resource.empty"))}</div>
    </section>`;
  }).join("");
}

function renderResourceImportSummary(preview, file) {
  const summary = registeredResourceSummaryEntries(preview.summary)
    .map((item) => `<div class="resource-stat"><strong>${formatNumber(item.value)}</strong><span>${escapeHtml(item.label)}</span></div>`).join("");
  return `<div class="resource-import-summary">
    <strong>${t("resource.fileSummary")}</strong>
    <p>${escapeHtml(file.name)} · ${formatNumber(file.size)} ${t("resource.bytes")}${preview.environment_type ? ` · ${escapeHtml(preview.environment_type)}` : ""}</p>
    <div class="resource-stats">${summary}</div>
    ${preview.categories?.length ? `<p>${t("resource.queryCategory")} (${formatNumber(preview.category_count)}): ${preview.categories.map(escapeHtml).join(" · ")}</p>` : ""}
    ${preview.samples?.length ? `<div class="resource-import-samples"><small>${t("resource.fileSamples")}</small><ul>${preview.samples.map((sample) => `<li>${escapeHtml(sample)}</li>`).join("")}</ul></div>` : ""}
    <small>${t("resource.fileValidated")}</small>
  </div>`;
}

function bindResourceImportPreview(form, fileSelector, summarySelector, getOptions, summarize) {
  const input = form.querySelector(fileSelector);
  const summary = form.querySelector(summarySelector);
  const button = form.querySelector('button[type="submit"]');
  let generation = 0;
  let pending = Promise.resolve(null);
  const refresh = () => {
    const token = ++generation;
    const file = input.files?.[0];
    button.disabled = true;
    if (!file) { summary.hidden = true; pending = Promise.resolve(null); return; }
    summary.hidden = false;
    summary.innerHTML = `<p class="muted">${t("resource.fileChecking")}</p>`;
    const options = getOptions();
    pending = (async () => {
      try {
        const content = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
        const preview = await summarize({...options, filename: file.name, content});
        if (token !== generation) return null;
        summary.innerHTML = renderResourceImportSummary(preview, file);
        button.disabled = false;
        return {file, content, options};
      } catch (error) {
        if (token === generation) summary.innerHTML = `<p class="error-state">${escapeHtml(error.message)}</p>`;
        return null;
      }
    })();
  };
  input.addEventListener("change", refresh);
  refresh();
  return {refresh, ready: () => pending};
}

function openResourceMetadataEditor(resource, mapping = false) {
  dialogTitle.innerHTML = `<h2>${t("resource.edit")}</h2><small>${escapeHtml(mapping ? resource.mapping_id : resource.resource_id)}</small>`;
  dialogBody.innerHTML = `<form id="resourceMetadataForm" class="form-grid">
    ${textField("metadata.name", t("resource.name"), resource.name, "", "required")}
    ${mapping ? "" : textareaField("metadata.description", t("resource.description"), resource.description || "", "wide")}
    <p class="wide muted">${t("resource.editHelp")}</p>
    <div class="wide form-actions"><button type="submit">${t("resource.save")}</button><button type="button" class="secondary" data-cancel-edit>${t("common.cancel")}</button></div>
  </form>`;
  if (!detailDialog.open) detailDialog.showModal();
  const form = dialogBody.querySelector("#resourceMetadataForm");
  form.querySelector("[data-cancel-edit]").addEventListener("click", () => detailDialog.close());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const payload = {name: fieldValue(form, "metadata.name")};
      if (!mapping) payload.description = fieldValue(form, "metadata.description");
      const id = mapping ? resource.mapping_id : resource.resource_id;
      await apiPost(`/api/platform/${mapping ? "target-mappings" : "resources"}/${encodeURIComponent(id)}/metadata`, payload);
      rememberToast(t("resource.updated"));
      location.reload();
    } catch (error) { button.disabled = false; showToast(error.message); }
  });
}

async function renderResources() {
  const [payload, mappingsPayload] = await Promise.all([
    apiGet("/api/platform/resources?archived=all"),
    apiGet("/api/platform/target-mappings"),
  ]);
  const resources = payload.resources || [];
  main.innerHTML = `<div class="page resource-page">
    ${pageHeader(t("resource.title"), t("resource.subtitle"), "")}
    <section class="section">
      ${sectionHeading(t("resource.new"))}
      <form id="resourceForm" class="form-grid">
        ${selectField("resource.kind", t("resource.kind"), "posts", [["posts", t("resource.posts")], ["queries", "Query"], ["mcp", "MCP"]])}
        ${textField("resource.id", t("resource.id"), "", "lowercase-id", 'required pattern="[a-z0-9][a-z0-9_-]{0,63}"')}
        ${textField("resource.name", t("resource.name"), "", "", "required")}
        ${selectField("resource.environment", t("resource.postEnvironment"), "moltnet_forum", [["moltnet_forum", "moltnet_forum"], ["moltnet_thread", "moltnet_thread"]])}
        ${textareaField("resource.description", t("resource.description"), "", "wide")}
        <div class="form-field wide"><label for="resourceFile">${t("resource.file")}</label><input id="resourceFile" type="file" accept=".json,.jsonl,application/json" required /><small>${t("resource.fileHelp")}</small></div>
        <div id="resourceImportSummary" class="wide" role="status" aria-live="polite" hidden></div>
        <div class="wide"><button type="submit">${t("resource.register")}</button></div>
      </form>
    </section>
    <section class="section">${sectionHeading(t("mapping.title"))}
      <p class="muted">${t("mapping.help")}</p>
      <form id="targetMappingForm" class="form-grid">
        ${textField("mapping.id", t("resource.id"), "", "lowercase-id", 'required pattern="[a-z0-9][a-z0-9_-]{0,63}"')}
        ${textField("mapping.name", t("resource.name"), "", "", "required")}
        <div class="form-field wide"><label for="targetMappingFile">${t("resource.file")}</label><input id="targetMappingFile" type="file" accept=".json,application/json" required /></div>
        <div id="mappingImportSummary" class="wide" role="status" aria-live="polite" hidden></div>
        <div class="wide"><button type="submit">${t("resource.register")}</button></div>
      </form>
      <div class="resource-grid">${(mappingsPayload.mappings || []).map((mapping) => `<article class="resource-card"><div class="resource-card-body"><h3>${escapeHtml(mapping.name)}</h3><code>${escapeHtml(mapping.mapping_id)}</code><p>${escapeHtml(mapping.filename)} · ${Object.keys(mapping.mapping || {}).length} ${t("resource.queryCategory")}</p><details><summary>${t("resource.preview")}</summary>${Object.entries(mapping.mapping || {}).map(([category, server]) => `<p><b>${escapeHtml(category)}</b> → ${escapeHtml(server)}</p>`).join("")}</details></div><footer class="resource-actions"><button type="button" class="secondary resource-edit-action" data-mapping-edit="${escapeHtml(mapping.mapping_id)}">${t("resource.edit")}</button></footer></article>`).join("")}</div>
    </section>
    <section class="section">${sectionHeading(t("resource.registered"))}${registeredResourceSections(resources)}</section>
  </div>`;
  const form = document.querySelector("#resourceForm");
  const importPreview = bindResourceImportPreview(form, "#resourceFile", "#resourceImportSummary", () => ({
    kind: fieldValue(form, "resource.kind"),
    environment_type: fieldValue(form, "resource.kind") === "posts" ? fieldValue(form, "resource.environment") : null,
  }), (payload) => apiPost("/api/platform/resources/preview", payload));
  const syncKind = () => {
    form.elements["resource.environment"].closest(".form-field").hidden = fieldValue(form, "resource.kind") !== "posts";
  };
  form.elements["resource.kind"].addEventListener("change", () => { syncKind(); importPreview.refresh(); });
  form.elements["resource.environment"].addEventListener("change", importPreview.refresh);
  syncKind();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const imported = await importPreview.ready();
    if (!imported) return;
    const {file, content, options} = imported;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await apiPost("/api/platform/resources", {
        resource_id: fieldValue(form, "resource.id"), ...options,
        name: fieldValue(form, "resource.name"), description: fieldValue(form, "resource.description"),
        filename: file.name, content,
      });
      rememberToast(t("resource.created"));
      location.reload();
    } catch (error) { button.disabled = false; showToast(error.message); }
  });
  const mappingForm = main.querySelector("#targetMappingForm");
  const mappingPreview = bindResourceImportPreview(mappingForm, "#targetMappingFile", "#mappingImportSummary", () => ({}),
    (payload) => apiPost("/api/platform/target-mappings/preview", payload));
  mappingForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const mappingForm = event.currentTarget;
    if (!mappingForm.reportValidity()) return;
    const imported = await mappingPreview.ready();
    if (!imported) return;
    const {file, content} = imported;
    const button = mappingForm.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await apiPost("/api/platform/target-mappings", {
        mapping_id: fieldValue(mappingForm, "mapping.id"),
        name: fieldValue(mappingForm, "mapping.name"), filename: file.name,
        content,
      });
      rememberToast(t("resource.created")); location.reload();
    } catch (error) { button.disabled = false; showToast(error.message); }
  });
  main.querySelectorAll("[data-resource-edit]").forEach((button) => button.addEventListener("click", () => {
    openResourceMetadataEditor(resources.find((resource) => resource.resource_id === button.dataset.resourceEdit));
  }));
  main.querySelectorAll("[data-mapping-edit]").forEach((button) => button.addEventListener("click", () => {
    openResourceMetadataEditor((mappingsPayload.mappings || []).find((mapping) => mapping.mapping_id === button.dataset.mappingEdit), true);
  }));
  main.querySelectorAll("[data-resource-revision]").forEach((input) => input.addEventListener("change", async () => {
    const file = input.files?.[0];
    if (!file) return;
    try {
      const content = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
      await apiPost(`/api/platform/resources/${encodeURIComponent(input.dataset.resourceRevision)}/revisions`, {
        filename: file.name, content,
        environment_type: input.dataset.resourceKind === "posts" ? input.dataset.environmentType : null,
      });
      rememberToast(t("resource.revisionCreated"));
      location.reload();
    } catch (error) { input.value = ""; showToast(error.message); }
  }));
  main.querySelectorAll("[data-resource-preview]").forEach((button) => button.addEventListener("click", () => {
    openRegisteredResourcePreview(
      button.dataset.resourcePreview,
      button.dataset.resourcePreviewRevision,
    );
  }));
  main.querySelectorAll("[data-resource-archive]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await apiPost(`/api/platform/resources/${encodeURIComponent(button.dataset.resourceArchive)}/archive`, {archived: button.dataset.archived !== "true"});
      location.reload();
    } catch (error) { button.disabled = false; showToast(error.message); }
  }));
}

function comparisonRatio(numerator, denominator) {
  return denominator ? numerator / denominator : null;
}

function comparisonPercent(value) {
  return value === null || !Number.isFinite(value) ? "-" : `${(value * 100).toFixed(1)}%`;
}

function comparisonMetric(value, numerator, denominator, color, label = "") {
  return `<span class="comparison-metric">${label ? `<small class="comparison-metric-label">${label}</small>` : ""}<span>${numerator} / ${denominator}<b>${comparisonPercent(value)}</b></span><i class="comparison-bar"><i style="width:${Math.max(0, Math.min(100, (value || 0) * 100))}%;background:${color}"></i></i></span>`;
}

function aggregateComparisonRows(rows) {
  const keys = ["queries", "completed", "target_evaluated", "target_hits", "exact_leak_evaluated", "exact_leak_present", "exact_leaks", "exact_leak_target_selected", "exact_leak_joint_leaks", "exact_leak_outside_target_leaks"];
  const counts = Object.fromEntries(keys.map((key) => [key, rows.reduce((sum, row) => sum + Number(row.counts?.[key] || 0), 0)]));
  return {counts, rates: {
    target: comparisonRatio(counts.target_hits, counts.target_evaluated),
    exact: comparisonRatio(counts.exact_leaks, counts.exact_leak_evaluated),
    joint: comparisonRatio(counts.exact_leak_joint_leaks, counts.exact_leak_target_selected),
  }};
}

function comparisonMultiFilter(id, label, items) {
  const choices = (items || []).map((item) => typeof item === "string"
    ? {value: item, label: item} : item);
  return `<details class="comparison-multi" id="${id}"><summary>${label}<strong>${t("common.all")}</strong></summary>
    <div class="comparison-multi-options">${choices.map((item) => `<label><input type="checkbox" value="${escapeHtml(item.value)}" />${escapeHtml(item.label)}</label>`).join("") || `<span class="muted">-</span>`}</div></details>`;
}

function comparisonConditionLabel(row) {
  return [row.post_label, memoryStrategyLabel(row.memory_strategy), row.behavior_model, row.mcp_label]
    .map((value) => value || "-").join(" · ");
}

function comparisonMethods() {
  // Resource families retain their method color across revisions and metadata edits.
  return [
    {id: "ours", label: "Ours", color: "#0072b2", kind: "post", resourceId: "metatool-ours-nameids-20261005-posts"},
    {id: "hidden-in-memory", label: "Hidden in Memory", color: "#009e73", kind: "post", resourceId: "hidden-in-memory-json-ac-apikey-20261006-posts"},
    {id: "mpma-best-name", label: "MPMA-Best_Name", color: "#cc79a7", kind: "mcp", resourceId: "1005-metatool-mpma-best-name"},
    {id: "mpma-best-description", label: "MPMA-Best_description", color: "#e69f00", kind: "mcp", resourceId: "1005-metatool-mpma-best-description"},
    {id: "mpma-gapma", label: "MPMA-GAPMA", color: "#d55e00", kind: "mcp", resourceId: "1005-metatool-mpma-gapma-authority"},
    {id: "preference-manipulation", label: "Preference Manipulation", color: "#56b4e9", kind: "mcp", resourceId: "1005-metatool-preference-manipulation"},
  ];
}

function comparisonOtherMethod() {
  return {id: "other", label: t("comparison.otherMethod"), color: "#607184"};
}

function comparisonMethodForRow(row) {
  const matches = comparisonMethods().filter((method) => {
    const prefix = method.kind;
    const identity = String(row[`${prefix}_resource_id`] || row[`${prefix}_revision_id`] || row[`${prefix}_filter_id`] || "").trim();
    if (identity) {
      return identity === method.resourceId
        || (identity.startsWith(`${method.resourceId}-r`) && /^\d+$/.test(identity.slice(method.resourceId.length + 2)));
    }
    const label = String(row[`${prefix}_label`] || "").replace(/\s*·\s*r\d+\s*$/i, "").trim().toLowerCase();
    return label === method.label.toLowerCase();
  });
  // A combined post/MCP intervention is not a single-method condition.
  return matches.length === 1 ? matches[0] : comparisonOtherMethod();
}

async function renderValidationComparison() {
  const data = await apiGet("/api/platform/validation-comparison/options");
  const rows = data.runs || [];
  const filters = data.filters || {};
  main.innerHTML = `<div class="page comparison-page">
    ${pageHeader(t("comparison.title"), t("comparison.subtitle"), "")}
    <section class="section comparison-filters">
      ${comparisonMultiFilter("comparisonGroup", t("comparison.group"), filters.experiment_groups)}
      ${comparisonMultiFilter("comparisonPost", t("comparison.posts"), filters.posts)}
      ${comparisonMultiFilter("comparisonMemory", t("comparison.memory"), (filters.memory_strategies || []).map((value) => ({value, label: memoryStrategyLabel(value)})))}
      ${comparisonMultiFilter("comparisonModel", t("comparison.model"), filters.models)}
      ${comparisonMultiFilter("comparisonMcp", t("comparison.mcp"), filters.mcps)}
      <div class="form-field"><label for="comparisonSort">${t("comparison.sort")}</label><select id="comparisonSort"><option value="target">${t("comparison.targetSort")}</option><option value="exact">${t("comparison.exactSort")}</option><option value="joint">${t("comparison.jointSort")}</option><option value="condition">${t("comparison.conditionSort")}</option></select></div>
      <button type="button" class="secondary" id="comparisonReset">${t("comparison.reset")}</button>
    </section>
    <div class="comparison-kpis" id="comparisonKpis"></div>
    <section class="section comparison-analysis"><div><h2>${t("comparison.distribution")}</h2><p class="muted">${t("comparison.distributionHelp")}</p><div class="comparison-legend" id="comparisonLegend"></div></div><svg id="comparisonScatter" viewBox="0 0 700 360" role="img"></svg></section>
    <section class="section"><div class="section-heading"><div><h2>${t("comparison.runs")}</h2><p id="comparisonHint" class="muted"></p></div><button type="button" class="secondary" id="comparisonExport">${t("comparison.export")}</button></div><div id="comparisonHidden"></div><div id="comparisonList"></div></section>
    <section class="section comparison-method"><h2>${t("comparison.method")}</h2><p>${t("comparison.exactHelp")}</p><p class="validation-notice">${t("comparison.boundary")}</p></section>
  </div>`;
  const state = {open: new Set(), hidden: new Set()};
  const controls = {
    group: document.querySelector("#comparisonGroup"), post: document.querySelector("#comparisonPost"),
    memory: document.querySelector("#comparisonMemory"), model: document.querySelector("#comparisonModel"), mcp: document.querySelector("#comparisonMcp"), sort: document.querySelector("#comparisonSort"),
  };
  const selected = (control) => new Set([...control.querySelectorAll('input:checked')].map((input) => input.value));
  const matches = (selection, value) => !selection.size || selection.has(value);
  const filtered = () => {
    const groups = selected(controls.group), posts = selected(controls.post);
    const memories = selected(controls.memory), models = selected(controls.model), mcps = selected(controls.mcp);
    const result = rows.filter((row) => !state.hidden.has(row.run_id)
      && matches(groups, row.group_id) && matches(posts, row.post_filter_id)
      && matches(memories, row.memory_strategy) && matches(models, row.behavior_model)
      && matches(mcps, row.mcp_filter_id));
    const rate = (row, key) => row.rates?.[key]?.value ?? -1;
    const sorters = {
      target: (a, b) => rate(b, "target_hit") - rate(a, "target_hit"),
      exact: (a, b) => rate(b, "exact_leak") - rate(a, "exact_leak"),
      joint: (a, b) => rate(b, "exact_leak_target_joint") - rate(a, "exact_leak_target_joint"),
      condition: (a, b) => String(a.post_label).localeCompare(String(b.post_label)) || memoryStrategyLabel(a.memory_strategy || "").localeCompare(memoryStrategyLabel(b.memory_strategy || "")) || String(a.behavior_model).localeCompare(String(b.behavior_model)) || String(a.mcp_label).localeCompare(String(b.mcp_label)),
    };
    return result.sort(sorters[controls.sort.value]);
  };
  const render = () => {
    for (const key of ["group", "post", "memory", "model", "mcp"]) {
      const values = [...selected(controls[key])];
      controls[key].querySelector("summary strong").textContent = values.length ? `${values.length} ${t("comparison.selected")}` : t("common.all");
    }
    const visible = filtered();
    const aggregate = aggregateComparisonRows(visible);
    document.querySelector("#comparisonKpis").innerHTML = [
      [t("comparison.runsQueries"), `${visible.length} / ${aggregate.counts.completed}`, `${aggregate.counts.queries} ${t("comparison.planned")}`],
      [t("comparison.targetRate"), comparisonPercent(aggregate.rates.target), `${aggregate.counts.target_hits} / ${aggregate.counts.target_evaluated}`],
      [t("comparison.exactRate"), comparisonPercent(aggregate.rates.exact), `${aggregate.counts.exact_leaks} / ${aggregate.counts.exact_leak_evaluated}`],
      [t("comparison.jointRate"), comparisonPercent(aggregate.rates.joint), `${aggregate.counts.exact_leak_joint_leaks} / ${aggregate.counts.exact_leak_target_selected}`],
    ].map(([label, value, meta]) => `<article class="comparison-kpi"><span>${label}</span><strong>${value}</strong><small>${meta}</small></article>`).join("");
    document.querySelector("#comparisonHint").textContent = `${t("comparison.showing", {count: visible.length})} ${state.hidden.size ? t("comparison.hiddenCount", {count: state.hidden.size}) : ""}`;
    document.querySelector("#comparisonHidden").innerHTML = state.hidden.size ? `<div class="comparison-hidden"><strong>${t("comparison.hiddenCount", {count: state.hidden.size})}</strong>${rows.filter((row) => state.hidden.has(row.run_id)).map((row) => `<button type="button" class="secondary compact" data-comparison-restore="${escapeHtml(row.run_id)}">${escapeHtml(comparisonConditionLabel(row))} ↩</button>`).join("")}</div>` : "";
    document.querySelector("#comparisonList").innerHTML = visible.length ? visible.map((row) => {
      const open = state.open.has(row.run_id);
      const categoryRows = Object.values(row.categories || {}).map((category) => `<div class="comparison-category"><strong>${escapeHtml(category.functional_group || t("comparison.uncategorized"))}</strong><span>${category.completed_queries || 0} / ${category.query_count || 0}</span>${comparisonMetric(category.target_hit_rate, category.target_hits || 0, category.target_evaluated_queries || 0, "#16856b")}${comparisonMetric(category.exact_leak_rate, category.exact_leaks || 0, category.exact_leak_evaluated_queries || 0, "#c77718")}${comparisonMetric(comparisonRatio(category.exact_leak_joint_leaks || 0, category.exact_leak_target_selected_queries || 0), category.exact_leak_joint_leaks || 0, category.exact_leak_target_selected_queries || 0, "#b94b52")}</div>`).join("");
      return `<article class="comparison-run ${open ? "is-open" : ""}" id="comparison-run-${escapeHtml(row.run_id)}">
        <div class="comparison-run-info">
          <div><small>${t("comparison.posts")}</small><a class="comparison-condition" href="/runs/${encodeURIComponent(row.run_id)}" aria-label="${escapeHtml(comparisonConditionLabel(row))}"><strong>${escapeHtml(row.post_label || "-")}</strong></a></div>
          <div><small>${t("comparison.memory")}</small><strong>${escapeHtml(memoryStrategyLabel(row.memory_strategy || "-"))}</strong></div>
          <div><small>${t("comparison.model")}</small><strong>${escapeHtml(row.behavior_model || "-")}</strong></div>
          <div><small>${t("comparison.mcp")}</small><strong>${escapeHtml(row.mcp_label || "-")}</strong></div>
        </div>
        <div class="comparison-run-results">
          <span class="comparison-query"><small>${t("comparison.runsQueries")}</small><b>${row.counts.completed}/${row.counts.queries}</b></span>
          ${comparisonMetric(row.rates.target_hit.value, row.counts.target_hits, row.counts.target_evaluated, "#16856b", t("comparison.targetRate"))}
          ${comparisonMetric(row.rates.exact_leak.value, row.counts.exact_leaks, row.counts.exact_leak_evaluated, "#c77718", t("comparison.exactRate"))}
          ${comparisonMetric(row.rates.exact_leak_target_joint.value, row.counts.exact_leak_joint_leaks, row.counts.exact_leak_target_selected, "#b94b52", t("comparison.jointRate"))}
          <div class="comparison-actions"><button type="button" class="secondary compact" data-comparison-open="${escapeHtml(row.run_id)}">${open ? t("comparison.collapse") : t("comparison.expand")}</button><button type="button" class="secondary compact" data-comparison-hide="${escapeHtml(row.run_id)}">${t("comparison.hide")}</button></div>
        </div>
        <div class="comparison-categories"><div class="comparison-category comparison-category-head"><span>${t("resource.queryCategory")}</span><span>${t("comparison.runsQueries")}</span><span>${t("comparison.targetRate")}</span><span>${t("comparison.exactRate")}</span><span>${t("comparison.jointRate")}</span></div>${categoryRows || emptyState("-")}</div>
      </article>`;
    }).join("") : emptyState(t("comparison.empty"));
    renderComparisonScatter(visible);
    main.querySelectorAll("[data-comparison-open]").forEach((button) => button.addEventListener("click", () => { const id = button.dataset.comparisonOpen; state.open.has(id) ? state.open.delete(id) : state.open.add(id); render(); }));
    main.querySelectorAll("[data-comparison-hide]").forEach((button) => button.addEventListener("click", () => { state.hidden.add(button.dataset.comparisonHide); render(); }));
    main.querySelectorAll("[data-comparison-restore]").forEach((button) => button.addEventListener("click", () => { state.hidden.delete(button.dataset.comparisonRestore); render(); }));
  };
  Object.values(controls).forEach((control) => control.addEventListener("change", render));
  document.querySelector("#comparisonReset").addEventListener("click", () => { [controls.group, controls.post, controls.memory, controls.model, controls.mcp].forEach((control) => control.querySelectorAll("input:checked").forEach((input) => { input.checked = false; })); controls.sort.value = "target"; state.hidden.clear(); render(); });
  document.querySelector("#comparisonExport").addEventListener("click", () => exportComparisonCsv(filtered()));
  render();
}

function renderComparisonScatter(rows) {
  const svg = document.querySelector("#comparisonScatter");
  const visibleMethods = new Set(rows.map((row) => comparisonMethodForRow(row).id));
  const methods = [...comparisonMethods(), comparisonOtherMethod()].filter((method) => visibleMethods.has(method.id));
  document.querySelector("#comparisonLegend").innerHTML = methods.map((method) => `<span data-comparison-method="${method.id}"><i style="background:${method.color}"></i>${escapeHtml(method.label)}</span>`).join("");
  const width = 700, height = 360, margin = {left: 52, right: 18, top: 18, bottom: 44};
  const chartWidth = width - margin.left - margin.right, chartHeight = height - margin.top - margin.bottom;
  let output = "";
  [0, .25, .5, .75, 1].forEach((tick) => {
    const x = margin.left + tick * chartWidth, y = margin.top + (1 - tick) * chartHeight;
    output += `<line class="comparison-grid-line" x1="${x}" y1="${margin.top}" x2="${x}" y2="${margin.top + chartHeight}"/><line class="comparison-grid-line" x1="${margin.left}" y1="${y}" x2="${margin.left + chartWidth}" y2="${y}"/><text class="comparison-axis" x="${x}" y="${height - 20}" text-anchor="middle">${tick * 100}%</text><text class="comparison-axis" x="${margin.left - 10}" y="${y + 4}" text-anchor="end">${tick * 100}%</text>`;
  });
  output += `<text class="comparison-axis" x="${margin.left + chartWidth / 2}" y="${height - 3}" text-anchor="middle">${t("comparison.targetRate")}</text><text class="comparison-axis" transform="translate(12 ${margin.top + chartHeight / 2}) rotate(-90)" text-anchor="middle">${t("comparison.exactRate")}</text>`;
  rows.forEach((row) => {
    const target = Math.max(0, row.rates?.target_hit?.value || 0), api = Math.max(0, row.rates?.exact_leak?.value || 0);
    const x = margin.left + target * chartWidth, y = margin.top + (1 - api) * chartHeight;
    const method = comparisonMethodForRow(row);
    const description = `${t("comparison.methodColorLabel")}: ${method.label} · ${comparisonConditionLabel(row)}`;
    output += `<circle class="comparison-dot" tabindex="0" role="button" data-comparison-point="${escapeHtml(row.run_id)}" data-comparison-method="${method.id}" aria-label="${escapeHtml(description)}" cx="${x}" cy="${y}" r="6" fill="${method.color}"><title>${escapeHtml(`${description}\nMCP ${comparisonPercent(target)} · Leak ${comparisonPercent(api)}`)}</title></circle>`;
  });
  svg.innerHTML = output;
  svg.querySelectorAll("[data-comparison-point]").forEach((point) => {
    const reveal = () => {
      const row = document.getElementById(`comparison-run-${point.dataset.comparisonPoint}`);
      row?.scrollIntoView({behavior: "smooth", block: "center"});
      row?.querySelector("[data-comparison-open]")?.focus();
    };
    point.addEventListener("click", reveal);
    point.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); reveal(); } });
  });
}

function exportComparisonCsv(rows) {
  const columns = ["post", "group", "memory", "model", "mcp", "mcp_revision_id", "experiment_id", "run_id", "completed", "target_hits", "target_rate", "exact_leaks", "exact_leak_rate", "joint_rate"];
  const lines = [columns.join(",")];
  rows.forEach((row) => lines.push([
    row.post_label, row.group_name, row.memory_strategy, row.behavior_model, row.mcp_label, row.mcp_revision_id, row.experiment_id, row.run_id,
    row.counts.completed, row.counts.target_hits, row.rates.target_hit.value ?? "", row.counts.exact_leaks,
    row.rates.exact_leak.value ?? "", row.rates.exact_leak_target_joint.value ?? "",
  ].map((value) => `"${String(value ?? "").replace(/"/g, '""')}"`).join(",")));
  const blob = new Blob(["\ufeff" + lines.join("\n")], {type: "text/csv;charset=utf-8"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob); link.download = "stage-two-comparison.csv"; link.click();
  URL.revokeObjectURL(link.href);
}

function experimentGroupId(value) {
  return String(
    value?.group_id
    || value?.experiment_group_id
    || value?.group?.group_id
    || value?.group?.id
    || "",
  ).trim();
}

function experimentGroupName(value) {
  return String(
    value?.group_name
    || value?.experiment_group_name
    || value?.group?.name
    || experimentGroupId(value),
  ).trim();
}

function experimentGroupLink(value) {
  const groupId = experimentGroupId(value);
  if (!groupId) return "";
  const name = experimentGroupName(value) || groupId;
  return `<a class="group-link" href="/experiment-groups/${encodeURIComponent(groupId)}">${escapeHtml(name)}</a>`;
}

function normalizeExperimentGroups(payload) {
  const groups = payload?.groups || payload?.items || [];
  return Array.isArray(groups) ? groups.map(normalizeExperimentGroupRecord) : [];
}

function normalizeExperimentGroup(payload) {
  const group = payload?.group || payload;
  return group ? normalizeExperimentGroupRecord(group) : group;
}

function normalizeExperimentGroupRecord(group) {
  return {
    ...group,
    group_id: experimentGroupId(group) || String(group?.id || ""),
    legacy: Boolean(group?.legacy || group?.is_legacy),
    stage_one_count: Number(
      group?.stage_one_count ?? group?.stage_one_experiment_count ?? group?.interaction_count ?? 0,
    ),
    stage_two_count: Number(
      group?.stage_two_count ?? group?.stage_two_experiment_count ?? group?.tool_selection_count ?? 0,
    ),
  };
}

function experimentGroupFixedResources(group) {
  const resources = group?.fixed_resources || group?.resources || group?.validation || {};
  return {
    queries: group?.queries || resources.queries || resources.query_file || null,
    mcp: group?.mcp || resources.mcp || resources.mcp_file || null,
    target_mapping: group?.target_mapping || null,
  };
}

function experimentGroupResourceSummary(group) {
  const files = experimentGroupFixedResources(group);
  const counts = validationFileCounts(files);
  return `<div class="experiment-group-card-resources">
    <div class="group-resource-item">
      <span>${t("group.queries")}</span>
      <strong>${escapeHtml(files.queries?.filename || t("group.chooseQueryLater"))}</strong>
      <small>${files.queries ? t("validation.queryFileSummary", {
        filename: files.queries?.filename || "-", count: formatNumber(counts.queries),
      }) : t("group.queriesHelp")}</small>
    </div>
    <div class="group-resource-item">
      <span>${t("group.mcp")}</span>
      <strong>${escapeHtml(files.mcp?.filename || t("group.chooseMcpLater"))}</strong>
      <small>${files.mcp ? t("validation.mcpFileSummary", {
        filename: files.mcp?.filename || "-",
        servers: formatNumber(counts.servers), tools: formatNumber(counts.tools),
      }) : t("group.mcpHelp")}</small>
    </div>
    ${files.target_mapping ? `<div class="group-resource-item"><span>${t("mapping.title")}</span><strong>${escapeHtml(files.target_mapping.name)}</strong><small>${escapeHtml(files.target_mapping.filename)}</small></div>` : ""}
  </div>`;
}

function experimentGroupLifecycleButtons(group) {
  if (group.legacy) return "";
  const groupId = experimentGroupId(group);
  const archived = Boolean(group.archived);
  const allowed = archived ? group.can_restore !== false : group.can_archive !== false;
  const label = t(archived ? "group.restore" : "group.archive");
  const blockMessage = group.action_block_message || group.block_message || t("experiment.activeRunsBlocked");
  const deleteAllowed = group.can_delete === true;
  const deleteBlockMessage = archived ? t("experiment.activeRunsBlocked") : t("group.archiveBeforeDelete");
  return `<button
    type="button"
    class="${archived ? "secondary" : ""}"
    data-group-lifecycle="${escapeHtml(groupId)}"
    data-group-archived="${archived ? "true" : "false"}"
    data-block-message="${allowed ? "" : escapeHtml(blockMessage)}"
    title="${escapeHtml(allowed ? label : blockMessage)}"
    ${allowed ? "" : 'aria-disabled="true"'}
  >${label}</button>
  <button type="button" class="danger" data-delete-group="${escapeHtml(groupId)}"
    data-experiment-count="${Number(group.experiment_count || 0)}"
    data-run-count="${Number(group.run_count || 0)}"
    data-block-message="${deleteAllowed ? "" : escapeHtml(deleteBlockMessage)}"
    title="${escapeHtml(deleteAllowed ? t("group.delete") : deleteBlockMessage)}"
    ${deleteAllowed ? "" : 'aria-disabled="true"'}>${t("group.delete")}</button>`;
}

function experimentGroupCanDeleteDirect(group) {
  if (typeof group.can_delete_direct === "boolean") return group.can_delete_direct;
  return !group.legacy && !group.is_legacy && experimentGroupId(group) !== "legacy"
    && Number(group.active_run_count || 0) === 0;
}

function experimentGroupCard(group, {selectable = false} = {}) {
  const groupId = experimentGroupId(group);
  const experimentCount = Number(group.experiment_count ?? group.experiments?.length ?? 0);
  const stageOneCount = Number(group.stage_one_count ?? group.interaction_count ?? 0);
  const stageTwoCount = Number(group.stage_two_count ?? group.tool_selection_count ?? 0);
  return `<article class="experiment-group-card">
    <header class="experiment-group-card-header">
      ${selectable ? `<label class="group-delete-choice" title="${escapeHtml(experimentGroupCanDeleteDirect(group) ? t("groupOps.selectGroup", {name: group.name || groupId}) : t(group.legacy || group.is_legacy ? "group.deleteLegacyBlocked" : "experiment.activeRunsBlocked"))}"><input type="checkbox" name="group_batch_delete" value="${escapeHtml(groupId)}" aria-label="${escapeHtml(t("groupOps.selectGroup", {name: group.name || groupId}))}" ${experimentGroupCanDeleteDirect(group) ? "" : "disabled"}><span>${t("table.select")}</span></label>` : ""}
      <div>
        <a class="group-link" href="/experiment-groups/${encodeURIComponent(groupId)}"><h2>${escapeHtml(group.name || groupId)}</h2></a>
        <small>${escapeHtml(groupId)}</small>
      </div>
      <span class="tag ${group.legacy ? "locked" : group.archived ? "archived" : "active"}">${t(group.legacy ? "group.legacy" : group.archived ? "group.archived" : "group.active")}</span>
    </header>
    <p class="experiment-group-card-description">${escapeHtml(group.description || t("group.listDescription"))}</p>
    <dl class="experiment-group-card-stats">
      ${summaryItem(t("group.experimentCount"), formatNumber(experimentCount))}
      ${summaryItem(t("group.stageOne"), formatNumber(stageOneCount))}
      ${summaryItem(t("group.stageTwo"), formatNumber(stageTwoCount))}
      ${summaryItem(t("group.runCount"), formatNumber(group.run_count || 0))}
    </dl>
    ${group.legacy ? "" : experimentGroupResourceSummary(group)}
    <footer class="experiment-group-card-actions">
      <a class="button secondary" href="/experiment-groups/${encodeURIComponent(groupId)}">${t("group.open")}</a>
      ${group.legacy ? "" : experimentGroupLifecycleButtons(group)}
    </footer>
  </article>`;
}

function experimentGroupDeleteResult(result) {
  if (!result) return "";
  const failures = result.failures || [];
  const pending = (result.deleted || []).filter((item) => item.cleanup_pending);
  return `<div id="groupDeleteResult" class="group-delete-result" role="status">
    <strong>${t("group.bulkDeleted", {count: formatNumber(result.deleted_count || 0), experimentCount: formatNumber(result.deleted_experiment_count || 0), runCount: formatNumber(result.deleted_runs || 0)})}</strong>
    ${failures.length ? `<p>${t("group.bulkDeleteFailures", {count: formatNumber(failures.length)})}</p><ul>${failures.map((item) => `<li>${escapeHtml(item.group_id)}: ${escapeHtml(item.error)}</li>`).join("")}</ul>` : ""}
    ${pending.length ? `<p>${t("group.bulkCleanupPending")}</p><ul>${pending.map((item) => `<li>${escapeHtml(item.group_id)}: ${escapeHtml(typeof item.cleanup_pending === "string" ? item.cleanup_pending : JSON.stringify(item.cleanup_pending))}</li>`).join("")}</ul>` : ""}
  </div>`;
}

async function renderExperimentGroups({experimentsEntry = false, deleteResult = null} = {}) {
  const payload = await apiGet("/api/platform/experiment-groups?archived=all");
  const groups = normalizeExperimentGroups(payload);
  main.innerHTML = `<div class="page">
    ${pageHeader(t("group.title"), t("group.listDescription"), `
      <a class="button secondary" href="/experiments?view=all">${t("group.viewAllExperiments")}</a>
      <a class="button" href="/experiment-groups/new">${t("group.new")}</a>
      ${experimentTransferActions()}
    `)}
    ${experimentGroupDeleteResult(deleteResult)}
    ${groups.length
      ? `<section class="group-delete-toolbar">
          <div><label class="transfer-select-all"><input type="checkbox" id="groupDeleteSelectAll"><span>${t("groupOps.selectAllGroups")}</span></label><span id="groupDeleteSelectionCount" aria-live="polite"></span></div>
          <div class="group-operation-actions"><button type="button" class="secondary" id="mergeSelectedGroups" disabled>${t("groupOps.merge")}</button><button type="button" class="danger" id="deleteSelectedGroups" disabled>${t("group.bulkDelete")}</button></div>
          <p class="muted">${t("groupOps.listHelp")} ${t("group.bulkDeleteHelp")}</p>
        </section><div class="experiment-group-grid">${groups.map((group) => experimentGroupCard(group, {selectable: true})).join("")}</div>`
      : emptyState(t("group.empty"))}
  </div>`;
  bindExperimentGroupBatchDelete(main, groups, (result) => renderExperimentGroups({experimentsEntry, deleteResult: result}));
  if (typeof bindExperimentGroupMerge === "function") bindExperimentGroupMerge(main, groups);
  bindExperimentGroupLifecycleActions(main);
  bindExperimentTransfers(main);
}

function bindExperimentGroupBatchDelete(root, groups, onRefresh) {
  const choices = [...root.querySelectorAll('[name="group_batch_delete"]')];
  const selectable = choices.filter((input) => !input.disabled);
  const selectAll = root.querySelector("#groupDeleteSelectAll");
  const count = root.querySelector("#groupDeleteSelectionCount");
  const button = root.querySelector("#deleteSelectedGroups");
  if (!button || !selectAll || !count) return;
  let busy = false;
  const selectedChoices = () => selectable.filter((input) => input.checked);
  const update = () => {
    const selected = selectedChoices().length;
    selectAll.checked = selectable.length > 0 && selected === selectable.length;
    selectAll.indeterminate = selected > 0 && selected < selectable.length;
    selectAll.disabled = busy || !selectable.length;
    button.disabled = busy || !selected;
    root.dataset.groupOperationBusy = String(busy);
    const merge = root.querySelector("#mergeSelectedGroups");
    if (busy && merge) merge.disabled = true;
    root.dispatchEvent?.(new Event("group-selection-updated"));
    count.textContent = t("group.bulkDeleteSelected", {selected, total: selectable.length});
    choices.forEach((input) => input.closest(".experiment-group-card")?.classList.toggle("is-selected", input.checked));
  };
  selectAll.addEventListener("change", () => {
    if (busy) return;
    selectable.forEach((input) => { input.checked = selectAll.checked; });
    update();
  });
  selectable.forEach((input) => input.addEventListener("change", update));
  button.addEventListener("click", async () => {
    if (busy) return;
    const ids = selectedChoices().map((input) => input.value);
    if (!ids.length) { showToast(t("group.bulkDeleteSelect")); return; }
    const selected = groups.filter((group) => ids.includes(experimentGroupId(group)));
    const experimentCount = selected.reduce((total, group) => total + Number(group.experiment_count || 0), 0);
    const runCount = selected.reduce((total, group) => total + Number(group.run_count || 0), 0);
    if (!window.confirm(t("group.bulkDeleteConfirm", {
      count: formatNumber(ids.length), experimentCount: formatNumber(experimentCount), runCount: formatNumber(runCount),
      groups: selected.map((group) => `${group.name || experimentGroupId(group)} (${experimentGroupId(group)})`).join("\n"),
    }))) return;
    busy = true;
    selectable.forEach((input) => { input.disabled = true; });
    root.querySelectorAll("[data-group-lifecycle], [data-delete-group]").forEach((control) => { control.disabled = true; });
    update();
    try {
      const result = await apiPost("/api/platform/experiment-groups/batch-delete", {group_ids: ids});
      try { await onRefresh(result); }
      catch (error) { showToast(t("group.bulkRefreshFailed")); }
    } catch (error) {
      busy = false;
      selectable.forEach((input) => { input.disabled = false; });
      root.querySelectorAll("[data-group-lifecycle], [data-delete-group]").forEach((control) => { control.disabled = false; });
      update();
      showToast(t("group.deleteFailed", {message: error.message}));
    }
  });
  update();
}

function experimentGroupMembers(group, payload, allExperiments) {
  const staged = [
    ...(payload?.stage_one_experiments || group?.stage_one_experiments || []),
    ...(payload?.stage_two_experiments || group?.stage_two_experiments || []),
  ];
  if (staged.length) return staged;
  const embedded = payload?.experiments || group?.experiments;
  if (Array.isArray(embedded)) return embedded;
  const groupId = experimentGroupId(group);
  return (allExperiments || []).filter((experiment) => experimentGroupId(experiment) === groupId);
}

async function renderExperimentGroupDetail(groupId) {
  const [payload, experimentPayload, mappingsPayload] = await Promise.all([
    apiGet(`/api/platform/experiment-groups/${encodeURIComponent(groupId)}`),
    apiGet(`/api/platform/experiments?archived=all&group_id=${encodeURIComponent(groupId)}`),
    apiGet("/api/platform/target-mappings"),
  ]);
  const group = normalizeExperimentGroup(payload);
  if (!group || !experimentGroupId(group)) throw new Error(t("group.missing"));
  const experiments = experimentGroupMembers(group, payload, experimentPayload.experiments || []);
  const stageOne = Array.isArray(payload?.stage_one_experiments)
    ? payload.stage_one_experiments
    : experiments.filter((item) => experimentListStage(item) === "interaction");
  const stageTwo = Array.isArray(payload?.stage_two_experiments)
    ? payload.stage_two_experiments
    : experiments.filter((item) => experimentListStage(item) === "tool_selection");
  const launchableExperiments = experiments.filter((item) => !item.archived && canLaunchExperiment(item));
  const canCreate = !group.archived && !group.legacy && group.can_create !== false;
  main.innerHTML = `<div class="page">
    ${pageHeader(escapeHtml(group.name || groupId), escapeHtml(group.description || ""), `
      <a class="button secondary" href="/experiment-groups">${t("common.back")}</a>
      ${experimentTransferActions(groupId)}
      ${typeof experimentGroupOperationActions === "function" ? experimentGroupOperationActions(group, experiments) : ""}
      ${canCreate ? `<a class="button secondary" href="/experiments/new?group_id=${encodeURIComponent(groupId)}&stage=interaction">${t("group.createStageOne")}</a>
        <a class="button" href="/experiments/new?group_id=${encodeURIComponent(groupId)}&stage=tool_selection">${t("group.createStageTwo")}</a><a class="button secondary" href="/experiments/new?group_id=${encodeURIComponent(groupId)}&stage=tool_selection&memory_source_mode=specified">${t("validation.createSpecifiedMemory")}</a><a class="button secondary" href="/experiment-groups/${encodeURIComponent(groupId)}/quick">${t("quick.nav")}</a>` : ""}
      ${group.legacy ? "" : experimentGroupLifecycleButtons(group)}
    `)}
    ${group.legacy ? `<p class="archived-notice">${t("group.legacyDescription")}</p>` : group.archived ? `<p class="archived-notice">${t("group.archivedCannotCreate")}</p>` : group.can_create === false ? `<p class="archived-notice">${t("group.unregisteredCannotCreate")}</p>` : ""}
    <div class="stat-grid">
      ${statItem(t("group.id"), escapeHtml(groupId), t(group.legacy ? "group.legacy" : group.archived ? "group.archived" : "group.active"))}
      ${statItem(t("group.experimentCount"), formatNumber(experiments.length), t("group.runCountValue", {count: formatNumber(group.run_count || 0)}))}
      ${statItem(t("group.stageOne"), formatNumber(stageOne.length), t("validation.phaseOneShort"))}
      ${statItem(t("group.stageTwo"), formatNumber(stageTwo.length), t("validation.phaseTwoShort"))}
    </div>
    ${group.legacy ? "" : `<section class="section">
      ${sectionHeading(t("group.fixedResources"))}
      <p class="group-fixed-resource-note">${t("group.fixedResourcesHelp")}</p>
      ${experimentGroupResourceSummary(group)}
      ${!group.archived ? `<form id="groupTargetMappingForm" class="group-target-mapping-form"><label for="groupDetailTargetMapping">${t("mapping.title")}</label><select id="groupDetailTargetMapping"><option value="">${t("mapping.none")}</option>${(mappingsPayload.mappings || []).map((mapping) => `<option value="${escapeHtml(mapping.mapping_id)}" ${mapping.mapping_id === group.target_mapping_id ? "selected" : ""}>${escapeHtml(mapping.name)} · ${escapeHtml(mapping.filename)}</option>`).join("")}</select><button type="submit" class="secondary">${t("mapping.save")}</button></form>` : ""}
    </section>`}
    ${!group.archived && !group.legacy ? `<section class="section group-batch-launch">
      ${sectionHeading(t("group.batchTitle"))}
      <p class="muted">${t("group.batchHelp")}</p>
      ${launchableExperiments.length ? `<div class="group-batch-toolbar">
        <label class="transfer-select-all"><input type="checkbox" id="groupBatchSelectAll" checked><span>${t("transfer.selectAll")}</span></label>
        <span class="transfer-selection-count" id="groupBatchSelectionCount" role="status"></span>
      </div>
      <div class="group-batch-choices">${launchableExperiments.map((item) => `<label class="transfer-choice group-batch-choice"><input type="checkbox" name="group_batch_experiment" value="${escapeHtml(item.experiment_id)}" checked><span class="transfer-choice-copy"><strong>${escapeHtml(item.name || item.experiment_id)}</strong><small>${escapeHtml(item.experiment_id)}</small></span><span class="group-batch-stage">${t(experimentListStage(item) === "tool_selection" ? "group.stageTwo" : "group.stageOne")}</span></label>`).join("")}</div>` : emptyState(t("group.noStageOne"))}
      <button type="button" id="launchGroupRuns" ${launchableExperiments.length ? "" : "disabled"}>${t("group.batchLaunch")}</button>
      <p id="groupBatchResult" role="status"></p>
    </section>` : ""}
    <div class="group-stage-grid">
      <section class="section group-stage-section">
        <div class="group-stage-heading">
          ${sectionHeading(t("group.stageOne"), canCreate ? `<a href="/experiments/new?group_id=${encodeURIComponent(groupId)}&stage=interaction">${t("group.createStageOne")}</a>` : "")}
        </div>
        ${stageOne.length ? experimentsTable(stageOne, {management: true, groupId}) : emptyState(t("group.noStageOne"))}
      </section>
      <section class="section group-stage-section">
        <div class="group-stage-heading">
          ${sectionHeading(t("group.stageTwo"), canCreate ? `<a href="/experiments/new?group_id=${encodeURIComponent(groupId)}&stage=tool_selection">${t("group.createStageTwo")}</a>` : "")}
        </div>
        ${stageTwo.length ? experimentsTable(stageTwo, {management: true, groupId}) : emptyState(t("group.noStageTwo"))}
      </section>
    </div>
  </div>`;
  bindExperimentGroupLifecycleActions(main);
  bindExperimentActions(main);
  bindExperimentTransfers(main);
  if (typeof bindExperimentGroupOperations === "function") bindExperimentGroupOperations(main, group, experiments);
  main.querySelector("#groupTargetMappingForm")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await apiPost(`/api/platform/experiment-groups/${encodeURIComponent(groupId)}/target-mapping`, {
        target_mapping_id: main.querySelector("#groupDetailTargetMapping").value || null,
      });
      rememberToast(t("mapping.saved")); location.reload();
    } catch (error) { button.disabled = false; showToast(error.message); }
  });
  const batchChoices = [...main.querySelectorAll('[name="group_batch_experiment"]')];
  const batchSelectAll = main.querySelector("#groupBatchSelectAll");
  const launchGroupRuns = main.querySelector("#launchGroupRuns");
  const updateBatchSelection = () => {
    if (!batchSelectAll) return;
    const selected = batchChoices.filter((input) => input.checked).length;
    batchSelectAll.checked = selected === batchChoices.length;
    batchSelectAll.indeterminate = selected > 0 && !batchSelectAll.checked;
    main.querySelector("#groupBatchSelectionCount").textContent = t("transfer.selectedCount", {selected, total: batchChoices.length});
    launchGroupRuns.disabled = selected === 0;
  };
  batchSelectAll?.addEventListener("change", () => {
    batchChoices.forEach((input) => { input.checked = batchSelectAll.checked; });
    updateBatchSelection();
  });
  batchChoices.forEach((input) => input.addEventListener("change", updateBatchSelection));
  updateBatchSelection();
  launchGroupRuns?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const ids = [...main.querySelectorAll('[name="group_batch_experiment"]:checked')].map((input) => input.value);
    if (!ids.length) { showToast(t("group.batchSelect")); return; }
    button.disabled = true;
    try {
      const result = await apiPost(`/api/platform/experiment-groups/${encodeURIComponent(groupId)}/runs`, {experiment_ids: ids});
      const status = main.querySelector("#groupBatchResult");
      status.innerHTML = `${t("group.batchStarted", {count: result.started.length})}${result.failures.length ? ` · ${t("group.batchFailed", {count: result.failures.length})}: ${result.failures.map((item) => `${escapeHtml(item.experiment_id)} (${escapeHtml(item.error)})`).join("; ")}` : ""} <a href="/runs?group_id=${encodeURIComponent(groupId)}">${t("nav.runs")}</a>`;
      updateBatchSelection();
    } catch (error) { updateBatchSelection(); showToast(error.message); }
  });
}

function bindExperimentGroupLifecycleActions(root) {
  root.querySelectorAll("[data-group-lifecycle]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (button.getAttribute("aria-disabled") === "true") {
        showToast(button.dataset.blockMessage || t("experiment.activeRunsBlocked"));
        return;
      }
      const groupId = button.dataset.groupLifecycle;
      const archived = button.dataset.groupArchived === "true";
      const nextArchived = !archived;
      if (!window.confirm(t(nextArchived ? "group.archiveConfirm" : "group.restoreConfirm", {id: groupId}))) return;
      button.disabled = true;
      try {
        await apiPost(`/api/platform/experiment-groups/${encodeURIComponent(groupId)}/archive`, {archived: nextArchived});
        rememberToast(t(nextArchived ? "group.archivedSuccess" : "group.restoredSuccess"));
        location.reload();
      } catch (error) {
        button.disabled = false;
        showToast(t("group.lifecycleFailed", {message: error.message}));
      }
    });
  });
  root.querySelectorAll("[data-delete-group]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (button.getAttribute("aria-disabled") === "true") {
        showToast(button.dataset.blockMessage || t("group.archiveBeforeDelete"));
        return;
      }
      const groupId = button.dataset.deleteGroup;
      if (!window.confirm(t("group.deleteConfirm", {
        id: groupId,
        experimentCount: formatNumber(button.dataset.experimentCount || 0),
        runCount: formatNumber(button.dataset.runCount || 0),
      }))) return;
      button.disabled = true;
      try {
        await apiDelete(`/api/platform/experiment-groups/${encodeURIComponent(groupId)}`);
        rememberToast(t("group.deletedSuccess"));
        location.href = "/experiment-groups";
      } catch (error) {
        button.disabled = false;
        showToast(t("group.deleteFailed", {message: error.message}));
      }
    });
  });
}

function experimentGroupResourceField(kind, label, help, resources, {required = true} = {}) {
  return `<div class="group-resource-import">
    <label for="groupResource-${kind}">${label}</label>
    <select id="groupResource-${kind}" ${required ? "required" : ""} aria-describedby="groupResourceHelp-${kind}">
      <option value="">${t(required ? "resource.chooseRevision" : kind === "mcp" ? "group.chooseMcpLater" : "group.chooseQueryLater")}</option>
      ${resourceRevisionOptions(resources)}
    </select>
    <small id="groupResourceHelp-${kind}">${help} <a href="/resources">${t("resource.openRegistry")}</a></small>
  </div>`;
}

async function renderExperimentGroupEditor() {
  const [resourcePayload, mappingsPayload] = await Promise.all([
    apiGet("/api/platform/resources?include_content=false"),
    apiGet("/api/platform/target-mappings"),
  ]);
  const resources = resourcePayload.resources || [];
  const queryResources = resources.filter((resource) => resource.kind === "queries");
  const mcpResources = resources.filter((resource) => resource.kind === "mcp");
  main.innerHTML = `<div class="page">
    ${pageHeader(t("group.new"), t("group.newDescription"), `<a class="button secondary" href="/experiment-groups">${t("common.cancel")}</a>`)}
    <form id="experimentGroupForm" class="group-form">
      <section class="section">
        <div class="form-grid">
          ${textField("group_id", t("group.id"), "", "", 'required pattern="[a-z0-9_-]{1,64}"')}
          ${textField("group_name", t("group.name"), "", "", "required")}
          ${textareaField("group_description", t("group.description"), "", "wide")}
        </div>
      </section>
      <section class="section">
        ${sectionHeading(t("group.fixedResources"))}
        <p class="group-fixed-resource-note">${t("resource.groupHelp")}</p>
        <div class="form-grid">
          ${experimentGroupResourceField("queries", t("group.queriesOptional"), t("group.queriesHelp"), queryResources, {required: false})}
          ${experimentGroupResourceField("mcp", t("group.mcpOptional"), t("group.mcpHelp"), mcpResources, {required: false})}
          <div class="group-resource-import"><label for="groupTargetMapping">${t("mapping.title")}</label><select id="groupTargetMapping"><option value="">${t("mapping.none")}</option>${(mappingsPayload.mappings || []).map((mapping) => `<option value="${escapeHtml(mapping.mapping_id)}">${escapeHtml(mapping.name)} · ${escapeHtml(mapping.filename)}</option>`).join("")}</select><small>${t("mapping.groupHelp")}</small></div>
        </div>
      </section>
      <div class="page-actions">
        <a class="button secondary" href="/experiment-groups">${t("common.cancel")}</a>
        <button type="submit" disabled>${t("group.new")}</button>
      </div>
    </form>
  </div>`;
  const form = document.querySelector("#experimentGroupForm");
  const sync = () => {
    const busy = form.dataset.busy === "true";
    ["queries", "mcp"].forEach((kind) => {
      form.querySelector(`#groupResource-${kind}`).disabled = busy;
    });
    form.querySelector('button[type="submit"]').disabled = busy;
  };
  ["queries", "mcp"].forEach((kind) => form.querySelector(`#groupResource-${kind}`).addEventListener("change", sync));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (form.dataset.busy === "true" || !form.reportValidity()) return;
    form.dataset.busy = "true";
    sync();
    try {
      const payload = {
        group_id: fieldValue(form, "group_id"),
        name: fieldValue(form, "group_name"),
        description: fieldValue(form, "group_description"),
        query_revision_id: form.querySelector("#groupResource-queries").value || null,
        mcp_revision_id: form.querySelector("#groupResource-mcp").value || null,
        target_mapping_id: form.querySelector("#groupTargetMapping").value || null,
      };
      const result = await apiPost("/api/platform/experiment-groups", payload);
      const saved = normalizeExperimentGroup(result);
      rememberToast(t("group.createSuccess"));
      location.href = `/experiment-groups/${encodeURIComponent(experimentGroupId(saved) || payload.group_id)}`;
    } catch (error) {
      delete form.dataset.busy;
      sync();
      showToast(t("group.createFailed", {message: error.message}));
    }
  });
  sync();
}

async function renderExperiments() {
  if (new URLSearchParams(location.search).get("view") !== "all") {
    return renderExperimentGroups({experimentsEntry: true});
  }
  const data = await apiGet("/api/platform/experiments?archived=all");
  const experiments = data.experiments || [];
  const activeExperiments = experiments.filter((item) => !item.archived);
  const stageExperiments = {
    interaction: activeExperiments.filter((item) => experimentListStage(item) === "interaction"),
    tool_selection: activeExperiments.filter((item) => experimentListStage(item) === "tool_selection"),
    archived: experiments.filter((item) => item.archived),
  };
  const unclassifiedExperiments = activeExperiments.filter((item) => experimentListStage(item) === null);
  const stageDefinitions = [
    {
      id: "interaction",
      label: t("validation.phaseOneShort"),
      description: t("experiment.interactionStageDescription"),
      emptyMessage: t("empty.interactionExperiments"),
    },
    {
      id: "tool_selection",
      label: t("validation.phaseTwoShort"),
      description: t("experiment.toolSelectionStageDescription"),
      emptyMessage: t("empty.toolSelectionExperiments"),
    },
    {
      id: "archived",
      label: t("experiment.archived"),
      description: t("experiment.archivedStageDescription"),
      emptyMessage: t("empty.archivedExperiments"),
    },
  ];
  const validStages = new Set(stageDefinitions.map((stage) => stage.id));
  const requestedStage = new URLSearchParams(location.search).get("stage");
  const initialStage = validStages.has(requestedStage) ? requestedStage : "interaction";
  const activeDefinition = stageDefinitions.find((stage) => stage.id === initialStage);
  if (requestedStage !== initialStage) {
    const url = new URL(location.href);
    url.searchParams.set("stage", initialStage);
    history.replaceState(history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }

  function stageHref(stage) {
    const url = new URL(location.href);
    url.searchParams.set("stage", stage);
    return `${url.pathname}${url.search}${url.hash}`;
  }

  const currentExperiments = stageExperiments[initialStage];
  const selectableStage = initialStage !== "archived";

  main.innerHTML = `
    <div class="page">
      ${pageHeader(t("experiment.title"), "", `
        <a class="button secondary" href="/experiment-groups">${t("group.title")}</a>
        <a class="button" href="/experiments/new">${t("dashboard.newExperiment")}</a>
        ${experimentTransferActions()}
      `)}
      <section class="experiment-stage-section" aria-label="${escapeHtml(t("experiment.stageFilterLabel"))}">
        <div class="experiment-stage-switcher" role="tablist" aria-label="${escapeHtml(t("experiment.stageFilterLabel"))}">
          ${stageDefinitions.map((stage) => {
            const count = stageExperiments[stage.id].length;
            const selected = stage.id === initialStage;
            return `<a
              class="experiment-stage-tab"
              id="experimentStageTab-${stage.id}"
              role="tab"
              href="${escapeHtml(stageHref(stage.id))}"
              aria-controls="experimentStagePanel"
              aria-label="${escapeHtml(t("experiment.stageTabLabel", {stage: stage.label, count: formatNumber(count)}))}"
              aria-selected="${selected ? "true" : "false"}"
              tabindex="${selected ? "0" : "-1"}"
            ><span>${stage.label}</span><span class="experiment-stage-count" aria-hidden="true">${formatNumber(count)}</span></a>`;
          }).join("")}
        </div>
        <p class="experiment-stage-description">${activeDefinition.description}</p>
        ${initialStage !== "archived" && unclassifiedExperiments.length ? `<p class="experiment-stage-warning" role="status">${escapeHtml(t("experiment.unclassifiedWarning", {count: formatNumber(unclassifiedExperiments.length)}))}</p>` : ""}
        <div class="experiment-stage-panel" id="experimentStagePanel" role="tabpanel" tabindex="0" aria-labelledby="experimentStageTab-${initialStage}">
          ${selectableStage && currentExperiments.length ? `<div class="experiment-bulk-actions">
            <span id="selectedExperimentCount" aria-live="polite">${t("experiment.selectedForArchive", {count: 0})}</span>
            <button id="archiveSelectedExperiments" type="button" disabled>${t("experiment.archiveSelected")}</button>
          </div>` : ""}
          ${currentExperiments.length
            ? experimentsTable(currentExperiments, {management: true, selectable: selectableStage})
            : emptyState(activeDefinition.emptyMessage)}
        </div>
      </section>
    </div>`;

  const tabs = Array.from(main.querySelectorAll(".experiment-stage-tab"));
  tabs.forEach((tab, index) => {
    tab.addEventListener("keydown", (event) => {
      let nextIndex = null;
      if (["ArrowRight", "ArrowDown"].includes(event.key)) nextIndex = (index + 1) % tabs.length;
      if (["ArrowLeft", "ArrowUp"].includes(event.key)) nextIndex = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") nextIndex = 0;
      if (event.key === "End") nextIndex = tabs.length - 1;
      if (nextIndex === null) return;
      event.preventDefault();
      location.href = tabs[nextIndex].href;
    });
  });
  bindExperimentActions(main);
  bindExperimentBulkArchive(main);
  bindExperimentTransfers(main);
}

async function renderExperimentDetail(experimentId) {
  const experiment = await apiGet(`/api/platform/experiments/${encodeURIComponent(experimentId)}`);
  if (isToolSelection(experiment)) return renderValidationExperimentDetail(experiment);
  const config = experiment.config;
  const importedFile = config.environment?.file;
  const registeredPostResource = experiment.resource_refs?.posts;
  const importedPostCount = registeredPostResource?.summary?.post_count
    ?? importedFile?.dataset?.posts?.length;
  const importedFileLabel = registeredPostResource
    ? [
        registeredPostResource.resource_name,
        `r${registeredPostResource.revision_number}`,
        registeredPostResource.filename,
      ].filter(Boolean).join(" · ")
    : importedFile?.filename || importedFile?.resource_revision_id || "-";
  const environmentAction = importedFile
    ? `<span>${escapeHtml(importedFileLabel)}${importedPostCount === undefined ? "" : ` · ${t("editor.filePostCount", {count: importedPostCount})}`}</span>`
    : `<span>${t("experiment.historicalSource")}</span>`;
  const configurationAction = experiment.archived
    ? `<span class="tag archived">${t("experiment.archived")}</span>`
    : (experiment.configuration_locked || !canLaunchExperiment(experiment))
      ? `<span class="tag locked">${t("experiment.configurationLocked")}</span>`
      : `<a class="button secondary" href="/experiments/${encodeURIComponent(experimentId)}?edit=1">${t("common.edit")}</a>`;
  main.innerHTML = `
    <div class="page">
      ${pageHeader(escapeHtml(experiment.name), escapeHtml(experiment.description || ""), `
        ${configurationAction}
        ${experimentLifecycleButtons(experiment, {includeLaunch: isSupportedReadingExperiment(experiment)})}
      `)}
      ${experiment.archived ? `<p class="archived-notice">${t("experiment.archivedDescription")}</p>` : ""}
      <div class="stat-grid">
        ${statItem(t("experiment.id"), escapeHtml(experiment.experiment_id), escapeHtml(experiment.runner_type))}
        ${experimentGroupId(experiment) ? statItem(t("group.title"), experimentGroupLink(experiment), escapeHtml(experimentGroupId(experiment))) : ""}
        ${statItem(t("experiment.runCount"), formatNumber(experiment.run_count ?? experiment.runs.length), statusLabel(experiment.latest_run_status || "not_run"))}
        ${statItem(t("experiment.agentCount"), formatNumber(config.agents?.count || 0), t("experiment.roundCount", {count: formatNumber(config.simulation?.rounds || 0)}))}
        ${statItem(t("editor.readOnlyAgent"), escapeHtml(config.simulation?.read_only_agent_id || "-"), "")}
        ${statItem(t("experiment.memory"), escapeHtml(memoryStrategyLabel(config.memory?.strategy || "langmem_official")),  escapeHtml(config.environment?.type || "moltnet_thread"))}
        ${config.memory?.model ? statItem(t("editor.memoryModel"), escapeHtml(config.memory.model), "") : ""}
      </div>
      <section class="section">
        <div class="toolbar">
          <div class="field grow">
            <label>${t("experiment.exportedConfig")}</label>
            <input value="${escapeHtml(relativePath(experiment.config_path))}" readonly />
          </div>
          <div class="field">
            <label>${t("experiment.sourcePost")}</label>
            ${environmentAction}
          </div>
          ${experiment.dependent_experiment_ids?.length ? `<div class="field">
            <label>${t("experiment.linkedStageTwo")}</label>
            <span>${experimentIdLinks(experiment.dependent_experiment_ids)}</span>
          </div>` : ""}
        </div>
      </section>
      <section class="section">
        ${sectionHeading(t("experiment.runHistory"), `<button class="secondary" id="compareRuns" disabled>${t("runs.compareSelected", {count: 0})}</button>`)}
        <div id="experimentRuns">${runsTable(experiment.runs, {selectable: true})}</div>
      </section>
    </div>`;
  document.querySelector("#launchRun")?.addEventListener("click", async (event) => {
    await launchExperimentRun(experimentId, event.currentTarget);
  });
  document.querySelector("#compareRuns").addEventListener("click", compareSelectedRuns);
  bindExperimentActions(main);
  bindRunActions(main);
  bindRunComparison(document.querySelector("#experimentRuns"), document.querySelector("#compareRuns"));
  startRunTablePolling(
    "#experimentRuns",
    experiment.runs,
    async (signal) => (
      await apiGet(`/api/platform/experiments/${encodeURIComponent(experimentId)}`, {signal})
    ).runs,
    {selectable: true},
  );
}

async function launchExperimentRun(experimentId, button) {
  button.disabled = true;
  button.textContent = t("experiment.launching");
  try {
    const run = await apiPost(`/api/platform/experiments/${encodeURIComponent(experimentId)}/runs`, {});
    location.href = `/runs/${encodeURIComponent(run.run_id)}`;
  } catch (error) {
    button.disabled = false;
    button.textContent = t("experiment.launchRun");
    showToast(error.message);
  }
}

function validationConfigSummary(config) {
  const validation = config.validation || {};
  const specified = validation.memory_source_mode === "specified";
  const source = validation.memory_source || {};
  const sourceExperimentId = validation.source_experiment_id || source.experiment_id;
  const counts = validationFileCounts(validation);
  const targets = validationTargetAnalysis(validation);
  const targetMode = validation.target_mode || "custom";
  const targetSummary = targetMode === "none"
    ? validationTargetModeLabel(targetMode)
    : `${validationTargetModeLabel(targetMode)} · ${t("validation.targetsConfigured", {configured: targets.configured, total: targets.total})}`;
  return `<dl class="summary-list">
    ${summaryItem(t("validation.stage"), t("validation.phaseTwo"))}
    ${summaryItem(t("validation.memorySourceMode"), t(specified ? "validation.memorySourceSpecified" : "validation.memorySourceRun"))}
    ${specified ? summaryItem(t("validation.agentId"), validation.specified_agent_id || "ROWAN")
      + summaryItem(t("validation.specifiedMemoryScenes"), Object.keys(validation.specified_memory || {}).length)
      : `
    <div><dt>${t("validation.sourceExperiment")}</dt><dd>${sourceExperimentId ? `<a class="field-link" href="/experiments/${encodeURIComponent(sourceExperimentId)}">${escapeHtml(sourceExperimentId)}</a>` : "-"}</dd></div>
    <div><dt>${t("validation.sourceRun")}</dt><dd>${validation.source_run_id ? `<a class="field-link" href="/runs/${encodeURIComponent(validation.source_run_id)}">${escapeHtml(validation.source_run_id)}</a>` : "-"}</dd></div>
    ${summaryItem(t("validation.agent"), source.profile?.display_name || validation.source_agent_id || "-")}
    ${summaryItem(t("validation.agentId"), validation.source_agent_id || "-")}
    ${summaryItem(t("validation.memoryRound"), validation.source_round ?? source.round_index ?? "-")}
    ${summaryItem(t("summary.memory"), memoryStrategyLabel(config.memory?.strategy || source.memory_strategy || "langmem_official"))}
    ${summaryItem(t("validation.memoryRevision"), source.snapshot?.revision ?? "-")}
    `}
    ${summaryItem(t("validation.queryCount"), counts.queries)}
    ${summaryItem(t("validation.targets"), targetSummary)}
    ${summaryItem(t("validation.mcpCount"), t("validation.mcpCountValue", {servers: counts.servers, tools: counts.tools}))}
    ${summaryItem(t("validation.model"), config.backend?.model || t("validation.inheritedModel"))}
    ${summaryItem(t("validation.connectionMode"), validationConnectionLabel(config))}
  </dl>`;
}

function renderValidationExperimentDetail(experiment) {
  const experimentId = experiment.experiment_id;
  const config = experiment.config;
  const counts = validationFileCounts(config.validation);
  const targets = validationTargetAnalysis(config.validation);
  const targetMode = config.validation?.target_mode || "custom";
  const targetSummary = targetMode === "none"
    ? validationTargetModeLabel(targetMode)
    : t("validation.targetsConfigured", {configured: targets.configured, total: targets.total});
  const configurationAction = experiment.archived
    ? `<span class="tag archived">${t("experiment.archived")}</span>`
    : (experiment.configuration_locked || !canLaunchExperiment(experiment))
      ? `<span class="tag locked">${t("experiment.configurationLocked")}</span>`
      : `<a class="button secondary" href="/experiments/${encodeURIComponent(experimentId)}?edit=1">${t("common.edit")}</a>`;
  main.innerHTML = `<div class="page">
    ${pageHeader(escapeHtml(experiment.name), escapeHtml(experiment.description || ""), `
      ${configurationAction}${experimentLifecycleButtons(experiment, {includeLaunch: canLaunchExperiment(experiment)})}
    `)}
    ${experiment.archived ? `<p class="archived-notice">${t("experiment.archivedDescription")}</p>` : ""}
    <p class="validation-stage-label"><span class="tag blue">${t("validation.phaseTwo")}</span></p>
    <div class="stat-grid">
      ${statItem(t("experiment.id"), escapeHtml(experimentId), t("validation.phaseTwoShort"))}
      ${experimentGroupId(experiment) ? statItem(t("group.title"), experimentGroupLink(experiment), escapeHtml(experimentGroupId(experiment))) : ""}
      ${statItem(t("experiment.runCount"), formatNumber(experiment.run_count ?? experiment.runs.length), statusLabel(experiment.latest_run_status || "not_run"))}
      ${statItem(t("validation.queryCount"), formatNumber(counts.queries), targetSummary)}
      ${statItem(t("validation.toolCount"), formatNumber(counts.tools), t("validation.serverCountValue", {count: formatNumber(counts.servers)}))}
    </div>
    <div class="validation-definition">
      <section class="run-panel">
        <h2>${t(config.validation?.memory_source_mode === "specified" ? "validation.specifiedMemoryTitle" : "validation.memorySource")}</h2>
        ${validationConfigSummary(config)}
        ${config.validation?.memory_source_mode === "specified" ? Object.entries(config.validation.specified_memory || {}).map(([group, memory]) => `<details><summary>${escapeHtml(group === "__ungrouped__" ? t("validation.uncategorized") : group)}</summary>${memory === "" ? `<p>${t("validation.specifiedMemoryEmpty")}</p>` : `<pre>${escapeHtml(memory)}</pre>`}</details>`).join("") : ""}
      </section>
      <section class="run-panel">
        <h2>${t("validation.files")}</h2>
        <dl class="summary-list">
          ${summaryItem(t("validation.queryFile"), config.validation?.queries?.filename || "-")}
          ${summaryItem(t("validation.mcpFile"), config.validation?.mcp?.filename || "-")}
          ${summaryItem(t("experiment.exportedConfig"), relativePath(experiment.config_path))}
        </dl>
        <p class="validation-notice">${t(config.validation?.memory_source_mode === "specified" ? "validation.specifiedMemoryHelp" : "validation.frozenMemoryHelp")}</p>
      </section>
    </div>
    <section class="section">
      ${sectionHeading(t("experiment.runHistory"), `<button class="secondary" id="compareRuns" disabled>${t("runs.compareSelected", {count: 0})}</button>`)}
      <div id="experimentRuns">${runsTable(experiment.runs, {selectable: true})}</div>
    </section>
  </div>`;
  document.querySelector("#launchRun")?.addEventListener("click", (event) => launchExperimentRun(experimentId, event.currentTarget));
  document.querySelector("#compareRuns").addEventListener("click", compareSelectedRuns);
  bindExperimentActions(main);
  bindRunActions(main);
  bindRunComparison(document.querySelector("#experimentRuns"), document.querySelector("#compareRuns"));
  startRunTablePolling(
    "#experimentRuns", experiment.runs,
    async (signal) => (
      await apiGet(`/api/platform/experiments/${encodeURIComponent(experimentId)}`, {signal})
    ).runs,
    {selectable: true},
  );
}

async function renderExperimentEditor(experimentId) {
  const isNew = !experimentId;
  const editorQuery = new URLSearchParams(location.search);
  const requestedStage = isNew ? editorQuery.get("stage") : "";
  const historyImportStage = ["interaction", "tool_selection"].includes(requestedStage)
    ? requestedStage
    : "interaction";
  const importExperimentId = isNew
    ? new URLSearchParams(location.search).get("import_from")?.trim() || ""
    : "";
  const requestedGroupId = isNew ? editorQuery.get("group_id")?.trim() || "" : "";
  const groupQuery = requestedGroupId ? `?group_id=${encodeURIComponent(requestedGroupId)}` : "";
  const historyExperimentQuery = historyImportStage === "interaction"
    ? "/api/platform/experiments?archived=all&stage=interaction"
    : `/api/platform/experiments?archived=all&stage=tool_selection&group_id=${encodeURIComponent(requestedGroupId)}`;
  const [defaultSource, experimentList, historyExperimentList, groupListPayload, postResourcePayload, settingsPayload, queryResourcePayload, mcpResourcePayload] = await Promise.all([
    isNew
      ? apiGet("/api/platform/config/default")
      : apiGet(`/api/platform/experiments/${encodeURIComponent(experimentId)}`),
    apiGet(`/api/platform/experiments${groupQuery}`),
    isNew && requestedGroupId
      ? apiGet(historyExperimentQuery)
      : Promise.resolve({experiments: []}),
    apiGet("/api/platform/experiment-groups?archived=all"),
    apiGet("/api/platform/resources?kind=posts&include_content=true"),
    apiGet("/api/platform/settings"),
    apiGet("/api/platform/resources?kind=queries&include_content=true"),
    apiGet("/api/platform/resources?kind=mcp&include_content=true"),
  ]);
  platformSettingsCache = settingsPayload.settings;
  const postResources = (postResourcePayload.resources || []).map((resource) => ({
    ...resource, revisions: (resource.revisions || []).filter((revision) => ["moltnet_thread", "moltnet_forum"].includes(revision.environment_type)),
  })).filter((resource) => resource.revisions.length);
  const historyExperiments = historyExperimentImportCandidates(
    historyExperimentList.experiments || [], historyImportStage, requestedGroupId,
  );
  const importedExperiment = await loadHistoryExperimentImport(historyExperiments, importExperimentId);
  const importedParameters = importedExperiment
    ? prepareHistoryExperimentImport(importedExperiment, experimentList.experiments || [])
    : null;
  const source = importedExperiment || defaultSource;
  const groups = normalizeExperimentGroups(groupListPayload);
  let selectedGroupId = isNew ? requestedGroupId : experimentGroupId(source);
  let selectedGroup = groups.find((group) => experimentGroupId(group) === selectedGroupId) || null;
  if (isNew && selectedGroup && (
    selectedGroup.archived || selectedGroup.legacy || selectedGroup.can_create === false
  )) {
    selectedGroup = null;
    selectedGroupId = "";
  }
  if (selectedGroupId && selectedGroup) {
    const groupPayload = await apiGet(`/api/platform/experiment-groups/${encodeURIComponent(selectedGroupId)}`);
    const detailedGroup = normalizeExperimentGroup(groupPayload);
    if (detailedGroup && experimentGroupId(detailedGroup)) {
      selectedGroup = {
        ...detailedGroup,
        stage_one_experiments: groupPayload.stage_one_experiments || detailedGroup.stage_one_experiments,
        stage_two_experiments: groupPayload.stage_two_experiments || detailedGroup.stage_two_experiments,
      };
    }
  }
  if (!isNew && source.archived) {
    main.innerHTML = `<div class="page">
      ${pageHeader(t("experiment.archived"), t("experiment.archivedDescription"), `<a class="button secondary" href="/experiments/${encodeURIComponent(experimentId)}">${t("common.back")}</a>`)}
    </div>`;
    return;
  }
  if (!isNew && (source.configuration_locked || !canLaunchExperiment(source))) {
    main.innerHTML = `<div class="page">
      ${pageHeader(t("experiment.configurationLocked"), t("experiment.lockedDescription"), `<a class="button secondary" href="/experiments/${encodeURIComponent(experimentId)}">${t("common.back")}</a>`)}
    </div>`;
    return;
  }
  const config = clone(importedParameters?.config || source.config);
  if (isNew && requestedStage === "tool_selection" && !importedParameters) {
    config.validation = {
      ...(config.validation || {}),
      backend_connection: defaultValidationConnectionForModel(selectedExperimentModel(config)),
      api_key_position: platformSettingsCache.validation.api_key_position,
    };
  }
  if (isNew && !importedParameters && requestedStage === "tool_selection"
      && selectedGroup?.target_mapping) {
    config.validation = {...(config.validation || {}), target_mode: "mapping"};
  }
  if (isNew && requestedStage === "tool_selection" && editorQuery.get("memory_source_mode") === "specified") {
    config.validation = {...(config.validation || {}), memory_source_mode: "specified", injection_enabled: false};
  }
  if (isNew && requestedStage === "tool_selection" && editorQuery.get("source_experiment_id")) {
    config.validation = {
      ...(config.validation || {}),
      source_experiment_id: editorQuery.get("source_experiment_id"),
      source_run_id: editorQuery.get("source_run_id") || "",
      source_agent_id: editorQuery.get("source_agent_id") || "",
      source_round: Number(editorQuery.get("source_round")) || undefined,
    };
  }
  const initialEditorStage = ["interaction", "tool_selection"].includes(requestedStage)
    ? requestedStage
    : isToolSelection(source) ? "tool_selection" : "interaction";
  let suggestedId = null;
  if (isNew && selectedGroupId) {
    const stage = initialEditorStage;
    const sourceExperimentId = get(config, "validation.source_experiment_id", "");
    if (stage !== "tool_selection" || sourceExperimentId || config.validation?.memory_source_mode === "specified") {
      const params = new URLSearchParams({stage});
      if (config.validation?.memory_source_mode === "specified") params.set("memory_source_mode", "specified");
      if (sourceExperimentId) params.set("source_experiment_id", sourceExperimentId);
      params.set("group_id", selectedGroupId);
      suggestedId = await apiGet(`/api/platform/experiment-id/suggest?${params}`);
    }
  }
  const name = isNew ? "" : source.name;
  const description = isNew ? "" : source.description;
  const slug = isNew ? suggestedId?.experiment_id || "" : source.experiment_id;
  const sections = [
    {
      id: "identity",
      title: t("editor.section.setup"),
      body: `<div class="form-grid">
        ${experimentGroupSelectField(groups, selectedGroupId, {isNew})}
        ${isNew ? historyExperimentImportField(
          historyExperiments, importedExperiment, importedParameters?.sourceReset,
        ) : ""}
        ${selectField("experiment.stage", t("validation.stage"), initialEditorStage, [
          ["interaction", t("validation.phaseOne")], ["tool_selection", t("validation.phaseTwo")]
        ])}
        <p class="validation-notice wide" id="validationStageHelp">${t(config.validation?.memory_source_mode === "specified" && initialEditorStage === "tool_selection" ? "validation.specifiedStageHelp" : "validation.stageHelp")}</p>
      </div>`,
    },
    {
      id: "details",
      title: t("editor.section.identity"),
      body: `<div class="form-grid">
        ${textField("experiment_id", t("editor.experimentId"), slug, "exp/<id>/config.yaml", isNew ? 'required pattern="[a-z0-9_-]{1,64}"' : "readonly")}
        ${textField("platform.name", t("editor.experimentName"), name, t("editor.experimentNameHelp"))}
        ${textareaField("platform.description", t("editor.description"), description, "wide")}
      </div>`,
    },
    {
      id: "environment",
      title: t("editor.section.environment"),
      body: `<div class="form-grid">
        ${selectField("environment.type", t("editor.environmentType"), get(config, "environment.type", "moltnet_forum"), [
          ["moltnet_forum", t("editor.forumEnvironment")], ["moltnet_thread", t("editor.threadEnvironment")]
        ])}
        ${speakerVisibilityField(config)}
        <div class="form-field wide post-file-import">
          <label for="postResourceRevision">${t("resource.postsResource")}</label>
          <select id="postResourceRevision" required aria-describedby="postFileHelp postFileStatus postFileSummary">
            <option value="">${t("resource.chooseRevision")}</option>
            ${resourceRevisionOptions(postResources, config.environment?.file?.resource_revision_id || "")}
          </select>
          <small id="postFileHelp">${t("resource.newFileHelp")} <a href="/resources">${t("resource.openResources")}</a></small>
          <p id="postFileStatus" class="post-file-status" role="status" aria-live="polite"></p>
          <p id="postFileSummary" class="post-file-status" aria-live="polite"></p>
        </div>
      </div>`,
    },
    {
      id: "agents",
      title: t("editor.section.agents"),
      body: `<div class="form-grid">
        <div class="form-field wide">
          <label for="simulation.read_only_agent_id">${t("editor.readOnlyAgent")}</label>
          <input id="simulation.read_only_agent_id" name="simulation.read_only_agent_id" value="${escapeHtml(get(config, "simulation.read_only_agent_id", ""))}" list="readOnlyAgentOptions" required aria-describedby="readOnlyAgentHelp" />
          <datalist id="readOnlyAgentOptions"></datalist>
          <small id="readOnlyAgentHelp">${t("editor.readOnlyAgentHelp")}</small>
        </div>
        <p class="validation-notice wide">${t("editor.readOnlyHelp")}</p>
        ${numberField("agents.history_limit", t("editor.historyLimit"), get(config, "agents.history_limit", 100), 1)}
        ${checkboxField("agents.require_identity", t("editor.requireIdentity"), get(config, "agents.require_identity", true))}
      </div>`,
    },
    {
      id: "memory",
      title: t("editor.section.memory"),
      body: memoryEditorFields(config),
    },
    {
      id: "backend",
      title: t("editor.section.backend"),
      advanced: true,
      body: `<div class="form-grid">
        ${textField("backend.api_base_env", t("editor.apiBaseEnv"), get(config, "backend.api_base_env", "AGENT_COMMUNITY_API_BASE"), "", "required")}
        ${textField("backend.model_env", t("editor.modelEnv"), get(config, "backend.model_env", "AGENT_COMMUNITY_MODEL"), "", "required")}
        ${textField("backend.api_key_env", t("editor.apiKeyEnv"), get(config, "backend.api_key_env", "AGENT_COMMUNITY_API_KEY"), "", "required")}
        ${textField("backend.mem0_api_key_env", t("editor.mem0ApiKeyEnv"), get(config, "backend.mem0_api_key_env", "MEM0_API_KEY"), "", "required")}
        ${numberField("backend.concurrency", t("editor.concurrency"), get(config, "backend.concurrency", 1), 1)}
        ${numberField("backend.timeout_seconds", t("editor.timeoutSeconds"), get(config, "backend.timeout_seconds", 90), 0.001, "any")}
        ${numberField("backend.temperature", t("editor.temperature"), get(config, "backend.temperature", 0.2), "", "any")}
        ${numberField("backend.max_tokens", t("editor.maxTokens"), get(config, "backend.max_tokens", 8000), 1)}
        ${numberField("backend.min_request_interval_seconds", t("editor.minRequestInterval"), get(config, "backend.min_request_interval_seconds", 0), 0, "any")}
        ${numberField("backend.max_transient_retries", t("editor.transientRetries"), get(config, "backend.max_transient_retries", 8), 0)}
        <div data-config-when="transient-retries">
          ${numberField("backend.retry_backoff_seconds", t("editor.retryBackoff"), get(config, "backend.retry_backoff_seconds", 2), 0.001, "any")}
        </div>
        <div data-config-when="transient-retries">
          ${numberField("backend.max_retry_wait_seconds", t("editor.maxRetryWait"), get(config, "backend.max_retry_wait_seconds", 60), 0.001, "any")}
        </div>
      </div>`,
    },
    {
      id: "artifacts",
      title: t("editor.section.artifacts"),
      advanced: true,
      body: `<div class="form-grid">
        ${textField(
          "output.root_dir",
          t("editor.outputRoot"),
          isNew
            ? configuredOutputRoot(slug)
            : get(config, "output.root_dir", `output/experiments/${slug || "experiment"}`),
          "", "required", "wide",
        )}
        ${checkboxField("output.save_thread_after_each_round", t("editor.saveThreadSnapshots"), get(config, "output.save_thread_after_each_round", true))}
        ${checkboxField("output.save_memory_after_each_round", t("editor.saveMemorySnapshots"), get(config, "output.save_memory_after_each_round", true))}
      </div>`,
    },
  ];
  sections.forEach((section) => {
    if (!["identity", "details"].includes(section.id)) section.stage = "interaction";
  });
  const [validationMemorySection, validationFilesSection] = validationEditorSections(config);
  sections.splice(1, 0, validationMemorySection);
  sections.splice(3, 0, validationFilesSection);
  main.innerHTML = `
    <div class="page">
      ${pageHeader(isNew ? t("experiment.new") : t("experiment.edit"), "", "")}
      <form id="experimentForm" class="config-workspace">
        ${configNavigation(sections)}
        <div class="config-form">
          ${sections.map(configSection).join("")}
        </div>
        <aside class="sticky-summary">
          <div class="summary-panel">
            <h2>${t("editor.summary")}</h2>
            <dl class="summary-list" id="configSummary"></dl>
            <div class="config-readiness" id="configReadiness" role="status"></div>
            <button type="button" class="secondary" id="validateConfig">${t("editor.validate")}</button>
            <div class="summary-actions">
              <a class="button secondary" href="${isNew && selectedGroupId ? `/experiment-groups/${encodeURIComponent(selectedGroupId)}` : isNew ? "/experiments" : `/experiments/${encodeURIComponent(experimentId)}`}">${t("common.cancel")}</a>
              <button type="submit">${t("editor.saveExport")}</button>
            </div>
          </div>
        </aside>
      </form>
    </div>`;
  const form = document.querySelector("#experimentForm");
  form.dataset.existingExperiment = String(!isNew);
  if (isNew && !importedParameters) {
    form.elements["memory.strategy"].addEventListener("change", () => {
      const strategy = fieldValue(form, "memory.strategy");
      const profile = platformSettingsCache?.memory_profiles?.[strategy];
      if (!profile) return;
      const optionFields = {
        query_limit: "memory.langmem_query_limit", max_steps: "memory.langmem_max_steps",
        embedding_model: "memory.langmem_embedding_model",
        embedding_dimensions: "memory.langmem_embedding_dimensions",
        enable_deletes: "memory.langmem_enable_deletes",
        custom_instructions: strategy === "langmem_official" ? "memory.langmem_custom_instructions" : "memory.platform_custom_instructions",
        evo_threshold: "memory.amem_evo_threshold",
        response_tolerance_enabled: "memory.amem_response_tolerance_enabled",
        response_validation_max_attempts: "memory.amem_response_validation_max_attempts",
        target_bank: "memory.hindsight_target_bank",
        request_timeout_seconds: strategy === "mem0_platform_v3" ? "memory.mem0_request_timeout" : "memory.cloud_request_timeout",
        event_timeout_seconds: "memory.mem0_event_timeout",
        event_poll_interval_seconds: "memory.mem0_poll_interval",
        max_transient_retries: "memory.mem0_transient_retries",
        retry_backoff_seconds: "memory.mem0_retry_backoff",
        max_retry_wait_seconds: "memory.mem0_max_retry_wait",
        processing_timeout_seconds: "memory.cloud_processing_timeout",
        poll_interval_seconds: "memory.cloud_poll_interval", max_read_retries: "memory.cloud_read_retries",
      };
      Object.entries(profile.options || {}).forEach(([key, value]) => {
        const input = form.elements[optionFields[key]];
        if (!input) return;
        if (input.type === "checkbox") input.checked = Boolean(value);
        else input.value = String(value);
      });
      if (profile.model && form.elements["memory.model"]) form.elements["memory.model"].value = profile.model;
    });
  }
  form.postResources = postResources;
  form.validationQueryResources = queryResourcePayload.resources || [];
  form.validationMcpResources = mcpResourcePayload.resources || [];
  form.importedPostFile = findRegisteredResourceRevision(
    postResources,
    config.environment?.file?.resource_revision_id,
  );
  form.selectedPostIds = Array.isArray(config.environment?.file?.selected_post_ids)
    ? [...config.environment.file.selected_post_ids] : null;
  form.defaultValidationResources = selectedGroup && !selectedGroup.legacy
    ? experimentGroupFixedResources(selectedGroup)
    : null;
  form.querySelector("#experiment_group_id")?.addEventListener("change", (event) => {
    if (!isNew) return;
    const params = new URLSearchParams(location.search);
    if (event.target.value) params.set("group_id", event.target.value);
    else params.delete("group_id");
    params.set("stage", fieldValue(form, "experiment.stage") || initialEditorStage);
    location.assign(`/experiments/new?${params}`);
  });
  form.querySelector("#historyParameterImport")?.addEventListener("change", (event) => {
    const params = new URLSearchParams(location.search);
    if (event.target.value) params.set("import_from", event.target.value);
    else params.delete("import_from");
    params.set("stage", fieldValue(form, "experiment.stage") || initialEditorStage);
    const query = params.toString();
    location.assign(`/experiments/new${query ? `?${query}` : ""}`);
  });
  form.querySelectorAll(".config-nav a").forEach((link) => {
    link.addEventListener("click", () => {
      const target = document.querySelector(link.getAttribute("href"));
      if (target?.tagName === "DETAILS") target.open = true;
    });
  });
  let refreshGeneratedExperimentId = () => {};
  if (isNew) {
    const idInput = form.elements.experiment_id;
    const output = form.elements["output.root_dir"];
    let generatedOutput = output.value;
    let suggestionRequest = 0;
    const applyGeneratedExperimentId = (value) => {
      const tracksGeneratedOutput = output.value === generatedOutput;
      idInput.value = value;
      generatedOutput = configuredOutputRoot(value);
      if (tracksGeneratedOutput) output.value = generatedOutput;
    };
    form.elements.experiment_id.addEventListener("input", () => {
      if (output.value === generatedOutput) {
        const slugValue = form.elements.experiment_id.value.trim() || "experiment";
        generatedOutput = configuredOutputRoot(slugValue);
        output.value = generatedOutput;
      }
    });
    refreshGeneratedExperimentId = async () => {
      const stage = fieldValue(form, "experiment.stage") || "interaction";
      const sourceExperimentId = fieldValue(form, "validation.source_experiment_id");
      idInput.readOnly = false;
      const request = ++suggestionRequest;
      const groupId = fieldValue(form, "experiment_group_id");
      if (!groupId || (stage === "tool_selection" && !sourceExperimentId && !usesSpecifiedMemory(form))) {
        applyGeneratedExperimentId("");
        return;
      }
      const params = new URLSearchParams({stage});
      if (usesSpecifiedMemory(form)) params.set("memory_source_mode", "specified");
      if (sourceExperimentId) params.set("source_experiment_id", sourceExperimentId);
      params.set("group_id", groupId);
      try {
        const result = await apiGet(`/api/platform/experiment-id/suggest?${params}`);
        if (request === suggestionRequest && form.isConnected) {
          applyGeneratedExperimentId(result.experiment_id || "");
          syncConfigEditor(form);
        }
      } catch (error) {
        if (request === suggestionRequest) showToast(error.message);
      }
    };
    form.elements["experiment.stage"].addEventListener("change", refreshGeneratedExperimentId);
  }
  const update = () => syncConfigEditor(form);
  form.validationConnectionFollowsModel = isNew && !importedParameters;
  const groupFirstStageExperiments = (
    selectedGroup?.stage_one_experiments
    || (experimentList.experiments || []).filter((item) => (
      experimentGroupId(item) === selectedGroupId
      && experimentListStage(item) === "interaction"
    ))
  ).filter((item) => !item.archived && isSupportedReadingExperiment(item));
  bindValidationEditor(
    form,
    config,
    update,
    selectedGroupId ? groupFirstStageExperiments : [],
    refreshGeneratedExperimentId,
    form.defaultValidationResources,
  );
  bindPostFileImport(form, update);
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  update();
  if (isNew && importExperimentId && !importedExperiment) {
    showToast(t("editor.historyImportMissing"));
  } else if (isNew && requestedGroupId && !selectedGroup) {
    showToast(t("group.missing"));
  }
  document.querySelector("#validateConfig").addEventListener("click", async (event) => {
    if (isConfigEditorBusy(form)) return;
    if (!form.reportValidity()) return;
    form.dataset.validating = "true";
    syncConfigEditor(form);
    try {
      const payload = {
        group_id: fieldValue(form, "experiment_group_id"),
        config: buildConfig(form, config),
      };
      if (!isNew) payload.experiment_id = experimentId;
      await apiPost("/api/platform/config/validate", payload);
      showToast(t("editor.valid"));
    } catch (error) {
      showToast(error.message);
    } finally {
      delete form.dataset.validating;
      syncConfigEditor(form);
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (isConfigEditorBusy(form) || !form.reportValidity()) return;
    form.dataset.saving = "true";
    syncConfigEditor(form);
    try {
      const payload = {
        experiment_id: form.elements.experiment_id.value.trim(),
        group_id: fieldValue(form, "experiment_group_id"),
        name: experimentNameForSave(form),
        description: form.elements["platform.description"].value.trim(),
        config: buildConfig(form, config),
      };
      const endpoint = isNew
        ? "/api/platform/experiments"
        : `/api/platform/experiments/${encodeURIComponent(experimentId)}`;
      const saved = isNew ? await apiPost(endpoint, payload) : await apiPut(endpoint, payload);
      location.href = `/experiments/${encodeURIComponent(saved.experiment_id)}`;
    } catch (error) {
      showToast(error.message);
      delete form.dataset.saving;
      syncConfigEditor(form);
    }
  });
}

function bindPostFileImport(form, update) {
  const input = form.querySelector("#postResourceRevision");
  const status = form.querySelector("#postFileStatus");
  input.addEventListener("change", () => {
    const environmentType = fieldValue(form, "environment.type");
    status.classList.remove("has-error");
    const revision = findRegisteredResourceRevision(form.postResources, input.value);
    form.selectedPostIds = null;
    if (revision && revision.environment_type !== environmentType) {
      form.importedPostFile = null;
      status.classList.add("has-error");
      status.textContent = t("resource.environmentMismatch", {
        registered: revision.environment_type,
        selected: environmentType,
      });
    } else {
      form.importedPostFile = revision;
      status.textContent = revision
        ? t("resource.selected")
        : "";
    }
    update();
  });
}

function isToolSelection(value) {
  return value?.runner_type === "tool_selection" || value?.environment?.type === "tool_selection"
    || value?.config?.environment?.type === "tool_selection" || value?.manifest?.environment_type === "tool_selection";
}

function prepareHistoryExperimentImport(experiment, activeExperiments) {
  const config = clone(experiment.config || {});
  let sourceReset = false;
  if (isToolSelection(experiment) && config.validation?.memory_source_mode !== "specified") {
    const validation = config.validation && typeof config.validation === "object"
      ? config.validation
      : {};
    const sourceExperimentId = String(
      validation.source_experiment_id || validation.memory_source?.experiment_id || "",
    ).trim();
    const sourceIsActive = activeExperiments.some((item) => (
      item.experiment_id === sourceExperimentId
      && experimentListStage(item) === "interaction"
      && !item.archived
    ));
    sourceReset = Boolean(
      sourceExperimentId || validation.source_run_id || validation.source_agent_id
      || validation.memory_source,
    );
    if (!sourceIsActive) delete validation.source_experiment_id;
    ["source_run_id", "source_agent_id", "source_round", "memory_source"]
      .forEach((key) => delete validation[key]);
    config.validation = validation;
  }
  return {config, sourceReset};
}

function historyExperimentImportCandidates(experiments, stage, groupId) {
  return experiments.filter((experiment) => (
    experimentListStage(experiment) === stage
    && canLaunchExperiment(experiment)
    && (stage === "interaction" || experimentGroupId(experiment) === groupId)
  ));
}

async function loadHistoryExperimentImport(experiments, experimentId) {
  const candidate = experiments.find((item) => item.experiment_id === experimentId);
  if (!candidate) return null;
  const experiment = candidate.config
    ? candidate
    : await apiGet(`/api/platform/experiments/${encodeURIComponent(experimentId)}`);
  return experiment?.config && canLaunchExperiment(experiment) ? experiment : null;
}

function experimentGroupSelectField(groups, selectedGroupId, {isNew}) {
  const options = groups.map((group) => {
    const groupId = experimentGroupId(group);
    const unavailable = group.archived || group.legacy || group.can_create === false;
    if (isNew && unavailable && groupId !== selectedGroupId) return "";
    const suffix = group.legacy
      ? ` · ${t("group.legacy")}`
      : group.archived ? ` · ${t("group.archived")}` : "";
    return `<option value="${escapeHtml(groupId)}" ${groupId === selectedGroupId ? "selected" : ""} ${isNew && unavailable ? "disabled" : ""}>${escapeHtml(group.name || groupId)}${escapeHtml(suffix)}</option>`;
  }).join("");
  const missing = selectedGroupId && !groups.some((group) => experimentGroupId(group) === selectedGroupId)
    ? `<option value="${escapeHtml(selectedGroupId)}" selected>${escapeHtml(selectedGroupId)}</option>`
    : "";
  return `<div class="form-field wide">
    <label for="experiment_group_id">${t("group.select")}</label>
    <select id="experiment_group_id" name="experiment_group_id" required aria-describedby="experimentGroupHelp" ${isNew ? "" : "disabled"}>
      <option value="">${t("group.selectPlaceholder")}</option>
      ${missing}${options}
    </select>
    <small id="experimentGroupHelp">${t("group.required")}</small>
  </div>`;
}

function historyExperimentImportField(experiments, selectedExperiment, sourceReset) {
  const selectedId = selectedExperiment?.experiment_id || "";
  const choices = experiments.map((experiment) => {
    const stage = isToolSelection(experiment)
      ? t("validation.phaseTwoShort")
      : t("validation.phaseOneShort");
    const groupId = experimentGroupId(experiment);
    const groupName = experimentGroupName(experiment);
    const group = groupName && groupId && groupName !== groupId
      ? `${groupName} (${groupId})`
      : groupName || groupId || "-";
    const label = t("editor.historyImportOption", {
      name: experiment.name || experiment.experiment_id,
      id: experiment.experiment_id,
      stage,
      group,
    }) + (experiment.archived ? ` · ${t("experiment.archived")}` : "");
    return `<option value="${escapeHtml(experiment.experiment_id)}" ${experiment.experiment_id === selectedId ? "selected" : ""}>${escapeHtml(label)}</option>`;
  }).join("");
  const applied = selectedExperiment
    ? t("editor.historyImportApplied", {
        name: selectedExperiment.name || selectedExperiment.experiment_id,
        id: selectedExperiment.experiment_id,
      }) + (sourceReset ? ` ${t("editor.historyImportSourceReset")}` : "")
    : "";
  return `<div class="form-field wide">
    <label for="historyParameterImport">${t("editor.historyImport")}</label>
    <select id="historyParameterImport" aria-describedby="historyParameterImportHelp" ${experiments.length ? "" : "disabled"}>
      <option value="">${t(experiments.length ? "editor.historyImportDefault" : "editor.historyImportEmpty")}</option>
      ${choices}
    </select>
    <small id="historyParameterImportHelp">${t("editor.historyImportHelp")}</small>
    ${applied ? `<p class="validation-notice" role="status">${escapeHtml(applied)}</p>` : ""}
  </div>`;
}

function experimentListStage(value) {
  if (value?.runner_type === "tool_selection") return "tool_selection";
  // Historical session records remain browsable alongside first-stage runs.
  if (["moltnet_thread", "moltnet_forum", "interactive_thread"].includes(value?.runner_type)) return "interaction";
  return null;
}

function isConfigEditorBusy(form) {
  return ["saving", "validating"]
    .some((key) => form.dataset[key] === "true");
}

function validationEditorSections(config) {
  const specified = config.validation?.memory_source_mode === "specified";
  const targetMode = config.validation
    ? config.validation.target_mode || "custom"
    : "none";
  return [
    {
      id: "validation-memory", stage: "tool_selection", title: t(specified ? "validation.specifiedMemoryTitle" : "validation.memorySource"),
      body: `<div class="form-grid">
        <div class="wide">${selectField("validation.memory_source_mode", t("validation.memorySourceMode"),
          get(config, "validation.memory_source_mode", "run"), [
            ["run", t("validation.memorySourceRun")], ["specified", t("validation.memorySourceSpecified")],
          ])}</div>
        <section class="wide specified-memory-editor" data-config-when="validation-specified-memory" hidden>
          <p class="validation-notice">${t("validation.specifiedMemoryHelp")}</p>
          ${textField("validation.specified_agent_id", t("validation.agentId"), get(config, "validation.specified_agent_id", "ROWAN"), "", 'required maxlength="120"')}
          <div id="specifiedMemoryFields"></div>
          <p id="specifiedMemoryStatus" role="status" aria-live="polite"></p>
        </section>
        <div class="wide" data-config-when="validation-run-memory"><div class="form-grid">
        <div class="form-field wide">
          <label for="validation.source_experiment_id">${t("validation.sourceExperiment")}</label>
          <select id="validation.source_experiment_id" name="validation.source_experiment_id" required aria-describedby="sourceExperimentHelp">
            <option value="">${t("validation.chooseSourceExperiment")}</option>
          </select>
          <small id="sourceExperimentHelp">${t("validation.sourceExperimentHelp")}</small>
        </div>
        <div class="form-field wide">
          <label for="validation.source_key">${t("validation.sourceRun")}</label>
          <select id="validation.source_key" name="validation.source_key" required aria-describedby="memorySourceHelp memorySourceStatus">
            <option value="">${t("validation.chooseSource")}</option>
          </select>
          <small id="memorySourceHelp">${t("validation.sourceHelp")}</small>
          <p id="memorySourceStatus" class="post-file-status" role="status" aria-live="polite"></p>
          <div class="pager validation-source-pager">
            <span id="memorySourcePage"></span>
            <button type="button" class="secondary compact" id="memorySourceRetry" hidden>${t("common.retry")}</button>
            <button type="button" class="secondary compact" id="memorySourcePrevious">${t("common.previous")}</button>
            <button type="button" class="secondary compact" id="memorySourceNext">${t("common.next")}</button>
          </div>
        </div>
        <div class="form-field wide">
          <label for="validation.source_agent_id">${t("validation.agent")}</label>
          <select id="validation.source_agent_id" name="validation.source_agent_id" required>
            <option value="">${t("validation.chooseAgent")}</option>
          </select>
          <small>${t("validation.singleAgentHelp")}</small>
        </div>
        <div class="validation-source-summary wide" id="validationSourceSummary"></div>
        ${memoryAccountPoolFields(config)}
        </div></div>
        ${experimentModelField("validation.model", selectedExperimentModel(config), {
          label: t("validation.model"), help: t("validation.modelHelp"),
        })}
        <div class="wide">${checkboxField("validation.injection_enabled", t("validation.injectionEnabled"),
          get(config, "validation.injection_enabled", get(config, "validation.memory_source_mode", "run") !== "specified"))}
          <small>${t("validation.injectionEnabledHelp")}</small></div>
        <div class="wide" data-config-when="validation-injection"><div class="form-grid">
        ${selectField("validation.api_key_position", t("validation.apiKeyPosition"),
          get(config, "validation.api_key_position", "middle"), [
            ["first", t("validation.apiKeyPositionFirst")],
            ["middle", t("validation.apiKeyPositionMiddle")],
            ["last", t("validation.apiKeyPositionLast")],
            ["message", t("validation.apiKeyPositionMessage")],
            ["query_suffix", t("validation.apiKeyPositionQuerySuffix")],
          ])}
        <small class="wide">${t("validation.apiKeyPositionHelp")}</small>
        <div class="wide">${checkboxField("validation.custom_payload_enabled", t("validation.customPayloadEnabled"),
          get(config, "validation.custom_payload_enabled", false))}</div>
        <small class="wide">${t("validation.defaultLeakExamples")}</small>
        <div class="form-field wide" data-config-when="validation-custom-payload" hidden>
          <label for="validation.custom_payload">${t("validation.customPayload")}</label>
          <textarea id="validation.custom_payload" name="validation.custom_payload" maxlength="10000" required disabled>${escapeHtml(get(config, "validation.custom_payload", ""))}</textarea>
          <small>${t("validation.customPayloadHelp")}</small>
        </div>
        <div class="form-field wide" data-config-when="validation-custom-payload" hidden>
          <label for="validation.expected_leak_value">${t("validation.expectedLeakValue")}</label>
          <input id="validation.expected_leak_value" name="validation.expected_leak_value" type="text" maxlength="1000" value="${escapeHtml(get(config, "validation.custom_payload_enabled", false) ? get(config, "validation.expected_leak_value", "") : "")}" placeholder="sk-DEMO_NOT_A_REAL_SECRET" required disabled />
        </div>
        <div class="form-field wide" data-config-when="validation-custom-payload" hidden>
          <label for="validation.leak_argument_path">${t("validation.leakArgumentPath")}</label>
          <input id="validation.leak_argument_path" name="validation.leak_argument_path" type="text" maxlength="300" value="${escapeHtml(get(config, "validation.custom_payload_enabled", false) ? get(config, "validation.leak_argument_path", "") : "")}" placeholder="arguments.api_key" pattern="arguments([.][A-Za-z_][A-Za-z0-9_-]*)+" required disabled />
          <small>${t("validation.leakArgumentPathHelp")}</small>
        </div>
        </div></div>
        ${selectField("validation.connection_mode", t("validation.connectionMode"),
          get(config, "validation.backend_connection.mode", "inherit"), [
            ["inherit", t(specified ? "validation.specifiedConnectionInherit" : "validation.connectionInherit")],
            ["environment", t("validation.connectionEnvironment")],
            ["custom", t("validation.connectionCustom")],
          ])}
        <p class="muted wide" id="validationConnectionHelp">${t(specified ? "validation.specifiedConnectionHelp" : "validation.connectionHelp")}</p>
        <div class="form-field wide" data-config-when="validation-connection-custom" hidden>
          <label for="validation.connection_api_base">${t("validation.connectionUrl")}</label>
          <input id="validation.connection_api_base" name="validation.connection_api_base" type="url" value="${escapeHtml(get(config, "validation.backend_connection.api_base", ""))}" placeholder="https://api.example.com/v1" required disabled />
        </div>
        <div data-config-when="validation-connection-environment" hidden>
          ${textField("validation.connection_api_base_env", t("editor.apiBaseEnv"), get(config, "validation.backend_connection.api_base_env", "AGENT_COMMUNITY_API_BASE"), "", "required")}
        </div>
        <div data-config-when="validation-connection-key" hidden>
          ${textField("validation.connection_api_key_env", t("editor.apiKeyEnv"), get(config, "validation.backend_connection.api_key_env", "AGENT_COMMUNITY_API_KEY"), "", "required")}
        </div>
        <div class="form-field" data-config-when="validation-retrieval-top-k" hidden>
          <label for="validation.retrieval_top_k">${t("editor.retrievalTopK")}</label>
          <input id="validation.retrieval_top_k" name="validation.retrieval_top_k" type="number" value="5" min="1" step="1" required disabled />
          <small>${t("validation.retrievalSettingsHelp")}</small>
        </div>
        <div class="form-field" data-config-when="validation-retrieval-window" hidden>
          <label for="validation.max_items">${t("editor.windowItems")}</label>
          <input id="validation.max_items" name="validation.max_items" type="number" value="20" min="1" step="1" required disabled />
          <small>${t("validation.retrievalSettingsHelp")}</small>
        </div>
        <div class="form-field" data-config-when="validation-retrieval-threshold" hidden>
          <label for="validation.search_threshold">${t("validation.searchThreshold")}</label>
          <input id="validation.search_threshold" name="validation.search_threshold" type="number" value="0.1" min="0" max="1" step="any" required disabled />
          <small>${t("validation.retrievalThresholdHelp")}</small>
        </div>
        <div class="form-field" data-config-when="validation-retrieval-rerank" hidden>
          ${checkboxField("validation.rerank", t("editor.mem0Rerank"), false)}
        </div>
        <div class="form-field" data-config-when="validation-retrieval-budget" hidden>
          <label for="validation.recall_budget">${t("cloud.recallBudget")}</label>
          <select id="validation.recall_budget" name="validation.recall_budget" disabled><option value="low">low</option><option value="mid" selected>mid</option><option value="high">high</option></select>
        </div>
        <div class="form-field" data-config-when="validation-retrieval-tokens" hidden>
          <label for="validation.recall_max_tokens">${t("cloud.recallTokens")}</label>
          <input id="validation.recall_max_tokens" name="validation.recall_max_tokens" type="number" value="4096" min="1" max="100000" required disabled />
        </div>
        <div class="form-field" data-config-when="validation-retrieval-chars" hidden>
          <label for="validation.context_max_chars">${t("cloud.contextMaxChars")}</label>
          <input id="validation.context_max_chars" name="validation.context_max_chars" type="number" value="12000" min="1" max="1000000" required disabled />
        </div>
        <section class="validation-memory-preview wide" id="validationMemoryPreview" hidden>
          <div class="section-heading">
            <div>
              <h3>${t("validation.memoryPreview")}</h3>
              <p id="memoryPreviewHelp">${t("validation.memoryPreviewHelp")}</p>
            </div>
          </div>
          <div class="form-field">
            <label for="memoryPreviewQuery">${t("validation.memoryPreviewQuery")}</label>
            <select id="memoryPreviewQuery" aria-describedby="memoryPreviewStatus"></select>
          </div>
          <div class="memory-preview-status-row">
            <p id="memoryPreviewStatus" class="post-file-status" role="status" aria-live="polite"></p>
            <button type="button" class="secondary compact" id="memoryPreviewRetry" hidden>${t("common.retry")}</button>
          </div>
          <pre id="memoryPreviewContent" tabindex="0" hidden></pre>
          <div id="memoryPreviewNotes" class="memory-result-list" hidden></div>
        </section>
        <p class="validation-notice wide" data-config-when="validation-run-memory">${t("validation.frozenMemoryHelp")}</p>
      </div>`,
    },
    {
      id: "validation-files", stage: "tool_selection", title: t("validation.files"),
      body: `<div class="form-grid">
        <div class="form-field wide">
          <label for="validationQueryResourceRevision">${t("group.queries")}</label>
          <select id="validationQueryResourceRevision" required aria-describedby="validationQueryResourceHelp"></select>
          <small id="validationQueryResourceHelp">${t("validation.queryResourceHelp")} <a href="/resources">${t("resource.openRegistry")}</a></small>
        </div>
        <div class="form-field wide">
          <label for="validationMcpResourceRevision">${t("group.mcp")}</label>
          <select id="validationMcpResourceRevision" required aria-describedby="validationMcpResourceHelp"></select>
          <small id="validationMcpResourceHelp">${t("validation.mcpResourceHelp")} <a href="/resources">${t("resource.openRegistry")}</a></small>
        </div>
        ${mcpParameterAdditionsPanel()}
        ${selectField("validation.resource_mode", t("validation.resourceMode"), "default", [
          ["default", t("resource.groupRegistered")],
        ])}
        <small class="wide" id="validationResourceModeHelp"></small>
        ${selectField("validation.target_mode", t("validation.targetMode"), targetMode, [
          ["none", t("validation.targetModeNone")],
          ["dataset", t("validation.targetModeDataset")],
          ["custom", t("validation.targetModeCustom")],
          ["mapping", t("mapping.mode")],
        ])}
        <small class="wide" id="validationTargetModeHelp">${t("validation.targetModeHelp")}</small>
        <section class="validation-target-editor wide" id="validationTargetEditor" hidden>
          <div class="section-heading">
            <div><h3>${t("validation.targets")}</h3><p id="validationTargetsHelp">${t("validation.targetsHelp")}</p></div>
            <strong id="validationTargetCount"></strong>
          </div>
          <p id="validationTargetStatus" class="validation-target-status" role="status" aria-live="polite"></p>
          <div id="validationSharedTargetControls" class="validation-shared-target" hidden>
            <div class="form-field">
              <label for="validationSharedTarget">${t("validation.sharedTarget")}</label>
              <select id="validationSharedTarget" aria-describedby="validationSharedTargetHelp"></select>
              <small id="validationSharedTargetHelp"></small>
            </div>
            <button type="button" class="secondary" data-apply-shared-target disabled>${t("validation.applyTargetToAll")}</button>
          </div>
          <div id="validationCategoryTargetControls" hidden></div>
          <div id="validationTargetList"></div>
          <div class="pager validation-target-pager">
            <span id="validationTargetPageLabel"></span>
            <button type="button" class="secondary compact" data-target-page="previous">${t("common.previous")}</button>
            <button type="button" class="secondary compact" data-target-page="next">${t("common.next")}</button>
          </div>
          <select id="validationTargetsValidity" class="validation-target-validity-proxy" required tabindex="-1" aria-label="${escapeHtml(t("validation.targetsValidity"))}">
            <option value=""></option><option value="valid"></option>
          </select>
        </section>
      </div>`,
    },
  ];
}

function memorySourceKey(source) {
  return source ? JSON.stringify([source.run_id, source.source_round]) : "";
}

function savedMemorySource(config) {
  const validation = config.validation || {};
  if (!validation.source_run_id || !validation.source_agent_id) return null;
  const saved = validation.memory_source || {};
  return {
    run_id: validation.source_run_id,
    experiment_id: validation.source_experiment_id || saved.experiment_id || "",
    experiment_name: saved.experiment_name || saved.experiment_id || "",
    source_round: validation.source_round ?? saved.round_index,
    snapshot_sha256: saved.snapshot_sha256,
    memory_strategy: saved.memory_strategy || config.memory?.strategy,
    memory_options: clone(saved.memory_options || {}),
    retrieval_settings: clone(config.memory?.options || saved.memory_options || {}),
    retrieval_top_k: saved.memory_options?.retrieval_top_k
      ?? config.memory?.options?.retrieval_top_k,
    agents: [{
      agent_id: validation.source_agent_id,
      display_name: saved.profile?.display_name || validation.source_agent_id,
      memory_revision: saved.snapshot?.revision,
      snapshot_sha256: saved.snapshot_sha256,
    }],
    usable: true,
    integrity_warnings: Array.isArray(saved.integrity_warnings)
      ? clone(saved.integrity_warnings)
      : [],
    saved: true,
  };
}

function renderSourceExperimentOptions(form) {
  const select = form.elements["validation.source_experiment_id"];
  const selectedId = select.value || form.savedSourceExperimentId || "";
  const choices = form.firstStageExperiments.filter((experiment) => (
    experimentListStage(experiment) === "interaction"
    && !experiment.archived
    && (Number(experiment.run_count || 0) > 0 || experiment.experiment_id === selectedId)
  ));
  select.innerHTML = `<option value="">${t("validation.chooseSourceExperiment")}</option>` + choices.map((experiment) => (
    `<option value="${escapeHtml(experiment.experiment_id)}" ${experiment.experiment_id === selectedId ? "selected" : ""}>${escapeHtml(t("validation.sourceExperimentOption", {
      name: experiment.name || experiment.experiment_id,
      id: experiment.experiment_id,
    }))}</option>`
  )).join("");
}

function stableJsonStringify(value) {
  const normalize = (item) => {
    if (Array.isArray(item)) return item.map(normalize);
    if (!item || typeof item !== "object") return item;
    return Object.keys(item).sort().reduce((result, key) => {
      result[key] = normalize(item[key]);
      return result;
    }, {});
  };
  return JSON.stringify(normalize(value));
}

function validationResourcesMatchGroupDefaults(files, defaults, targetMode) {
  if (!files?.queries || !files?.mcp || !defaults?.queries || !defaults?.mcp) return false;
  if (targetMode === "mapping") return files.queries.resource_revision_id === defaults.queries.resource_revision_id
    && files.mcp.resource_revision_id === defaults.mcp.resource_revision_id;
  const ignoreTargets = targetMode === "none" || targetMode === "custom";
  const normalizedFiles = ignoreTargets ? withoutValidationTargets(files) : files;
  const normalizedDefaults = ignoreTargets ? withoutValidationTargets(defaults) : defaults;
  return stableJsonStringify(normalizedFiles.queries) === stableJsonStringify(normalizedDefaults.queries)
    && stableJsonStringify(normalizedFiles.mcp) === stableJsonStringify(normalizedDefaults.mcp);
}

function setValidationResourceMode(form, requestedMode) {
  const mode = requestedMode === "default" && form.validationGroupDefaults
    ? "default"
    : "custom";
  const previousMode = form.validationResourceMode;
  const select = form.elements["validation.resource_mode"];
  if (mode === previousMode) {
    if (select) select.value = mode;
    return false;
  }
  if (mode === "default") {
    if (previousMode === "custom") form.validationCustomFiles = clone(form.validationFiles);
    form.validationFiles = clone(form.validationGroupDefaults);
    form.validationFilesFromGroup = {queries: true, mcp: true};
  } else {
    if (!form.validationCustomFiles) form.validationCustomFiles = clone(form.validationFiles);
    form.validationFiles = clone(form.validationCustomFiles);
    form.validationFilesFromGroup = {queries: false, mcp: false};
  }
  form.validationResourceMode = mode;
  form.validationTargetPage = 0;
  if (select) select.value = mode;
  return true;
}

function usesSpecifiedMemory(form) {
  return form.elements?.["validation.memory_source_mode"]?.value === "specified";
}

function syncValidationMemoryLabels(form, specified = usesSpecifiedMemory(form)) {
  const title = t(specified ? "validation.specifiedMemoryTitle" : "validation.memorySource");
  for (const selector of ['#config-validation-memory > header > h2', '.config-nav a[href="#config-validation-memory"]']) {
    const label = form.querySelector(selector);
    if (label) label.textContent = title;
  }
  const stageHelp = form.querySelector("#validationStageHelp");
  if (stageHelp) stageHelp.textContent = t(specified ? "validation.specifiedStageHelp" : "validation.stageHelp");
  const connectionHelp = form.querySelector("#validationConnectionHelp");
  if (connectionHelp) connectionHelp.textContent = t(specified ? "validation.specifiedConnectionHelp" : "validation.connectionHelp");
  const inherited = form.elements["validation.connection_mode"]?.querySelector?.('option[value="inherit"]');
  if (inherited) inherited.textContent = t(specified ? "validation.specifiedConnectionInherit" : "validation.connectionInherit");
}

function specifiedMemoryGroups(files) {
  const counts = new Map();
  for (const query of files?.queries?.dataset?.queries || []) {
    const group = typeof query.functional_group === "string" && query.functional_group.trim()
      ? query.functional_group : "__ungrouped__";
    counts.set(group, (counts.get(group) || 0) + 1);
  }
  return [...counts].map(([key, count]) => ({key, count}));
}

function collectSpecifiedMemory(form) {
  form.specifiedMemoryDraft ||= {};
  form.querySelectorAll("[data-specified-empty-group]").forEach((input) => {
    if (input.checked) form.specifiedMemoryDraft[input.dataset.specifiedEmptyGroup] = "";
  });
  form.querySelectorAll("[data-specified-memory-group]").forEach((input) => {
    // Preserve the complete input, including whitespace and line breaks.
    const group = input.dataset.specifiedMemoryGroup;
    const empty = [...form.querySelectorAll("[data-specified-empty-group]")]
      .find((checkbox) => checkbox.dataset.specifiedEmptyGroup === group)?.checked;
    if (empty) return;
    if (input.value.trim()) form.specifiedMemoryDraft[group] = input.value;
    else delete form.specifiedMemoryDraft[group];
  });
  return form.specifiedMemoryDraft;
}

function syncSpecifiedMemoryFields(form) {
  const container = form.querySelector("#specifiedMemoryFields");
  if (!container) return [];
  const draft = collectSpecifiedMemory(form);
  const groups = specifiedMemoryGroups(form.validationFiles);
  const groupsKey = JSON.stringify(groups);
  if (form.specifiedMemoryGroupsKey !== groupsKey) {
    form.specifiedMemoryGroupsKey = groupsKey;
    container.innerHTML = groups.map(({key, count}, index) => `<div class="form-field specified-memory-field">
      <label for="specifiedMemory-${index}">${escapeHtml(key === "__ungrouped__" ? t("validation.uncategorized") : key)} · ${t("validation.queryCount")}: ${count}</label>
      <textarea id="specifiedMemory-${index}" data-specified-memory-group="${escapeHtml(key)}" required rows="7" spellcheck="false">${escapeHtml(draft[key] || "")}</textarea>
      <div class="checkbox-field"><input type="checkbox" id="specifiedMemoryEmpty-${index}" data-specified-empty-group="${escapeHtml(key)}" ${draft[key] === "" ? "checked" : ""}><label for="specifiedMemoryEmpty-${index}">${t("validation.specifiedMemoryEmpty")}</label></div>
    </div>`).join("");
  }
  const enabled = usesSpecifiedMemory(form);
  const missing = groups.filter(({key}) => typeof draft[key] !== "string");
  container.querySelectorAll("textarea").forEach((input) => {
    const group = input.dataset.specifiedMemoryGroup;
    const empty = [...form.querySelectorAll("[data-specified-empty-group]")]
      .find((checkbox) => checkbox.dataset.specifiedEmptyGroup === group)?.checked;
    input.disabled = !enabled || empty;
    input.setCustomValidity(enabled && !empty && !input.value.trim() ? t("validation.specifiedMemoryRequired", {group}) : "");
  });
  const status = form.querySelector("#specifiedMemoryStatus");
  status.classList.toggle("has-error", Boolean(missing.length));
  status.textContent = groups.length
    ? t("validation.specifiedMemoryCoverage", {configured: groups.length - missing.length, total: groups.length})
    : t("validation.memoryPreviewNoQuery");
  return missing;
}

function bindValidationEditor(
  form,
  config,
  update,
  experiments,
  onSourceExperimentChange,
  defaultResources = null,
) {
  if (typeof bindMemoryAccountPool === "function") bindMemoryAccountPool(form, config);
  const groupDefaults = defaultResources
    ? clone(defaultResources)
    : null;
  const validation = config.validation && typeof config.validation === "object"
    ? config.validation
    : {};
  form.specifiedMemoryDraft = clone(validation.specified_memory || {});
  const memoryMode = form.elements["validation.memory_source_mode"];
  let previousMemoryMode = memoryMode?.value || "run";
  const changeMemoryMode = () => {
    if (memoryMode.value === previousMemoryMode) return;
    collectSpecifiedMemory(form);
    previousMemoryMode = memoryMode.value;
    // Entering the manual condition starts without the legacy key prompt.
    if (usesSpecifiedMemory(form)) form.elements["validation.injection_enabled"].checked = false;
    resetMemoryPreview(form);
    onSourceExperimentChange();
    update();
  };
  memoryMode?.addEventListener("input", changeMemoryMode);
  memoryMode?.addEventListener("change", changeMemoryMode);
  const hasQueries = Object.prototype.hasOwnProperty.call(validation, "queries");
  const hasMcp = Object.prototype.hasOwnProperty.call(validation, "mcp");
  const savedQuery = findRegisteredResourceRevision(
    form.validationQueryResources, validation.queries?.resource_revision_id,
  ) || (validation.queries?.dataset ? clone(validation.queries) : null);
  const savedMcp = findRegisteredResourceRevision(
    form.validationMcpResources, validation.mcp?.resource_revision_id,
  ) || (validation.mcp?.dataset ? clone(validation.mcp) : null);
  const configuredFiles = groupDefaults ? {
    ...clone(groupDefaults),
    queries: savedQuery || clone(groupDefaults.queries || null),
    mcp: savedMcp || clone(groupDefaults.mcp || null),
  } : {
    queries: savedQuery || clone(hasQueries ? validation.queries : null),
    mcp: savedMcp || clone(hasMcp ? validation.mcp : null),
  };
  if (groupDefaults) {
    groupDefaults.queries = clone(configuredFiles.queries);
    groupDefaults.mcp = clone(configuredFiles.mcp);
  }
  if (validation.target_mode === "custom"
      && Object.prototype.hasOwnProperty.call(validation, "target_overrides")) {
    applyValidationTargetOverrides(configuredFiles, validation.target_overrides);
  }
  const initialResourceMode = groupDefaults ? "default" : "custom";
  form.validationGroupDefaults = groupDefaults;
  const mappingOption = Array.from(form.elements["validation.target_mode"].options || []).find((option) => option.value === "mapping");
  if (mappingOption) mappingOption.disabled = !groupDefaults?.target_mapping;
  form.validationResourceMode = initialResourceMode;
  form.validationFiles = configuredFiles;
  form.mcpParameterAdditionsEditor = bindMcpParameterAdditionsEditor(
    form.querySelector("[data-mcp-parameter-editor]"), configuredFiles.mcp,
    validation.mcp_parameter_additions || [], update,
  );
  form.validationCustomFiles = initialResourceMode === "custom" ? clone(configuredFiles) : null;
  form.validationFilesFromGroup = {
    queries: Boolean(defaultResources?.queries)
      && configuredFiles.queries?.resource_revision_id === defaultResources.queries.resource_revision_id,
    mcp: Boolean(defaultResources?.mcp)
      && configuredFiles.mcp?.resource_revision_id === defaultResources.mcp.resource_revision_id,
  };
  const querySelect = form.querySelector("#validationQueryResourceRevision");
  if (querySelect) {
    querySelect.innerHTML = `<option value="">${t("resource.chooseRevision")}</option>`
      + resourceRevisionOptions(form.validationQueryResources || [], configuredFiles.queries?.resource_revision_id);
    querySelect.value = configuredFiles.queries?.resource_revision_id || "";
    querySelect.addEventListener("change", () => {
      const revision = findRegisteredResourceRevision(form.validationQueryResources, querySelect.value);
      form.validationFiles.queries = clone(revision);
      if (form.validationGroupDefaults) form.validationGroupDefaults.queries = clone(revision);
      if (form.validationCustomFiles) form.validationCustomFiles.queries = clone(revision);
      form.validationFilesFromGroup.queries = false;
      form.validationTargetPage = 0;
      resetMemoryPreview(form);
      update();
    });
  }
  const mcpSelect = form.querySelector("#validationMcpResourceRevision");
  if (mcpSelect) {
    mcpSelect.innerHTML = `<option value="">${t("resource.chooseRevision")}</option>`
      + resourceRevisionOptions(form.validationMcpResources || [], configuredFiles.mcp?.resource_revision_id);
    mcpSelect.value = configuredFiles.mcp?.resource_revision_id || "";
    mcpSelect.addEventListener("change", () => {
      const revision = findRegisteredResourceRevision(form.validationMcpResources, mcpSelect.value);
      form.validationFiles.mcp = clone(revision);
      form.mcpParameterAdditionsEditor?.setMcp(revision);
      if (form.validationGroupDefaults) {
        form.validationGroupDefaults.mcp = clone(revision);
        form.validationFiles.queries = clone(form.validationGroupDefaults.queries);
      }
      if (form.validationCustomFiles) {
        form.validationCustomFiles.mcp = clone(revision);
        form.validationCustomFiles.queries = clone(form.validationFiles.queries);
      }
      form.validationFilesFromGroup.mcp = false;
      form.validationTargetPage = 0;
      resetMemoryPreview(form);
      update();
    });
  }
  form.elements["validation.resource_mode"].value = initialResourceMode;
  if (form.validationConnectionFollowsModel) {
    const connectionFields = ["validation.connection_mode", "validation.connection_api_base",
      "validation.connection_api_base_env", "validation.connection_api_key_env"];
    connectionFields.forEach((name) => ["input", "change"].forEach((eventName) => {
      form.elements[name].addEventListener(eventName, () => {
        form.validationConnectionFollowsModel = false;
      });
    }));
    form.elements["validation.model"].addEventListener("change", () => {
      if (!form.validationConnectionFollowsModel) return;
      const connection = defaultValidationConnectionForModel(fieldValue(form, "validation.model"));
      form.elements["validation.connection_mode"].value = connection.mode;
      form.elements["validation.connection_api_base"].value = connection.api_base || "";
      form.elements["validation.connection_api_base_env"].value = connection.api_base_env || "AGENT_COMMUNITY_API_BASE";
      form.elements["validation.connection_api_key_env"].value = connection.api_key_env || "AGENT_COMMUNITY_API_KEY";
    });
  }
  const savedSource = savedMemorySource(config);
  form.validationImportedMemorySettings = !savedSource && config.validation && config.memory
    ? clone(config.memory) : null;
  form.savedSourceExperimentId = config.validation?.source_experiment_id
    || savedSource?.experiment_id || "";
  form.firstStageExperiments = experiments;
  form.memorySourceState = {
    items: [], selected: savedSource, saved: savedSource, offset: 0, limit: 100,
    hasMore: false, loading: false, loaded: false, error: "",
  };
  form.memoryPreviewState = {
    queryId: "", queryOptionsKey: "", requestKey: "", requestId: 0,
    loading: false, error: "", data: null,
  };
  form.validationTargetPage = 0;
  renderSourceExperimentOptions(form);
  renderMemorySourceOptions(form);
  updateValidationAgentOptions(form, config.validation?.source_agent_id || "");
  const sourceExperimentSelect = form.elements["validation.source_experiment_id"];
  sourceExperimentSelect.addEventListener("change", () => {
    const state = form.memorySourceState;
    state.items = [];
    state.selected = state.saved?.experiment_id === sourceExperimentSelect.value
      ? state.saved
      : null;
    state.offset = 0;
    state.hasMore = false;
    state.loaded = false;
    state.error = "";
    resetValidationRetrievalSettings(form, state.selected);
    resetMemoryPreview(form);
    renderMemorySourceOptions(form);
    updateValidationAgentOptions(form, config.validation?.source_agent_id || "");
    onSourceExperimentChange();
    update();
  });
  const sourceSelect = form.elements["validation.source_key"];
  sourceSelect.addEventListener("change", () => {
    const state = form.memorySourceState;
    state.selected = state.items.find((item) => (
      item.usable !== false && memorySourceKey(item) === sourceSelect.value
    ))
      || (memorySourceKey(state.selected) === sourceSelect.value ? state.selected : null);
    resetValidationRetrievalSettings(form, state.selected);
    resetMemoryPreview(form);
    updateValidationAgentOptions(form);
    update();
  });
  const previewQuerySelect = form.querySelector("#memoryPreviewQuery");
  const handlePreviewQuerySelection = (event) => {
    const state = form.memoryPreviewState;
    const changed = state.queryId !== event.currentTarget.value;
    state.queryId = event.currentTarget.value;
    event.stopPropagation();
    if (changed) update();
  };
  previewQuerySelect?.addEventListener("input", handlePreviewQuerySelection);
  previewQuerySelect?.addEventListener("change", handlePreviewQuerySelection);
  form.querySelector("#memoryPreviewRetry")?.addEventListener("click", () => {
    resetMemoryPreview(form);
    update();
  });
  form.querySelector("#memorySourcePrevious").addEventListener("click", () => {
    loadMemorySources(form, Math.max(0, form.memorySourceState.offset - form.memorySourceState.limit), update);
  });
  form.querySelector("#memorySourceNext").addEventListener("click", () => {
    loadMemorySources(form, form.memorySourceState.offset + form.memorySourceState.limit, update);
  });
  form.querySelector("#memorySourceRetry").addEventListener("click", () => {
    loadMemorySources(form, form.memorySourceState.offset, update);
  });
  const targetEditor = form.querySelector("#validationTargetEditor");
  // Native selects emit `input` before `change`. Persist during `input` and
  // refresh the surrounding state without rebuilding the active select; a
  // synchronous list render would detach it before the interaction finishes.
  targetEditor.addEventListener("input", (event) => {
    const categorySelect = event.target.closest("[data-validation-category-target]");
    if (categorySelect) {
      event.stopPropagation();
      categorySelect.closest(".validation-category-target")
        .querySelector("[data-apply-category-target]").disabled = !categorySelect.value
          || fieldValue(form, "validation.target_mode") !== "custom" || isConfigEditorBusy(form);
      return;
    }
    if (event.target.id === "validationSharedTarget") {
      event.stopPropagation();
      targetEditor.querySelector("[data-apply-shared-target]").disabled = !event.target.value
        || fieldValue(form, "validation.target_mode") !== "custom" || isConfigEditorBusy(form);
      return;
    }
    const select = event.target.closest("[data-validation-target-index]");
    if (!select || fieldValue(form, "validation.target_mode") !== "custom") return;
    event.stopPropagation();
    if (!setValidationQueryTarget(
      form.validationFiles, Number(select.dataset.validationTargetIndex), select.value,
    )) return;
    if (form.validationResourceMode === "custom") {
      form.validationCustomFiles = clone(form.validationFiles);
      form.validationFilesFromGroup.queries = false;
    }
    const item = select.closest(".validation-target-item");
    item?.classList.toggle("is-configured", Boolean(select.value));
    item?.classList.toggle("has-error", !select.value);
    renderValidationCategoryTargets(form);
    syncValidationEditor(form, {renderTargets: false});
    updateConfigReadiness(form);
  });
  targetEditor.addEventListener("change", (event) => {
    if (event.target.id === "validationSharedTarget"
        || event.target.closest("[data-validation-category-target]")
        || event.target.closest("[data-validation-target-index]")) event.stopPropagation();
  });
  targetEditor.addEventListener("click", (event) => {
    const categoryButton = event.target.closest("[data-apply-category-target]");
    if (categoryButton) {
      if (categoryButton.disabled || fieldValue(form, "validation.target_mode") !== "custom"
          || isConfigEditorBusy(form)) return;
      const select = categoryButton.closest(".validation-category-target")
        .querySelector("[data-validation-category-target]");
      if (!setValidationCategoryTarget(
        form.validationFiles, select.dataset.validationCategoryTarget, select.value,
      )) return;
      if (form.validationResourceMode === "custom") {
        form.validationCustomFiles = clone(form.validationFiles);
        form.validationFilesFromGroup.queries = false;
      }
      update();
      return;
    }
    const applyButton = event.target.closest("[data-apply-shared-target]");
    if (applyButton) {
      if (applyButton.disabled || fieldValue(form, "validation.target_mode") !== "custom"
          || isConfigEditorBusy(form)) return;
      const candidateKey = form.querySelector("#validationSharedTarget").value;
      if (!setValidationSharedTarget(form.validationFiles, candidateKey)) return;
      if (form.validationResourceMode === "custom") {
        form.validationCustomFiles = clone(form.validationFiles);
        form.validationFilesFromGroup.queries = false;
      }
      update();
      return;
    }
    const button = event.target.closest("[data-target-page]");
    if (!button || button.disabled) return;
    form.validationTargetPage += button.dataset.targetPage === "next" ? 1 : -1;
    renderValidationTargetList(form);
  });
  const resourceModeSelect = form.elements["validation.resource_mode"];
  const handleResourceModeSelection = (event) => {
    const select = event.currentTarget;
    if (select.disabled) {
      select.value = form.validationResourceMode;
      event.stopPropagation();
      return;
    }
    const changed = setValidationResourceMode(form, select.value);
    // A native select emits `input` before `change`. The input handler must
    // persist the mode before the form-level input listener synchronizes the
    // editor; otherwise that synchronization restores the old select value.
    // If input already handled it, suppress the redundant change refresh. A
    // change-only browser/keyboard path still bubbles once when it changes.
    if (event.type === "change" && !changed) event.stopPropagation();
  };
  resourceModeSelect.addEventListener("input", handleResourceModeSelection);
  resourceModeSelect.addEventListener("change", handleResourceModeSelection);
  form.elements["validation.target_mode"].addEventListener("change", (event) => {
    if (
      event.target.value === "dataset"
      && form.validationResourceMode === "default"
      && form.validationGroupDefaults
    ) {
      form.validationFiles = clone(form.validationGroupDefaults);
      form.validationFilesFromGroup = {queries: true, mcp: true};
      form.validationTargetPage = 0;
    }
    update();
  });
}

function renderMemorySourceOptions(form) {
  const state = form.memorySourceState;
  const selectedKey = memorySourceKey(state.selected);
  const choices = state.items.map((item) => (
    state.selected?.saved && memorySourceKey(item) === selectedKey
      ? state.selected
      : item
  ));
  if (state.selected && !choices.some((item) => memorySourceKey(item) === selectedKey)) choices.unshift(state.selected);
  const select = form.elements["validation.source_key"];
  select.innerHTML = `<option value="">${t("validation.chooseSource")}</option>` + choices.map((item) => {
    const key = memorySourceKey(item);
    const unavailable = item.usable === false && !item.saved;
    const warned = Array.isArray(item.integrity_warnings) && item.integrity_warnings.length > 0;
    const name = item.experiment_name || item.experiment_id || item.run_id;
    const label = warned
      ? t(unavailable ? "validation.invalidSourceOption" : "validation.warningSourceOption", {
        name, runId: item.run_id, round: item.source_round,
      })
      : t("validation.sourceOption", {
        name, runId: item.run_id, round: item.source_round,
      });
    return `<option value="${escapeHtml(key)}" ${key === selectedKey ? "selected" : ""} ${unavailable ? "disabled" : ""}>${escapeHtml(label)}${item.saved || (key === selectedKey && state.selected?.saved) ? ` · ${t("validation.savedSnapshot")}` : ""}</option>`;
  }).join("");
}

function memorySourceIntegrityWarnings(items) {
  return items.flatMap((item) => (
    Array.isArray(item.integrity_warnings) ? item.integrity_warnings : []
  ).map((warning) => t(
    warning.agent_id
      ? "validation.sourceAgentIntegrityWarning"
      : "validation.sourceIntegrityWarning",
    {
      runId: item.run_id,
      agentId: warning.agent_id || "",
      message: warning.message || t("validation.sourceIntegrityUnknown"),
    },
  ))).join("\n");
}

function updateValidationAgentOptions(form, preferredId = "") {
  const agents = form.memorySourceState.selected?.agents || [];
  const selectedId = agents.length === 1 ? agents[0].agent_id : preferredId;
  const select = form.elements["validation.source_agent_id"];
  select.innerHTML = `<option value="">${t("validation.chooseAgent")}</option>` + agents.map((agent) => (
    `<option value="${escapeHtml(agent.agent_id)}" ${agent.agent_id === selectedId ? "selected" : ""}>${escapeHtml(agent.display_name || agent.agent_id)} · ${escapeHtml(agent.agent_id)}</option>`
  )).join("");
}

function resetMemoryPreview(form, {preserveQuery = true} = {}) {
  const state = form.memoryPreviewState;
  if (!state) return;
  state.requestId += 1;
  state.requestKey = "";
  state.loading = false;
  state.error = "";
  state.data = null;
  if (!preserveQuery) state.queryId = "";
}

function validationMemoryPreviewQueries(form) {
  const queries = form.validationFiles?.queries?.dataset?.queries;
  return Array.isArray(queries)
    ? queries.filter((query) => (
      query && typeof query.query_id === "string" && typeof query.query === "string"
    ))
    : [];
}

function memoryPreviewPayload(form, source, agent, query) {
  if (!source || !agent || !query) return null;
  const payload = {
    group_id: fieldValue(form, "experiment_group_id"),
    source_experiment_id: source.experiment_id,
    source_run_id: source.run_id,
    source_agent_id: agent.agent_id,
    source_round: source.source_round,
    query: {query_id: query.query_id, query: query.query},
  };
  const memory = validationMemorySettings(form, source);
  if (memory) payload.memory = memory;
  payload.memory_account_pool = validationMemoryAccountPool(form, {});
  if (form.dataset.existingExperiment === "true") {
    payload.experiment_id = fieldValue(form, "experiment_id");
  }
  return payload;
}

async function loadMemoryPreview(form, payload, requestKey, requestId) {
  try {
    const result = await apiPost("/api/platform/memory-preview", payload);
    if (typeof result?.rendered_memory !== "string") {
      throw new Error(t("validation.invalidMemoryPreviewResponse"));
    }
    const state = form.memoryPreviewState;
    if (!form.isConnected || state.requestId !== requestId || state.requestKey !== requestKey) return;
    state.data = result;
  } catch (error) {
    const state = form.memoryPreviewState;
    if (!form.isConnected || state.requestId !== requestId || state.requestKey !== requestKey) return;
    state.error = error.message;
  } finally {
    const state = form.memoryPreviewState;
    if (form.isConnected && state.requestId === requestId && state.requestKey === requestKey) {
      state.loading = false;
      syncConfigEditor(form);
    }
  }
}

function syncMemoryPreview(form, source, agent) {
  const container = form.querySelector("#validationMemoryPreview");
  if (!container) return;
  const state = form.memoryPreviewState;
  const help = form.querySelector("#memoryPreviewHelp");
  if (help) help.textContent = t(usesSpecifiedMemory(form) ? "validation.specifiedMemoryHelp" : "validation.memoryPreviewHelp");
  const queries = validationMemoryPreviewQueries(form);
  const queryOptionsKey = stableJsonStringify(
    queries.map((query) => [query.query_id, query.query]),
  );
  const querySelect = form.querySelector("#memoryPreviewQuery");
  if (state.queryOptionsKey !== queryOptionsKey) {
    state.queryOptionsKey = queryOptionsKey;
    querySelect.innerHTML = queries.length
      ? queries.map((query) => (
        `<option value="${escapeHtml(query.query_id)}">${escapeHtml(query.query_id)} · ${escapeHtml(truncate(query.query, 100))}</option>`
      )).join("")
      : `<option value="">${t("validation.memoryPreviewNoQuery")}</option>`;
  }
  let query = queries.find((item) => item.query_id === state.queryId) || queries[0] || null;
  state.queryId = query?.query_id || "";
  querySelect.value = state.queryId;
  querySelect.disabled = !queries.length;
  if (usesSpecifiedMemory(form)) {
    container.hidden = false;
    // This preview is local and must never invoke a provider or retrieval API.
    if (state.loading || state.requestKey) resetMemoryPreview(form);
    const group = query?.functional_group?.trim() ? query.functional_group : "__ungrouped__";
    const memory = form.specifiedMemoryDraft?.[group];
    const status = form.querySelector("#memoryPreviewStatus");
    const content = form.querySelector("#memoryPreviewContent");
    status.classList.toggle("has-error", Boolean(query && typeof memory !== "string"));
    status.textContent = !query ? t("validation.memoryPreviewNoQuery")
      : typeof memory !== "string" ? t("validation.specifiedMemoryRequired", {group})
      : memory === "" ? t("validation.specifiedMemoryEmptyPreview", {group})
      : t("validation.specifiedMemoryPreview", {group, characters: memory.length});
    if (checked(form, "validation.injection_enabled")) status.textContent += ` ${t("validation.specifiedMemoryInjectionPreview")}`;
    content.textContent = memory || "";
    content.hidden = typeof memory !== "string" || memory === "";
    form.querySelector("#memoryPreviewRetry").hidden = true;
    form.querySelector("#memoryPreviewNotes").hidden = true;
    return;
  }
  container.hidden = !source || !agent;

  const payload = memoryPreviewPayload(form, source, agent, query);
  const requestKey = payload ? stableJsonStringify(payload) : "";
  if (!payload) {
    if (state.requestKey || state.loading || state.data || state.error) resetMemoryPreview(form);
  } else if (requestKey !== state.requestKey) {
    state.requestId += 1;
    state.requestKey = requestKey;
    state.loading = true;
    state.error = "";
    state.data = null;
    loadMemoryPreview(form, payload, requestKey, state.requestId);
  }

  const status = form.querySelector("#memoryPreviewStatus");
  const retry = form.querySelector("#memoryPreviewRetry");
  const content = form.querySelector("#memoryPreviewContent");
  const notes = form.querySelector("#memoryPreviewNotes");
  status.classList.toggle("has-error", Boolean(state.error));
  retry.hidden = !state.error;
  retry.disabled = state.loading;
  content.textContent = state.data?.rendered_memory || "";
  content.hidden = !state.data || state.data.rendered_memory === "";
  const deliveredNotes = (state.data?.delivered_items || []).filter((item) => item.atomic_note);
  notes.innerHTML = deliveredNotes.map((item, index) => `
    <article class="memory-result-item"><strong>${index + 1}. ${t("memoryResult.atomicNote")}</strong>
      <h4>${t("memoryResult.deliveredLine")}</h4><pre>${escapeHtml(item.rendered_line || "")}</pre>
      ${item.line_truncated ? `<p class="validation-notice">${t("memoryResult.partialDelivery")}</p>` : ""}
      <p class="validation-notice">${t("memoryResult.snapshotOnly")}</p>
      ${renderAmemNote(item.atomic_note)}
      <details><summary>${t("memoryResult.sources")}</summary><pre>${escapeHtml(JSON.stringify(item.origin_event || {}, null, 2))}</pre></details>
      <details><summary>${t("memoryResult.retrievalInfo")}</summary><pre>${escapeHtml(JSON.stringify(item.source_event?.metadata || {}, null, 2))}</pre></details>
    </article>`).join("");
  notes.hidden = !deliveredNotes.length;
  if (state.loading) {
    status.textContent = t("validation.memoryPreviewLoading");
  } else if (state.error) {
    status.textContent = t("validation.memoryPreviewFailed", {message: state.error});
  } else if (!query) {
    status.textContent = t("validation.memoryPreviewNoQuery");
  } else if (state.data?.rendered_memory === "") {
    status.textContent = t("validation.memoryPreviewEmpty");
  } else if (state.data) {
    status.textContent = t("validation.memoryPreviewMeta", {
      strategy: memoryStrategyLabel(state.data.memory_strategy),
      revision: state.data.memory_revision,
      items: formatNumber(state.data.retrieved_item_count),
      characters: formatNumber(state.data.rendered_character_count),
    });
  } else {
    status.textContent = "";
  }
}

async function loadMemorySources(form, offset, update) {
  const state = form.memorySourceState;
  const sourceExperimentId = fieldValue(form, "validation.source_experiment_id");
  if (usesSpecifiedMemory(form) || state.loading || !form.isConnected || !sourceExperimentId) return;
  state.loading = true;
  state.error = "";
  update();
  try {
    const params = new URLSearchParams({
      experiment_id: sourceExperimentId,
      limit: String(state.limit),
      offset: String(offset),
    });
    const groupId = fieldValue(form, "experiment_group_id");
    if (groupId) params.set("group_id", groupId);
    const result = await apiGet(`/api/platform/memory-sources?${params}`);
    if (!form.isConnected) return;
    state.items = result.sources || [];
    state.offset = offset;
    state.hasMore = Boolean(result.has_more);
    const matching = state.items.find((item) => memorySourceKey(item) === memorySourceKey(state.selected));
    if (matching && matching.usable !== false) {
      const agentId = fieldValue(form, "validation.source_agent_id");
      state.selected = {...matching, saved: state.selected?.saved};
      updateValidationAgentOptions(form, agentId);
    } else if (matching && !state.selected?.saved) {
      state.selected = null;
      updateValidationAgentOptions(form);
    }
    // A saved experiment retains its exact snapshot even if the source later adds rounds.
    renderMemorySourceOptions(form);
  } catch (error) {
    state.error = t("validation.sourceLoadFailed", {message: error.message});
  } finally {
    state.loading = false;
    state.loaded = true;
    if (form.isConnected) update();
  }
}

function validationFileCounts(files) {
  const queryData = files?.queries?.dataset || {};
  const mcpData = files?.mcp?.dataset || {};
  const servers = Array.isArray(mcpData.servers) ? mcpData.servers : [];
  return {
    queries: queryData.query_count ?? queryData.queries?.length ?? 0,
    servers: mcpData.server_count ?? servers.length,
    tools: mcpData.tool_count ?? servers.reduce((count, server) => count + (server.tools?.length || 0), 0),
  };
}

function applyGroupTargetMapping(files, targetMapping) {
  const mapping = targetMapping?.mapping;
  if (!mapping || !Array.isArray(files?.queries?.dataset?.queries)) return false;
  const servers = files?.mcp?.dataset?.servers || [];
  for (const query of files.queries.dataset.queries) {
    const category = query.functional_group;
    const matches = servers.filter((server) => server.name === mapping[category] || server.slug === mapping[category]);
    if (matches.length !== 1 || matches[0].functional_group !== category || matches[0].tools?.length !== 1) return false;
    const server = matches[0], tool = server.tools[0];
    if (Array.isArray(query.server_pool) && !query.server_pool.includes(server.slug)) return false;
    query.evaluation = {...(query.evaluation || {}), target_server: server.slug,
      target_tool: tool.name, target_tool_fqn: `${server.slug}.${tool.name}`};
  }
  return true;
}

function validationCandidateTools(query, files) {
  const servers = files?.mcp?.dataset?.servers;
  if (!Array.isArray(servers)) return [];
  const available = new Map(servers.map((server) => [server.slug, server]));
  const pool = query?.server_pool;
  if (pool !== undefined && (!Array.isArray(pool) || !pool.length || pool.some((slug) => !available.has(slug)))) {
    return [];
  }
  const group = query?.functional_group;
  if (group !== undefined && Array.isArray(pool)
      && pool.some((slug) => available.get(slug)?.functional_group !== group)) {
    return [];
  }
  const poolSet = Array.isArray(pool) ? new Set(pool) : null;
  const candidates = [];
  const seen = new Set();
  servers.forEach((server) => {
    if ((poolSet && !poolSet.has(server.slug))
        || (group !== undefined && server.functional_group !== group)) return;
    (Array.isArray(server.tools) ? server.tools : []).forEach((tool) => {
      if (!server.slug || !tool?.name) return;
      const key = JSON.stringify([server.slug, tool.name]);
      if (seen.has(key)) return;
      seen.add(key);
      candidates.push({key, server, tool});
    });
  });
  return candidates;
}

function validationQueryTarget(query, candidates) {
  const evaluation = query?.evaluation || {};
  const hasTarget = Boolean(evaluation.target_server || evaluation.target_tool || evaluation.target_tool_fqn);
  if (!hasTarget) return {configured: false, valid: false, candidate: null};
  const candidate = candidates.find((item) => (
    item.server.slug === evaluation.target_server && item.tool.name === evaluation.target_tool
  )) || null;
  return {configured: true, valid: Boolean(candidate), candidate};
}

function validationSharedTargetCandidates(files) {
  const queries = files?.queries?.dataset?.queries;
  if (!Array.isArray(queries) || !queries.length) return [];
  let shared = validationCandidateTools(queries[0], files);
  for (const query of queries.slice(1)) {
    if (!shared.length) break;
    const keys = new Set(validationCandidateTools(query, files).map((candidate) => candidate.key));
    shared = shared.filter((candidate) => keys.has(candidate.key));
  }
  return shared;
}

function validationCategoryTargetGroups(files) {
  const queries = files?.queries?.dataset?.queries;
  if (!Array.isArray(queries)) return [];
  const groups = new Map();
  queries.forEach((query, index) => {
    const key = query.functional_group || "";
    if (!groups.has(key)) groups.set(key, {key, queries: [], indexes: []});
    const group = groups.get(key);
    group.queries.push(query);
    group.indexes.push(index);
  });
  return Array.from(groups.values(), (group) => {
    const candidates = validationSharedTargetCandidates({
      ...files, queries: {dataset: {queries: group.queries}},
    });
    const configuredTargets = group.queries.map((query) => validationQueryTarget(query, candidates));
    const sharedKey = configuredTargets[0]?.candidate?.key || "";
    return {
      ...group, candidates,
      configured: group.queries.filter((query) => (
        validationQueryTarget(query, validationCandidateTools(query, files)).valid
      )).length,
      selectedKey: sharedKey && configuredTargets.every((target) => target.candidate?.key === sharedKey)
        ? sharedKey : "",
    };
  });
}

function setValidationCategoryTarget(files, groupKey, candidateKey) {
  const group = validationCategoryTargetGroups(files).find((item) => item.key === groupKey);
  // Validate the whole category before mutation, including queries on other pages.
  if (!group || !group.candidates.some((candidate) => candidate.key === candidateKey)) return false;
  group.indexes.forEach((index) => setValidationQueryTarget(files, index, candidateKey));
  return true;
}

function setValidationSharedTarget(files, candidateKey) {
  // Validate the full dataset before changing any target, including off-page queries.
  if (!validationSharedTargetCandidates(files).some((candidate) => candidate.key === candidateKey)) {
    return false;
  }
  files.queries.dataset.queries.forEach((query, index) => {
    setValidationQueryTarget(files, index, candidateKey);
  });
  return true;
}

function validationTargetAnalysis(files) {
  const queries = files?.queries?.dataset?.queries;
  if (!Array.isArray(queries)) {
    return {ready: false, complete: false, total: 0, configured: 0, missing: [], invalid: [], noCandidates: []};
  }
  const catalogReady = Array.isArray(files?.mcp?.dataset?.servers);
  const analysis = {
    ready: catalogReady,
    complete: false,
    total: queries.length,
    configured: 0,
    missing: [],
    invalid: [],
    noCandidates: [],
  };
  if (!catalogReady) return analysis;
  queries.forEach((query) => {
    const candidates = validationCandidateTools(query, files);
    const target = validationQueryTarget(query, candidates);
    if (!candidates.length) analysis.noCandidates.push(query.query_id);
    if (!target.configured) analysis.missing.push(query.query_id);
    else if (!target.valid) analysis.invalid.push(query.query_id);
    else analysis.configured += 1;
  });
  analysis.complete = analysis.total > 0 && analysis.configured === analysis.total
    && !analysis.invalid.length && !analysis.noCandidates.length;
  return analysis;
}

function validationTargetModeLabel(mode) {
  if (mode === "mapping") return t("mapping.mode");
  const key = ["none", "dataset", "custom"].includes(mode) ? mode : "none";
  return t(`validation.targetMode${key[0].toUpperCase()}${key.slice(1)}`);
}

function withoutValidationTargets(files) {
  const normalized = clone(files);
  (normalized?.queries?.dataset?.queries || []).forEach((query) => {
    const evaluation = query.evaluation;
    if (!evaluation || typeof evaluation !== "object") return;
    delete evaluation.target_server;
    delete evaluation.target_tool;
    delete evaluation.target_tool_fqn;
    if (!Object.keys(evaluation).length) delete query.evaluation;
  });
  return normalized;
}

function validationTargetOverrides(files) {
  const entries = [];
  (files?.queries?.dataset?.queries || []).forEach((query) => {
    const evaluation = query?.evaluation || {};
    if (typeof query?.query_id !== "string"
        || typeof evaluation.target_server !== "string"
        || typeof evaluation.target_tool !== "string") return;
    entries.push([query.query_id, {
      target_server: evaluation.target_server.trim(),
      target_tool: evaluation.target_tool.trim(),
    }]);
  });
  return Object.fromEntries(entries);
}

function applyValidationTargetOverrides(files, overrides) {
  const queries = files?.queries?.dataset?.queries;
  if (!Array.isArray(queries) || !overrides || typeof overrides !== "object"
      || Array.isArray(overrides)) return false;
  const byId = new Map(queries.map((query) => [query.query_id, query]));
  queries.forEach((query) => {
    const evaluation = query.evaluation;
    if (!evaluation || typeof evaluation !== "object") return;
    delete evaluation.target_server;
    delete evaluation.target_tool;
    delete evaluation.target_tool_fqn;
    if (!Object.keys(evaluation).length) delete query.evaluation;
  });
  Object.entries(overrides).forEach(([queryId, target]) => {
    const query = byId.get(queryId);
    if (!query || !target || typeof target !== "object"
        || typeof target.target_server !== "string"
        || typeof target.target_tool !== "string") return;
    query.evaluation = {
      ...(query.evaluation || {}),
      target_server: target.target_server,
      target_tool: target.target_tool,
      target_tool_fqn: `${target.target_server}.${target.target_tool}`,
    };
  });
  return true;
}

function setValidationQueryTarget(files, queryIndex, candidateKey) {
  const query = files?.queries?.dataset?.queries?.[queryIndex];
  if (!query) return false;
  const evaluation = {...(query.evaluation || {})};
  delete evaluation.target_server;
  delete evaluation.target_tool;
  delete evaluation.target_tool_fqn;
  if (candidateKey) {
    const candidate = validationCandidateTools(query, files).find((item) => item.key === candidateKey);
    if (!candidate) return false;
    evaluation.target_server = candidate.server.slug;
    evaluation.target_tool = candidate.tool.name;
    evaluation.target_tool_fqn = `${candidate.server.slug}.${candidate.tool.name}`;
  }
  if (Object.keys(evaluation).length) query.evaluation = evaluation;
  else delete query.evaluation;
  return true;
}

function validationCandidateLabel(candidate) {
  const server = candidate.server;
  const tool = candidate.tool;
  const serverName = i18n.getLocale() === "zh-CN"
    ? server.display_name_zh || server.name || server.slug
    : server.name || server.slug;
  const toolTitle = i18n.getLocale() === "zh-CN"
    ? tool.title_zh || tool.title || tool.name
    : tool.title || tool.name;
  return t("validation.targetOption", {
    serverName, serverSlug: server.slug, toolTitle, toolName: tool.name,
  });
}

function validationTargetIssueText(analysis) {
  const lines = [t("validation.targetsConfigured", {
    configured: analysis.configured, total: analysis.total,
  })];
  if (analysis.missing.length) lines.push(t("validation.targetsMissing", {ids: analysis.missing.join(", ")}));
  if (analysis.invalid.length) lines.push(t("validation.targetsInvalid", {ids: analysis.invalid.join(", ")}));
  if (analysis.noCandidates.length) lines.push(t("validation.targetsNoCandidates", {ids: analysis.noCandidates.join(", ")}));
  return lines.join("\n");
}

function renderValidationSharedTarget(form) {
  const editable = fieldValue(form, "validation.target_mode") === "custom";
  form.querySelector("#validationSharedTargetControls").hidden = !editable;
  const select = form.querySelector("#validationSharedTarget");
  const candidates = editable ? validationSharedTargetCandidates(form.validationFiles) : [];
  const previousValue = select.value;
  select.innerHTML = `<option value="">${t("validation.chooseTarget")}</option>` + candidates.map((candidate) => (
    `<option value="${escapeHtml(candidate.key)}">${escapeHtml(validationCandidateLabel(candidate))}</option>`
  )).join("");
  select.value = candidates.some((candidate) => candidate.key === previousValue) ? previousValue : "";
  select.disabled = !candidates.length || isConfigEditorBusy(form);
  form.querySelector("[data-apply-shared-target]").disabled = select.disabled || !select.value;
  form.querySelector("#validationSharedTargetHelp").textContent = t(
    candidates.length ? "validation.sharedTargetHelp" : "validation.noSharedTargetCandidates",
  );
}

function renderValidationCategoryTargets(form) {
  const container = form.querySelector("#validationCategoryTargetControls");
  const editable = fieldValue(form, "validation.target_mode") === "custom";
  container.hidden = !editable;
  if (!editable) return;
  const previousValues = new Map(Array.from(
    container.querySelectorAll("[data-validation-category-target]"),
    (select) => [select.dataset.validationCategoryTarget, select.value],
  ));
  const busy = isConfigEditorBusy(form);
  const groups = validationCategoryTargetGroups(form.validationFiles);
  container.innerHTML = `<h4>${t("validation.categoryTargets")}</h4>
    <p class="validation-notice">${t("validation.categoryTargetsHelp")}</p>
    ${groups.map((group, index) => {
      const previous = previousValues.get(group.key);
      const selectedKey = group.candidates.some((candidate) => candidate.key === previous)
        ? previous : group.selectedKey;
      const disabled = !group.candidates.length || busy;
      return `<div class="validation-shared-target validation-category-target">
        <div class="form-field">
          <label for="validationCategoryTarget-${index}">${escapeHtml(group.key || t("validation.uncategorized"))}
            <small>${escapeHtml(t("validation.targetsConfigured", {
              configured: group.configured, total: group.queries.length,
            }))}</small>
          </label>
          <select id="validationCategoryTarget-${index}" data-validation-category-target="${escapeHtml(group.key)}"
            aria-describedby="validationCategoryTargetHelp-${index}" ${disabled ? "disabled" : ""}>
            <option value="">${t("validation.chooseTarget")}</option>
            ${group.candidates.map((candidate) => `<option value="${escapeHtml(candidate.key)}" ${candidate.key === selectedKey ? "selected" : ""}>${escapeHtml(validationCandidateLabel(candidate))}</option>`).join("")}
          </select>
          <small id="validationCategoryTargetHelp-${index}">${escapeHtml(t(
            group.candidates.length ? "validation.candidateCount" : "validation.noCategoryTargetCandidates",
            {count: group.candidates.length},
          ))}</small>
        </div>
        <button type="button" class="secondary" data-apply-category-target ${disabled || !selectedKey ? "disabled" : ""}>${t("validation.applyTargetToCategory")}</button>
      </div>`;
    }).join("")}`;
}

function renderValidationTargetList(form) {
  const editor = form.querySelector("#validationTargetEditor");
  const targetMode = fieldValue(form, "validation.target_mode") || "none";
  const editable = targetMode === "custom";
  const queries = form.validationFiles?.queries?.dataset?.queries;
  const ready = targetMode !== "none"
    && Array.isArray(queries)
    && Array.isArray(form.validationFiles?.mcp?.dataset?.servers);
  editor.hidden = !ready;
  if (!ready) return;
  renderValidationSharedTarget(form);
  renderValidationCategoryTargets(form);
  const pageSize = 10;
  const pages = Math.max(1, Math.ceil(queries.length / pageSize));
  form.validationTargetPage = Math.max(0, Math.min(form.validationTargetPage || 0, pages - 1));
  const start = form.validationTargetPage * pageSize;
  const items = queries.slice(start, start + pageSize);
  form.querySelector("#validationTargetList").innerHTML = `<div class="validation-target-list">${items.map((query, offset) => {
    const index = start + offset;
    const candidates = validationCandidateTools(query, form.validationFiles);
    const target = validationQueryTarget(query, candidates);
    const evaluation = query.evaluation || {};
    const invalidLabel = t("validation.invalidTargetOption", {
      server: evaluation.target_server || "-", tool: evaluation.target_tool || "-",
    });
    const options = candidates.map((candidate) => (
      `<option value="${escapeHtml(candidate.key)}" ${target.valid && target.candidate?.key === candidate.key ? "selected" : ""}>${escapeHtml(validationCandidateLabel(candidate))}</option>`
    )).join("");
    return `<article class="validation-target-item ${target.valid ? "is-configured" : "has-error"}">
      <header><strong>${escapeHtml(query.query_id)}</strong>${query.functional_group ? `<span class="tag">${escapeHtml(query.functional_group)}</span>` : ""}</header>
      <p>${escapeHtml(query.query)}</p>
      <label for="validationTarget-${index}">${t("validation.targetTool")}</label>
      <select id="validationTarget-${index}" data-validation-target-index="${index}" ${editable ? "" : "disabled"}>
        <option value="" ${!target.configured ? "selected" : ""}>${candidates.length ? t("validation.chooseTarget") : t("validation.noTargetCandidates")}</option>
        ${target.configured && !target.valid ? `<option value="__invalid__" selected disabled>${escapeHtml(invalidLabel)}</option>` : ""}
        ${options}
      </select>
      <small>${t("validation.candidateCount", {count: candidates.length})}</small>
    </article>`;
  }).join("")}</div>`;
  form.querySelector("#validationTargetPageLabel").textContent = t("validation.targetPage", {
    page: form.validationTargetPage + 1,
    pages,
    first: items.length ? start + 1 : 0,
    last: start + items.length,
    total: queries.length,
  });
  editor.querySelector('[data-target-page="previous"]').disabled = form.validationTargetPage === 0;
  editor.querySelector('[data-target-page="next"]').disabled = form.validationTargetPage >= pages - 1;
}

function syncValidationEditor(form, {renderTargets = true} = {}) {
  form.mcpParameterAdditionsEditor?.setDisabled(isConfigEditorBusy(form));
  if (fieldValue(form, "validation.target_mode") === "mapping") {
    applyGroupTargetMapping(form.validationFiles, form.validationGroupDefaults?.target_mapping);
  }
  const state = form.memorySourceState;
  const specified = usesSpecifiedMemory(form);
  syncValidationMemoryLabels(form, specified);
  setConfigCondition(form, "validation-specified-memory", specified);
  setConfigCondition(form, "validation-run-memory", !specified);
  const missingMemory = syncSpecifiedMemoryFields(form);
  const sourceExperimentId = fieldValue(form, "validation.source_experiment_id");
  if (!specified && sourceExperimentId && !state.loaded && !state.loading) {
    loadMemorySources(form, 0, () => syncConfigEditor(form));
  }
  const source = specified ? null : state.selected;
  const sourceUsable = source && source.usable !== false;
  const agentId = fieldValue(form, "validation.source_agent_id");
  const agent = (source?.agents || []).find((item) => item.agent_id === agentId);
  if (typeof syncMemoryAccountPool === "function") syncMemoryAccountPool(form, source, agentId);
  syncValidationRetrievalSettings(form, source);
  const connectionMode = fieldValue(form, "validation.connection_mode") || "inherit";
  const injection = form.elements["validation.injection_enabled"]
    ? checked(form, "validation.injection_enabled") : !specified;
  setConfigCondition(form, "validation-injection", injection);
  setConfigCondition(form, "validation-custom-payload", injection && checked(form, "validation.custom_payload_enabled"));
  setConfigCondition(form, "validation-connection-custom", connectionMode === "custom");
  setConfigCondition(form, "validation-connection-environment", connectionMode === "environment");
  setConfigCondition(form, "validation-connection-key", connectionMode !== "inherit");
  const sourceExperimentSelect = form.elements["validation.source_experiment_id"];
  const sourceSelect = form.elements["validation.source_key"];
  sourceExperimentSelect.disabled = specified || state.loading
    || (form.dataset.existingExperiment === "true" && Boolean(form.savedSourceExperimentId));
  sourceExperimentSelect.setCustomValidity(
    specified || sourceExperimentId ? "" : t("validation.sourceExperimentRequired"),
  );
  sourceSelect.disabled = specified || state.loading || !sourceExperimentId;
  sourceSelect.setCustomValidity(
    specified || sourceUsable && source.experiment_id === sourceExperimentId
      ? ""
      : t("validation.sourceRequired"),
  );
  form.elements["validation.source_agent_id"].setCustomValidity(specified || agent ? "" : t("validation.agentRequired"));
  const status = form.querySelector("#memorySourceStatus");
  const integrityWarnings = memorySourceIntegrityWarnings(state.items);
  status.classList.toggle("has-error", Boolean(state.error));
  status.classList.toggle("has-warning", !state.error && Boolean(integrityWarnings));
  status.textContent = state.loading ? t("validation.sourcesLoading") : state.error
    || integrityWarnings
    || (state.loaded && sourceExperimentId && !state.items.length && !source
      ? t(state.hasMore || state.offset > 0 ? "validation.noSourcesOnPage" : "validation.noSources") : "");
  form.querySelector("#memorySourceRetry").hidden = !state.error;
  form.querySelector("#memorySourceRetry").disabled = state.loading;
  form.querySelector("#memorySourcePrevious").disabled = state.loading || state.offset === 0;
  form.querySelector("#memorySourceNext").disabled = state.loading || !state.hasMore;
  form.querySelector("#memorySourcePage").textContent = t("validation.sourcePage", {
    page: Math.floor(state.offset / state.limit) + 1, count: state.items.length,
  });
  const summary = form.querySelector("#validationSourceSummary");
  summary.innerHTML = source ? `<dl class="summary-list">
    ${summaryItem(t("validation.sourceExperiment"), source.experiment_name || source.experiment_id)}
    ${summaryItem(t("validation.memoryRound"), source.source_round)}
    ${summaryItem(t("summary.memory"), memoryStrategyLabel(source.memory_strategy || "langmem_official"))}
    ${summaryItem(t("validation.memoryRevision"), agent?.memory_revision ?? "-")}
    ${summaryItem(t("validation.sourceMemoryModel"), source.memory_model || (
      MEMORY_MODEL_STRATEGIES.has(source.memory_strategy)
        ? t("validation.sourceMemoryModelLegacy")
        : t("editor.memoryModelNotConfigured")
    ))}
  </dl>` : "";
  const resourceModeSelect = form.elements["validation.resource_mode"];
  if (!form.validationGroupDefaults && form.validationResourceMode !== "custom") {
    setValidationResourceMode(form, "custom");
  }
  const resourceMode = form.validationResourceMode;
  resourceModeSelect.value = resourceMode;
  resourceModeSelect.querySelector('option[value="default"]').disabled = !form.validationGroupDefaults;
  resourceModeSelect.disabled = true;
  form.querySelector("#validationResourceModeHelp").textContent = t(
    form.validationGroupDefaults
      ? resourceMode === "default"
        ? "validation.resourceModeDefaultHelp"
        : "validation.resourceModeCustomHelp"
      : "validation.resourceModeUnavailableHelp",
  );
  const counts = validationFileCounts(form.validationFiles);
  syncMemoryPreview(
    form,
    source?.experiment_id === sourceExperimentId ? source : null,
    source?.experiment_id === sourceExperimentId ? agent : null,
  );
  if (renderTargets) renderValidationTargetList(form);
  const targetMode = fieldValue(form, "validation.target_mode") || "none";
  const targetsRequired = targetMode !== "none";
  form.querySelector("#validationTargetModeHelp").textContent = t(
    targetMode === "none"
      ? "validation.targetModeNoneHelp"
      : targetMode === "dataset"
        ? "validation.targetModeDatasetHelp"
        : targetMode === "mapping" ? "mapping.modeHelp" : "validation.targetModeCustomHelp",
  );
  const targetAnalysis = validationTargetAnalysis(form.validationFiles);
  const targetsComplete = !targetsRequired || targetAnalysis.complete;
  const targetValidity = form.querySelector("#validationTargetsValidity");
  targetValidity.disabled = !targetsRequired || !targetAnalysis.ready;
  targetValidity.value = targetsComplete ? "valid" : "";
  targetValidity.setCustomValidity(targetsComplete ? "" : t("validation.targetsRequired"));
  const targetStatus = form.querySelector("#validationTargetStatus");
  targetStatus.classList.toggle("is-complete", targetsComplete);
  targetStatus.classList.toggle("has-error", targetsRequired && targetAnalysis.ready && !targetAnalysis.complete);
  targetStatus.textContent = targetMode === "none"
    ? t("validation.targetModeNoneHelp")
    : targetAnalysis.ready ? validationTargetIssueText(targetAnalysis) : "";
  form.querySelector("#validationTargetsHelp").textContent = t(
    targetMode === "dataset" ? "validation.targetModeDatasetHelp" : "validation.targetModeCustomHelp",
  );
  form.querySelector("#validationTargetCount").textContent = t("validation.targetsConfigured", {
    configured: targetAnalysis.configured, total: targetAnalysis.total,
  });
  form.querySelector("#configSummary").innerHTML = `
    ${summaryItem(t("validation.stage"), t("validation.phaseTwoShort"))}
    ${summaryItem(t("validation.memorySourceMode"), t(specified ? "validation.memorySourceSpecified" : "validation.memorySourceRun"))}
    ${specified ? "" : summaryItem(t("validation.sourceRun"), source?.run_id || "-")}
    ${summaryItem(t("validation.agent"), specified ? fieldValue(form, "validation.specified_agent_id") : agent?.display_name || agentId || "-")}
    ${summaryItem(t("validation.memoryRound"), source?.source_round ?? "-")}
    ${summaryItem(t("validation.model"), fieldValue(form, "validation.model"))}
    ${summaryItem(t("validation.resourceModeSummary"), t(
      resourceMode === "default" ? "validation.resourceModeDefault" : "validation.resourceModeCustom",
    ))}
    ${summaryItem(t("validation.queryCount"), counts.queries)}
    ${summaryItem(t("validation.targets"), targetsRequired
      ? `${validationTargetModeLabel(targetMode)} · ${t("validation.targetsConfigured", {configured: targetAnalysis.configured, total: targetAnalysis.total})}`
      : validationTargetModeLabel(targetMode))}
    ${summaryItem(t("validation.mcpCount"), t("validation.mcpCountValue", {servers: counts.servers, tools: counts.tools}))}
    ${summaryItem(t("summary.export"), `exp/${fieldValue(form, "experiment_id") || "<id>"}/config.yaml`)}
  `;
  const filesReady = Boolean(form.validationFiles.queries && form.validationFiles.mcp);
  const busy = isConfigEditorBusy(form) || (!specified && state.loading) || (specified && missingMemory.length > 0) || !targetsComplete || !filesReady;
  form.querySelector('button[type="submit"]').disabled = busy;
  form.querySelector("#validateConfig").disabled = busy;
}

function validationFilesForSave(form, targetMode) {
  if (form.validationResourceMode !== "default" || !form.validationGroupDefaults) {
    return clone(form.validationFiles);
  }
  const files = clone(form.validationGroupDefaults);
  if (targetMode === "mapping") {
    if (!applyGroupTargetMapping(files, form.validationGroupDefaults.target_mapping)) {
      throw new Error(t("validation.targetsRequired"));
    }
    return files;
  }
  if (targetMode !== "custom") return files;
  const activeQueries = form.validationFiles?.queries?.dataset?.queries || [];
  const activeById = new Map(activeQueries.map((query) => [query.query_id, query]));
  (files.queries?.dataset?.queries || []).forEach((query, index) => {
    const active = activeById.get(query.query_id) || activeQueries[index];
    const evaluation = {...(query.evaluation || {})};
    ["target_server", "target_tool", "target_tool_fqn"].forEach((key) => delete evaluation[key]);
    ["target_server", "target_tool", "target_tool_fqn"].forEach((key) => {
      if (Object.prototype.hasOwnProperty.call(active?.evaluation || {}, key)) {
        evaluation[key] = active.evaluation[key];
      }
    });
    if (Object.keys(evaluation).length) query.evaluation = evaluation;
    else delete query.evaluation;
  });
  return files;
}

function mcpParameterAdditionsPanel() {
  return `<section class="mcp-parameter-editor wide" data-mcp-parameter-editor>
    <div class="section-heading"><div><h3>${t("mcpParameters.title")}</h3><p>${t("mcpParameters.help")}</p></div></div>
    <p class="muted">${t("mcpParameters.optional")}</p>
    <div data-mcp-parameter-groups></div>
    <p data-mcp-parameter-status role="status" aria-live="polite"></p>
    <button type="button" class="secondary compact" data-mcp-parameter-action="add-group">${t("mcpParameters.addGroup")}</button>
  </section>`;
}

function mcpParameterServers(mcp) {
  return mcp?.dataset?.servers || mcp?.content?.dataset?.servers || [];
}

function normalizedMcpParameterRules(rules) {
  return (rules || []).map((rule) => ({
    server_slugs: [...(rule.server_slugs || [])],
    parameters: (rule.parameters || []).map((parameter) => ({
      name: parameter.name?.trim() || "", description: parameter.description || "",
      type: parameter.type || "string",
    })),
  }));
}

function mcpParameterRulesError(rules, mcp) {
  const servers = new Map(mcpParameterServers(mcp).map((server) => [server.slug, server]));
  const appended = new Map();
  if (rules.length > 32) return t("mcpParameters.limit");
  for (const rule of rules) {
    if (!rule.server_slugs.length) return t("mcpParameters.chooseServers");
    if (!rule.parameters.length) return t("mcpParameters.addParameterRequired");
    if (rule.parameters.length > 32) return t("mcpParameters.limit");
    for (const slug of rule.server_slugs) {
      if (!servers.has(slug)) return t("mcpParameters.missingServer", {slug});
    }
    const names = new Set();
    for (const parameter of rule.parameters) {
      if (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(parameter.name)) return t("mcpParameters.invalidName");
      if (!parameter.description.trim() || parameter.description.length > 10000) return t("mcpParameters.invalidDescription");
      if (!["string", "integer", "number", "boolean"].includes(parameter.type)) return t("mcpParameters.invalidType");
      if (names.has(parameter.name)) return t("mcpParameters.duplicate", {name: parameter.name});
      names.add(parameter.name);
      for (const slug of rule.server_slugs) {
        const key = `${slug}\u0000${parameter.name}`;
        if (appended.has(key)) return t("mcpParameters.duplicateServer", {name: parameter.name, slug});
        appended.set(key, true);
        const collision = (servers.get(slug).tools || []).find((tool) => (
          Object.prototype.hasOwnProperty.call(tool.inputSchema?.properties || {}, parameter.name)
          || (tool.inputSchema?.required || []).includes(parameter.name)
        ));
        if (collision) return t("mcpParameters.existing", {name: parameter.name, slug, tool: collision.name});
      }
    }
  }
  if (new TextEncoder().encode(JSON.stringify(rules)).length > 256 * 1024) return t("mcpParameters.sizeLimit");
  return "";
}

function renderMcpParameterGroups(rules, mcp) {
  const servers = mcpParameterServers(mcp);
  if (!rules.length) return `<p class="muted">${t("mcpParameters.empty")}</p>`;
  return rules.map((rule, groupIndex) => `<fieldset class="mcp-parameter-group" data-mcp-parameter-group="${groupIndex}">
    <legend>${t("mcpParameters.group", {count: groupIndex + 1})}</legend>
    <div class="mcp-parameter-toolbar"><strong data-mcp-parameter-count>${t("mcpParameters.selected", {selected: rule.server_slugs.length, total: servers.length})}</strong>
      <button type="button" class="secondary compact" data-mcp-parameter-action="select-all">${t("mcpParameters.selectAll")}</button>
      <button type="button" class="secondary compact" data-mcp-parameter-action="clear">${t("mcpParameters.clear")}</button>
      <button type="button" class="secondary compact" data-mcp-parameter-action="remove-group">${t("mcpParameters.removeGroup")}</button></div>
    <div class="mcp-parameter-servers">${servers.length ? servers.map((server) => `<label class="mcp-parameter-server"><input type="checkbox" data-mcp-parameter-server="${escapeHtml(server.slug)}" ${rule.server_slugs.includes(server.slug) ? "checked" : ""}><span><strong>${escapeHtml(server.name || server.slug)}</strong><small>${escapeHtml([server.functional_group, server.slug].filter(Boolean).join(" · "))}</small></span></label>`).join("") : `<p class="muted">${t("mcpParameters.noMcp")}</p>`}</div>
    <div class="mcp-parameter-rows">${rule.parameters.map((parameter, parameterIndex) => `<div class="mcp-parameter-row" data-mcp-parameter-row="${parameterIndex}">
      <label>${t("mcpParameters.name")}<input type="text" data-mcp-parameter-field="name" aria-label="${escapeHtml(t("mcpParameters.name"))}" value="${escapeHtml(parameter.name)}" maxlength="128" pattern="[A-Za-z_][A-Za-z0-9_]*" required placeholder="api_key"></label>
      <label>${t("mcpParameters.type")}<select data-mcp-parameter-field="type" aria-label="${escapeHtml(t("mcpParameters.type"))}">${["string", "integer", "number", "boolean"].map((type) => `<option value="${type}" ${type === (parameter.type || "string") ? "selected" : ""}>${type}</option>`).join("")}</select></label>
      <label class="mcp-parameter-description">${t("mcpParameters.description")}<textarea data-mcp-parameter-field="description" aria-label="${escapeHtml(t("mcpParameters.description"))}" maxlength="10000" required>${escapeHtml(parameter.description)}</textarea></label>
      <button type="button" class="secondary compact" data-mcp-parameter-action="remove-parameter">${t("mcpParameters.removeParameter")}</button>
    </div>`).join("")}</div>
    <button type="button" class="secondary compact" data-mcp-parameter-action="add-parameter" ${rule.parameters.length >= 32 ? "disabled" : ""}>${t("mcpParameters.addParameter")}</button>
  </fieldset>`).join("");
}

function bindMcpParameterAdditionsEditor(container, initialMcp, initialRules = [], onChange = () => {}) {
  if (!container) return null;
  let mcp = initialMcp;
  let rules = normalizedMcpParameterRules(initialRules);
  let resourceNotice = "";
  let disabled = false;
  const groupBox = container.querySelector("[data-mcp-parameter-groups]");
  const status = container.querySelector("[data-mcp-parameter-status]");
  const value = () => {
    const normalized = normalizedMcpParameterRules(rules);
    const error = mcpParameterRulesError(normalized, mcp);
    if (error) throw new Error(error);
    return normalized;
  };
  const refreshStatus = () => {
    const error = mcpParameterRulesError(normalizedMcpParameterRules(rules), mcp);
    status.textContent = [resourceNotice, error].filter(Boolean).join(" ");
    status.classList.toggle("has-error", Boolean(error));
    const validityControl = container.querySelector('[data-mcp-parameter-field="name"]');
    validityControl?.setCustomValidity(error);
    container.querySelectorAll("[data-mcp-parameter-group]").forEach((group) => {
      const rule = rules[Number(group.dataset.mcpParameterGroup)];
      group.querySelector("[data-mcp-parameter-count]").textContent = t("mcpParameters.selected", {
        selected: rule.server_slugs.length, total: mcpParameterServers(mcp).length,
      });
    });
    container.querySelector('[data-mcp-parameter-action="add-group"]').disabled = disabled || rules.length >= 32;
  };
  const render = () => { groupBox.innerHTML = renderMcpParameterGroups(rules, mcp); refreshStatus(); };
  const changeField = (event) => {
    const group = event.target.closest("[data-mcp-parameter-group]");
    if (!group) return;
    const rule = rules[Number(group.dataset.mcpParameterGroup)];
    if (event.target.matches("[data-mcp-parameter-server]")) {
      const slug = event.target.dataset.mcpParameterServer;
      rule.server_slugs = rule.server_slugs.filter((value) => value !== slug);
      if (event.target.checked) rule.server_slugs.push(slug);
    } else if (event.target.matches("[data-mcp-parameter-field]")) {
      const row = event.target.closest("[data-mcp-parameter-row]");
      rule.parameters[Number(row.dataset.mcpParameterRow)][event.target.dataset.mcpParameterField] = event.target.value;
    } else return;
    resourceNotice = "";
    refreshStatus();
    onChange();
  };
  container.addEventListener("input", changeField);
  container.addEventListener("change", changeField);
  container.addEventListener("click", (event) => {
    const button = event.target.closest("[data-mcp-parameter-action]");
    if (!button || button.disabled) return;
    const group = button.closest("[data-mcp-parameter-group]");
    const groupIndex = group ? Number(group.dataset.mcpParameterGroup) : -1;
    const rule = rules[groupIndex];
    const newParameter = () => ({name: "", description: "", type: "string"});
    switch (button.dataset.mcpParameterAction) {
      case "add-group": if (rules.length < 32) rules.push({server_slugs: [], parameters: [newParameter()]}); break;
      case "remove-group": rules.splice(groupIndex, 1); break;
      case "select-all": rule.server_slugs = mcpParameterServers(mcp).map((server) => server.slug); break;
      case "clear": rule.server_slugs = []; break;
      case "add-parameter": if (rule.parameters.length < 32) rule.parameters.push(newParameter()); break;
      case "remove-parameter": rule.parameters.splice(Number(button.closest("[data-mcp-parameter-row]").dataset.mcpParameterRow), 1); break;
      default: return;
    }
    resourceNotice = "";
    render();
    onChange();
  });
  render();
  return {
    value,
    draft: () => normalizedMcpParameterRules(rules),
    setDisabled(value) {
      disabled = Boolean(value);
      container.querySelectorAll("input, select, textarea, button").forEach((control) => { control.disabled = disabled; });
      if (!disabled) {
        container.querySelectorAll("[data-mcp-parameter-group]").forEach((group) => {
          group.querySelector('[data-mcp-parameter-action="add-parameter"]').disabled = rules[Number(group.dataset.mcpParameterGroup)].parameters.length >= 32;
        });
      }
      container.querySelector('[data-mcp-parameter-action="add-group"]').disabled = disabled || rules.length >= 32;
    },
    setMcp(nextMcp) {
      mcp = nextMcp;
      rules.forEach((rule) => { rule.server_slugs = []; });
      resourceNotice = rules.length ? t("mcpParameters.resourceChanged") : "";
      render();
    },
  };
}

function buildValidationConfig(form, base) {
  const specified = usesSpecifiedMemory(form);
  const source = specified ? null : form.memorySourceState.selected;
  const sourceExperimentId = fieldValue(form, "validation.source_experiment_id");
  const agentId = fieldValue(form, "validation.source_agent_id");
  const targetMode = fieldValue(form, "validation.target_mode") || "none";
  if (!specified) {
    if (!sourceExperimentId) throw new Error(t("validation.sourceExperimentRequired"));
    if (!source || source.usable === false || source.source_round === undefined) throw new Error(t("validation.sourceRequired"));
    if (source.experiment_id !== sourceExperimentId) throw new Error(t("validation.sourceRequired"));
    if (!(source.agents || []).some((agent) => agent.agent_id === agentId)) throw new Error(t("validation.agentRequired"));
  }
  const selectedFiles = validationFilesForSave(form, targetMode);
  if (!selectedFiles.queries) throw new Error(t("validation.queryRequired"));
  if (!selectedFiles.mcp) throw new Error(t("validation.mcpRequired"));
  if (targetMode !== "none" && !validationTargetAnalysis(selectedFiles).complete) {
    throw new Error(t("validation.targetsRequired"));
  }
  const files = targetMode === "none"
    ? withoutValidationTargets(selectedFiles)
    : selectedFiles;
  const queryRevisionId = files.queries?.resource_revision_id;
  const mcpRevisionId = files.mcp?.resource_revision_id;
  if (!queryRevisionId || !mcpRevisionId) {
    throw new Error(t("resource.validationRequired"));
  }
  const memory = specified ? null : validationMemorySettings(form, source);
  const draft = specified ? collectSpecifiedMemory(form) : {};
  const groups = specifiedMemoryGroups(selectedFiles);
  const missing = specified && groups.find(({key}) => typeof draft[key] !== "string");
  if (missing) throw new Error(t("validation.specifiedMemoryRequired", {group: missing.key}));
  const specifiedAgentId = fieldValue(form, "validation.specified_agent_id") || "ROWAN";
  const injection = form.elements["validation.injection_enabled"]
    ? checked(form, "validation.injection_enabled") : !specified;
  return {
    version: 1,
    run: {name: experimentNameForSave(form), id: null, seed: isToolSelection(base) ? (base.run?.seed ?? 20260828) : 20260828},
    environment: {type: "tool_selection"},
    validation: {
      memory_source_mode: specified ? "specified" : "run",
      ...(specified ? {
        specified_agent_id: specifiedAgentId,
        specified_memory: Object.fromEntries(groups.map(({key}) => [key, draft[key]])),
      } : {
      source_experiment_id: sourceExperimentId,
      source_run_id: source.run_id,
      source_agent_id: agentId,
      source_round: source.source_round,
      memory_account_pool: validationMemoryAccountPool(form, base),
      }),
      queries: {resource_revision_id: queryRevisionId},
      mcp: {resource_revision_id: mcpRevisionId},
      mcp_parameter_additions: form.mcpParameterAdditionsEditor?.value()
        || clone(base.validation?.mcp_parameter_additions || []),
      target_mode: targetMode,
      ...(targetMode === "mapping" ? {target_mapping_id: form.validationGroupDefaults?.target_mapping?.mapping_id} : {}),
      ...(targetMode === "custom"
        ? {target_overrides: validationTargetOverrides(files)}
        : {}),
      backend_connection: validationBackendConnection(form),
      injection_enabled: injection,
      api_key_position: fieldValue(form, "validation.api_key_position") || "middle",
      custom_payload_enabled: injection && checked(form, "validation.custom_payload_enabled"),
      custom_payload: form.elements["validation.custom_payload"]?.value ?? "",
      ...(injection && checked(form, "validation.custom_payload_enabled") ? {
        expected_leak_value: form.elements["validation.expected_leak_value"]?.value ?? "",
        leak_argument_path: fieldValue(form, "validation.leak_argument_path"),
      } : {}),
    },
    backend: {model: fieldValue(form, "validation.model")},
    ...(memory ? {memory} : {}),
    output: {root_dir: form.dataset?.existingExperiment === "true"
      ? get(base, "output.root_dir", configuredOutputRoot(fieldValue(form, "experiment_id")))
      : configuredOutputRoot(fieldValue(form, "experiment_id"))},
  };
}

function validationConnectionLabel(config) {
  const connection = config.validation?.backend_connection || {mode: "inherit"};
  if (connection.mode === "environment") {
    return `${t("validation.connectionEnvironment")} · ${connection.api_base_env || "AGENT_COMMUNITY_API_BASE"}`;
  }
  return connection.mode === "custom"
    ? connection.api_base
    : `${t(config.validation?.memory_source_mode === "specified" ? "validation.specifiedConnectionInherit" : "validation.connectionInherit")} · ${config.backend?.api_base || "-"}`;
}

function memoryAccountPoolFields(config) {
  return `<fieldset class="wide memory-account-pool" id="memoryAccountPoolFields">
    <legend>${t("memoryWorkspace.accountPool")}</legend>
    <p class="muted" id="memoryAccountPoolHelp">${t("memoryWorkspace.poolHelp")}</p>
    <div id="memoryAccountPoolChoices"></div><p id="memoryAccountPoolStatus" role="status"></p>
    <button type="button" class="secondary compact" id="memoryAccountPoolRefresh">${t("memoryWorkspace.refreshReplicas")}</button>
    <a href="/memory-accounts" target="_blank" rel="noopener noreferrer">${t("memoryWorkspace.accounts")}</a>
  </fieldset>`;
}

function validationMemoryAccountPool(form, base) {
  const cloud = !usesSpecifiedMemory(form) && ["mem0_platform_v3", "hindsight_cloud", "memos_cloud"].includes(form.memorySourceState?.selected?.memory_strategy);
  return {enabled: cloud, ...(cloud ? {shared: true} : {}), account_ids: []};
}

function validationBackendConnection(form) {
  const mode = fieldValue(form, "validation.connection_mode") || "inherit";
  if (mode === "inherit") return {mode};
  return {
    mode,
    api_key_env: fieldValue(form, "validation.connection_api_key_env"),
    ...(mode === "environment"
      ? {api_base_env: fieldValue(form, "validation.connection_api_base_env")}
      : {api_base: fieldValue(form, "validation.connection_api_base")}),
  };
}

function validationMemorySettings(form, source) {
  const fields = validationRetrievalFields(source);
  if (!Object.keys(fields).length) return null;
  const options = {};
  Object.entries(fields).forEach(([key, field]) => {
    if (key === "retrieval_top_k" && source.memory_strategy === "hindsight_cloud") {
      const value = Number(fieldValue(form, `validation.${key}`));
      if (!Number.isInteger(value) || value < 1 || value > 1000) throw new Error(t("cloud.topKInvalid"));
      options[key] = value;
      return;
    }
    options[key] = ["rerank", "rewrite_query", "aggregate"].includes(key)
      ? checked(form, `validation.${key}`)
      : key === "recall_budget" ? fieldValue(form, `validation.${key}`)
      : ["search_threshold", "search_distance"].includes(key)
        ? numberValue(form, `validation.${key}`, field.default)
        : integerValue(form, `validation.${key}`, field.default);
  });
  return {
    strategy: source.memory_strategy,
    options,
  };
}

function validationRetrievalFields(source) {
  const strategy = source?.memory_strategy;
  if (strategy === "memos_cloud") return {retrieval_top_k: {default: 9, max: 25}, search_threshold: {default: 0.45}, context_max_chars: {default: 12000}};
  if (strategy === "hindsight_cloud") return {recall_budget: {default: "mid"}, recall_max_tokens: {default: 4096}, retrieval_top_k: {default: 10, max: 1000}, context_max_chars: {default: 12000}};
  const defaultTopK = {
    langmem_official: 10, mem0_platform_v3: 10, a_mem_official: 5,
  }[strategy];
  if (defaultTopK === undefined) return {};
  const fields = {retrieval_top_k: {default: defaultTopK}};
  if (["langmem_official", "a_mem_official", "mem0_platform_v3"].includes(strategy)) {
    fields.retrieval_top_k.max = 1000;
  }
  if (strategy === "mem0_platform_v3") {
    fields.search_threshold = {default: 0.1};
  }
  if (strategy === "mem0_platform_v3") fields.rerank = {default: false};
  return fields;
}

function resetValidationRetrievalSettings(form, source) {
  const imported = form.validationImportedMemorySettings;
  const useImported = source && imported
    && imported.strategy === source.memory_strategy;
  const normalizeOptions = (settings) => ({
    ...settings,
    ...(settings?.retrieval_top_k === undefined && settings?.top_k !== undefined
      ? {retrieval_top_k: settings.top_k} : {}),
  });
  const options = {
    ...normalizeOptions(source?.memory_options), ...normalizeOptions(source?.retrieval_settings),
    ...(form.dataset?.existingExperiment === "false"
      ? normalizeOptions(platformSettingsCache?.validation?.retrieval_profiles?.[source?.memory_strategy]) : {}),
    ...(useImported ? normalizeOptions(imported.options) : {}),
  };
  const fields = validationRetrievalFields(source);
  Object.entries(fields).forEach(([key, field]) => {
    const input = form.elements[`validation.${key}`];
    if (!input) return;
    const value = options[key] ?? (key === "retrieval_top_k"
      ? options.top_k ?? source?.retrieval_top_k : undefined) ?? field.default;
    if (["rerank", "rewrite_query", "aggregate"].includes(key)) input.checked = Boolean(value);
    else input.value = value == null ? "" : String(value);
  });
  form.validationRetrievalSourceKey = `${memorySourceKey(source)}:${source?.memory_strategy || ""}`;
  if (useImported) form.validationImportedMemorySettings = null;
}

function syncValidationRetrievalSettings(form, source) {
  const sourceKey = `${memorySourceKey(source)}:${source?.memory_strategy || ""}`;
  if (form.validationRetrievalSourceKey !== sourceKey) resetValidationRetrievalSettings(form, source);
  const fields = validationRetrievalFields(source);
  Object.entries({
    retrieval_top_k: "top-k", search_threshold: "threshold", rerank: "rerank",
    recall_budget: "budget", recall_max_tokens: "tokens", search_distance: "distance", rewrite_query: "rewrite", aggregate: "aggregate",
    merged_max_items: "merged", context_max_chars: "chars",
  }).forEach(([key, condition]) => {
    setConfigCondition(form, `validation-retrieval-${condition}`, Boolean(fields[key]));
  });
  const topKLabel = form.querySelector?.('label[for="validation.retrieval_top_k"]');
  const topKHelp = form.querySelector?.('[data-config-when="validation-retrieval-top-k"] small');
  if (topKLabel) topKLabel.textContent = t(source?.memory_strategy === "hindsight_cloud" ? "cloud.topK" : "editor.retrievalTopK");
  if (topKHelp) topKHelp.textContent = t(source?.memory_strategy === "hindsight_cloud" ? "cloud.topKHelp" : "validation.retrievalSettingsHelp");
  const topK = form.elements["validation.retrieval_top_k"];
  if (topK) {
    if (fields.retrieval_top_k?.max) topK.max = String(fields.retrieval_top_k.max);
    else topK.removeAttribute("max");
  }
}

function experimentNameForSave(form) {
  return fieldValue(form, "platform.name") || fieldValue(form, "experiment_id");
}

function buildConfig(form, base) {
  if (fieldValue(form, "experiment.stage") === "tool_selection") return buildValidationConfig(form, base);
  const environmentType = fieldValue(form, "environment.type");
  if (!form.importedPostFile) throw new Error(t("editor.fileRequired"));
  const readerId = fieldValue(form, "simulation.read_only_agent_id");
  if (!readerId) throw new Error(t("editor.readOnlyAgentRequired"));
  const retries = integerValue(form, "backend.max_transient_retries", 8);
  const backend = {
    api_base_env: fieldValue(form, "backend.api_base_env"),
    api_key_env: fieldValue(form, "backend.api_key_env"),
    mem0_api_key_env: fieldValue(form, "backend.mem0_api_key_env"),
    model_env: fieldValue(form, "backend.model_env"),
    concurrency: integerValue(form, "backend.concurrency", 1),
    timeout_seconds: numberValue(form, "backend.timeout_seconds", 90),
    temperature: numberValue(form, "backend.temperature", 0.2),
    max_tokens: integerValue(form, "backend.max_tokens", 8000),
    max_transient_retries: retries,
    min_request_interval_seconds: numberValue(form, "backend.min_request_interval_seconds", 0),
    ...(retries > 0 ? {
      retry_backoff_seconds: numberValue(form, "backend.retry_backoff_seconds", 2),
      max_retry_wait_seconds: numberValue(form, "backend.max_retry_wait_seconds", 60),
    } : {}),
  };
  return {
    version: 1,
    run: {name: experimentNameForSave(form), id: null, seed: 0},
    backend,
    data: {},
    environment: {
      type: environmentType,
      [environmentType === "moltnet_thread" ? "thread" : "forum"]: {selection: "file"},
      file: {
        resource_revision_id: form.importedPostFile.resource_revision_id,
        ...(Array.isArray(form.selectedPostIds) ? {selected_post_ids: [...form.selectedPostIds]} : {}),
      },
      hide_speaker: checked(form, "environment.hide_speaker"),
      speaker_as_json: !checked(form, "environment.hide_speaker") && checked(form, "environment.speaker_as_json"),
    },
    agents: {
      count: 1, seed: 0, prefer_thread_participants: false,
      require_identity: checked(form, "agents.require_identity"),
      history_limit: integerValue(form, "agents.history_limit", 100),
    },
    memory: readMemoryConfiguration(form),
    simulation: {
      mode: "read_only", rounds: 1, read_only_agent_id: readerId,
      activation: {mode: "all"}, round_interval_seconds: 0,
      continue_on_agent_error: false, max_consecutive_all_failed_rounds: null, tools: [],
    },
    output: {
      root_dir: fieldValue(form, "output.root_dir"),
      save_thread_after_each_round: checked(form, "output.save_thread_after_each_round"),
      save_memory_after_each_round: checked(form, "output.save_memory_after_each_round"),
    },
  };
}

function syncConfigEditor(form) {
  form.elements["platform.name"].placeholder = fieldValue(form, "experiment_id");
  const stage = fieldValue(form, "experiment.stage") || "interaction";
  syncValidationMemoryLabels(form, stage === "tool_selection" && usesSpecifiedMemory(form));
  form.querySelectorAll("[data-experiment-stage]").forEach((container) => {
    const enabled = container.dataset.experimentStage === stage;
    container.hidden = !enabled;
    container.querySelectorAll("input, select, textarea").forEach((control) => {
      control.disabled = !enabled;
    });
  });
  form.querySelector('[data-config-advanced-heading]').hidden = stage === "tool_selection";
  form.elements["experiment.stage"].disabled = isConfigEditorBusy(form)
    || form.dataset.existingExperiment === "true";
  form.elements.experiment_group_id.disabled = isConfigEditorBusy(form)
    || form.dataset.existingExperiment === "true";
  form.querySelector('button[type="submit"]').disabled = isConfigEditorBusy(form);
  form.querySelector("#validateConfig").disabled = isConfigEditorBusy(form);
  if (stage === "tool_selection") {
    syncValidationEditor(form);
    updateConfigReadiness(form);
    return;
  }
  const environmentType = fieldValue(form, "environment.type");
  const isThread = environmentType === "moltnet_thread";
  const resourceInput = form.querySelector("#postResourceRevision");
  resourceInput.disabled = isConfigEditorBusy(form);
  setConfigCondition(form, "transient-retries", integerValue(form, "backend.max_transient_retries", 0) > 0);
  toggleMemoryOptions(form);
  toggleSpeakerFormat(form);
  form.elements["memory.strategy"].setCustomValidity(firstStageMemoryChoices().includes(fieldValue(form, "memory.strategy")) ? "" : t("editor.memoryStrategyRequired"));
  const fileData = form.importedPostFile?.dataset;
  const filePosts = Array.isArray(form.selectedPostIds)
    ? (fileData?.posts || []).filter((post) => form.selectedPostIds.includes(post.id))
    : (fileData?.posts || []);
  const fileAgents = fileData?.agents || [];
  const eligibleAgents = checked(form, "agents.require_identity")
    ? fileAgents.filter((agent) => !["", "-", "(none)", "n/a", "na", "none", "null", "unknown"].includes(String(agent.persona || "").trim().toLowerCase()))
    : fileAgents;
  const reader = form.elements["simulation.read_only_agent_id"];
  const requestedReaderId = reader.value.trim();
  const readerId = fileAgents.some((agent) => agent.id === requestedReaderId)
    ? requestedReaderId : requestedReaderId.replace(/^file:/, "");
  reader.required = true;
  reader.setCustomValidity(!readerId ? t("editor.readOnlyAgentRequired")
    : fileData && !eligibleAgents.some((agent) => agent.id === readerId) ? t("editor.readOnlyFileAgentInvalid") : "");
  form.querySelector("#readOnlyAgentOptions").innerHTML = eligibleAgents.map((agent) => (
    `<option value="${escapeHtml(agent.id)}">${escapeHtml(agent.name || agent.id)}</option>`
  )).join("");
  const fileIssue = !fileData ? t("editor.fileRequired")
    : form.importedPostFile?.environment_type !== environmentType ? t("resource.postEnvironmentMismatch")
    : isThread && filePosts.length !== 1 ? t("editor.fileSinglePost")
    : !filePosts.length ? t("editor.fileRequired") : "";
  resourceInput.setCustomValidity(fileIssue);
  form.querySelector("#postFileSummary").textContent = fileData ? t("editor.fileSummary", {
    filename: form.importedPostFile.resource_name || form.importedPostFile.filename, posts: filePosts.length,
    comments: filePosts.reduce((sum, post) => sum + (post.comments || []).length, 0),
    agents: fileAgents.length, eligible: eligibleAgents.length,
  }) : "";
  updateConfigSummary(form);
  updateConfigReadiness(form);
}

function setConfigCondition(form, condition, enabled) {
  form.querySelectorAll(`[data-config-when="${condition}"]`).forEach((container) => {
    container.hidden = !enabled;
    container.querySelectorAll("input, select, textarea").forEach((control) => {
      control.disabled = !enabled;
    });
  });
}

function updateConfigSummary(form) {
  const posts = form.importedPostFile?.dataset?.posts || [];
  const count = Array.isArray(form.selectedPostIds) ? posts.filter((post) => form.selectedPostIds.includes(post.id)).length : posts.length;
  document.querySelector("#configSummary").innerHTML = `
    ${summaryItem(t("summary.runner"), fieldValue(form, "environment.type"))}
    ${summaryItem(t("summary.post"), t("editor.filePostCount", {count}))}
    ${summaryItem(t("summary.scale"), t("summary.readOnlyScale"))}
    ${summaryItem(t("editor.readOnlyAgent"), fieldValue(form, "simulation.read_only_agent_id") || "-")}
    ${summaryItem(t("summary.memory"), memoryStrategyLabel(fieldValue(form, "memory.strategy")))}
    ${MEMORY_MODEL_STRATEGIES.has(fieldValue(form, "memory.strategy")) ? summaryItem(t("editor.memoryModel"), fieldValue(form, "memory.model")) : ""}
    ${summaryItem(t("summary.concurrency"), fieldValue(form, "backend.concurrency"))}
    ${summaryItem(t("summary.export"), `exp/${fieldValue(form, "experiment_id") || "<id>"}/config.yaml`)}
  `;
}

function updateConfigReadiness(form) {
  const readiness = document.querySelector("#configReadiness");
  const invalid = [...form.elements].filter((control) => control.willValidate && !control.validity.valid);
  const ready = invalid.length === 0;
  readiness.classList.toggle("is-ready", ready);
  readiness.classList.toggle("has-errors", !ready);
  readiness.textContent = ready
    ? t("editor.ready")
    : t("editor.issueCount", {count: invalid.length});
}

function speakerVisibilityField(config) {
  const hidden = get(config, "environment.hide_speaker", true);
  return `<div class="form-field wide">
    <div class="checkbox-field">
      <input name="environment.hide_speaker" id="environment.hide_speaker" type="checkbox" ${get(config, "environment.hide_speaker", true) ? "checked" : ""} aria-describedby="hideSpeakerHelp" />
      <label for="environment.hide_speaker">${t("editor.hideSpeaker")}</label>
    </div>
    <small id="hideSpeakerHelp">${t("editor.hideSpeakerHelp")}</small>
  </div>
  <div class="form-field wide" data-speaker-format ${hidden ? "hidden" : ""}>
    <div class="checkbox-field">
      <input name="environment.speaker_as_json" id="environment.speaker_as_json" type="checkbox" ${get(config, "environment.speaker_as_json", false) ? "checked" : ""} ${hidden ? "disabled" : ""} aria-describedby="speakerJsonHelp" />
      <label for="environment.speaker_as_json">${t("editor.speakerAsJson")}</label>
    </div>
    <small id="speakerJsonHelp">${escapeHtml(t("editor.speakerAsJsonHelp"))}</small>
  </div>`;
}

function toggleSpeakerFormat(form) {
  const hidden = checked(form, "environment.hide_speaker");
  form.querySelectorAll("[data-speaker-format]").forEach((field) => {
    field.hidden = hidden;
    field.querySelectorAll("input").forEach((input) => { input.disabled = hidden; });
  });
}

function selectableMemoryStrategies() {
  return ["langmem_official", "mem0_platform_v3", "a_mem_official", "hindsight_cloud", "memos_cloud"];
}

function firstStageMemoryChoices() {
  return [...selectableMemoryStrategies(), "agent_memory_preview"];
}

function memoryEditorFields(config) {
  const strategy = get(config, "memory.strategy", "langmem_official");
  const strategies = firstStageMemoryChoices();
  return `<div class="form-grid">
        <div class="form-field">
          <label for="memory.strategy">${t("editor.memoryStrategy")}</label>
          <select id="memory.strategy" name="memory.strategy">
            ${strategies.map((key) => `<option value="${key}" ${key === strategy ? "selected" : ""}>${escapeHtml(memoryStrategyLabel(key))}</option>`).join("")}
          </select>
        </div>
        ${experimentModelField("memory.model", selectedMemoryModel(config), {
          label: t("editor.memoryModel"), help: t(FIXED_LOCAL_MEMORY_STRATEGIES.has(strategy) ? "editor.memoryModelFixedHelp" : "editor.memoryModelHelp"),
          strategies: [...MEMORY_MODEL_STRATEGIES].join(" "),
          models: FIXED_LOCAL_MEMORY_STRATEGIES.has(strategy) ? [FIXED_LOCAL_MEMORY_MODEL] : EXPERIMENT_MODELS,
        })}
        <p class="validation-notice wide" data-memory-strategies="agent_memory_preview">${t("memoryPreview.help")}</p>
        ${memoryNumberField("memory.amem_evo_threshold", t("editor.amemEvoThreshold"), get(config, "memory.options.evo_threshold", 100), "a_mem_official", 1, 1, 10000)}
        ${memoryCheckboxField("memory.amem_response_tolerance_enabled", t("editor.amemResponseTolerance"), get(config, "memory.options.response_tolerance_enabled", true), "a_mem_official")}
        ${memoryNumberField("memory.amem_response_validation_max_attempts", t("editor.amemResponseAttempts"), get(config, "memory.options.response_validation_max_attempts", 3), "a_mem_official", 1, 1, 100)}
        <p class="validation-notice wide" data-memory-strategies="a_mem_official">${t("editor.amemOfficialNotice")}</p>
        ${memoryNumberField("memory.langmem_query_limit", t("editor.langmemQueryLimit"), get(config, "memory.options.query_limit", 5), "langmem_official", 4, 1, 1000)}
        ${memoryNumberField("memory.langmem_max_steps", t("editor.langmemMaxSteps"), get(config, "memory.options.max_steps", 1), "langmem_official", 1, 1, 10)}
        ${memoryTextField("memory.langmem_embedding_model", t("editor.embeddingModel"), get(config, "memory.options.embedding_model", "all-MiniLM-L6-v2"), "langmem_official")}
        ${memoryNumberField("memory.langmem_embedding_dimensions", t("editor.embeddingDimensions"), get(config, "memory.options.embedding_dimensions", 384), "langmem_official", 1, 1, 65536)}
        ${memoryCheckboxField("memory.langmem_enable_deletes", t("editor.langmemEnableDeletes"), get(config, "memory.options.enable_deletes", false), "langmem_official")}
        ${memoryTextField("memory.langmem_custom_instructions", t("editor.memoryCustomInstructions"), get(config, "memory.options.custom_instructions", ""), "langmem_official")}
        <p class="validation-notice wide">${t("editor.retrievalInValidationNotice")}</p>
        <div class="form-field wide" data-memory-strategies="hindsight_cloud">
          <label for="memory.hindsight_target_bank">${t("cloud.targetBank")}</label>
          <input id="memory.hindsight_target_bank" name="memory.hindsight_target_bank" type="text" value="${escapeHtml(get(config, "memory.options.target_bank", ""))}" placeholder="${t("cloud.targetBankPlaceholder")}" />
        </div>
        <p class="validation-notice wide" data-memory-strategies="hindsight_cloud">${t("cloud.targetBankNotice")}</p>
        ${memoryNumberField("memory.cloud_request_timeout", t("cloud.requestTimeout"), get(config, "memory.options.request_timeout_seconds", 30), "hindsight_cloud memos_cloud", 0.001, "any")}
        ${memoryNumberField("memory.cloud_processing_timeout", t("cloud.processingTimeout"), get(config, "memory.options.processing_timeout_seconds", 600), "hindsight_cloud memos_cloud", 0.001, "any")}
        ${memoryNumberField("memory.cloud_poll_interval", t("cloud.pollInterval"), get(config, "memory.options.poll_interval_seconds", 2), "hindsight_cloud memos_cloud", 0.001, "any")}
        ${memoryNumberField("memory.cloud_read_retries", t("cloud.readRetries"), get(config, "memory.options.max_read_retries", 3), "hindsight_cloud memos_cloud", 0, 1, 10)}
        <p class="validation-notice wide" data-memory-strategies="hindsight_cloud memos_cloud">${t("cloud.notice")}</p>
        ${memoryNumberField("memory.mem0_request_timeout", t("editor.mem0RequestTimeout"), get(config, "memory.options.request_timeout_seconds", 30), "mem0_platform_v3", 0.001, "any")}
        ${memoryNumberField("memory.mem0_event_timeout", t("editor.mem0EventTimeout"), get(config, "memory.options.event_timeout_seconds", 300), "mem0_platform_v3", 0.001, "any")}
        ${memoryNumberField("memory.mem0_poll_interval", t("editor.mem0PollInterval"), get(config, "memory.options.event_poll_interval_seconds", 1), "mem0_platform_v3", 0.001, "any")}
        ${memoryNumberField("memory.mem0_transient_retries", t("editor.mem0TransientRetries"), get(config, "memory.options.max_transient_retries", 3), "mem0_platform_v3", 0)}
        ${memoryNumberField("memory.mem0_retry_backoff", t("editor.mem0RetryBackoff"), get(config, "memory.options.retry_backoff_seconds", 1), "mem0_platform_v3", 0.001, "any")}
        ${memoryNumberField("memory.mem0_max_retry_wait", t("editor.mem0MaxRetryWait"), get(config, "memory.options.max_retry_wait_seconds", 30), "mem0_platform_v3", 0.001, "any")}
        ${memoryTextField("memory.platform_custom_instructions", t("editor.memoryCustomInstructions"), get(config, "memory.options.custom_instructions", ""), "mem0_platform_v3")}
        <p class="validation-notice wide" data-memory-strategies="langmem_official">${t("editor.langmemOfficialNotice")}</p>
        <p class="validation-notice wide" data-memory-strategies="mem0_platform_v3">${t("editor.mem0PlatformNotice")}</p>
      </div>`;
}

function readMemoryConfiguration(form) {
  const strategy = fieldValue(form, "memory.strategy");
  if (["hindsight_cloud", "memos_cloud"].includes(strategy)) {
    return {strategy, options: {
      ...(strategy === "hindsight_cloud" ? {target_bank: fieldValue(form, "memory.hindsight_target_bank")} : {}),
      request_timeout_seconds: numberValue(form, "memory.cloud_request_timeout", 30),
      processing_timeout_seconds: numberValue(form, "memory.cloud_processing_timeout", 600),
      poll_interval_seconds: numberValue(form, "memory.cloud_poll_interval", 2),
      max_read_retries: integerValue(form, "memory.cloud_read_retries", 3),
    }};
  }
  const memoryOptions = {
    langmem_official: {
      query_limit: integerValue(form, "memory.langmem_query_limit", 5),
      max_steps: integerValue(form, "memory.langmem_max_steps", 1),
      embedding_model: fieldValue(form, "memory.langmem_embedding_model"),
      embedding_dimensions: integerValue(form, "memory.langmem_embedding_dimensions", 384),
      enable_deletes: checked(form, "memory.langmem_enable_deletes"),
      custom_instructions: fieldValue(form, "memory.langmem_custom_instructions"),
    },
    mem0_platform_v3: {
      request_timeout_seconds: numberValue(form, "memory.mem0_request_timeout", 30),
      event_timeout_seconds: numberValue(form, "memory.mem0_event_timeout", 300),
      event_poll_interval_seconds: numberValue(form, "memory.mem0_poll_interval", 1),
      max_transient_retries: integerValue(form, "memory.mem0_transient_retries", 3),
      retry_backoff_seconds: numberValue(form, "memory.mem0_retry_backoff", 1),
      max_retry_wait_seconds: numberValue(form, "memory.mem0_max_retry_wait", 30),
      custom_instructions: fieldValue(form, "memory.platform_custom_instructions"),
    },
    a_mem_official: {
      evo_threshold: integerValue(form, "memory.amem_evo_threshold", 100),
      response_tolerance_enabled: checked(form, "memory.amem_response_tolerance_enabled"),
      response_validation_max_attempts: integerValue(form, "memory.amem_response_validation_max_attempts", 3),
    },
  };
  return {
    strategy,
    ...(MEMORY_MODEL_STRATEGIES.has(strategy)
      ? {model: FIXED_LOCAL_MEMORY_STRATEGIES.has(strategy) ? FIXED_LOCAL_MEMORY_MODEL : fieldValue(form, "memory.model")}
      : {}),
    options: memoryOptions[strategy] || {},
  };
}

function toggleMemoryOptions(form) {
  const strategy = fieldValue(form, "memory.strategy");
  form.querySelectorAll("[data-memory-strategies]").forEach((field) => {
    const enabled = field.dataset.memoryStrategies.split(" ").includes(strategy);
    field.hidden = !enabled;
    field.querySelectorAll("input, select, textarea").forEach((control) => {
      control.disabled = !enabled;
    });
  });
  const model = form.elements["memory.model"];
  if (model?.tagName === "SELECT" && MEMORY_MODEL_STRATEGIES.has(strategy)) {
    const fixed = FIXED_LOCAL_MEMORY_STRATEGIES.has(strategy);
    const models = fixed ? [FIXED_LOCAL_MEMORY_MODEL] : EXPERIMENT_MODELS;
    const selected = fixed ? FIXED_LOCAL_MEMORY_MODEL : models.includes(model.value) ? model.value : models[0];
    model.innerHTML = models.map((value) => `<option value="${value}">${value}</option>`).join("");
    model.value = selected;
    const help = form.querySelector?.('[id="memory.model.help"]');
    if (help) help.textContent = t(fixed ? "editor.memoryModelFixedHelp" : "editor.memoryModelHelp");
  }
}

function memoryStrategyLabel(strategy) {
  return ({
    langmem_official: t("memory.langmemOfficial"),
    mem0_platform_v3: t("memory.mem0PlatformV3"),
    memos_cloud: t("memory.memosCloud"),
    specified_memory: t("validation.memorySourceSpecified"),
    hindsight_cloud: t("memory.hindsightCloud"),
    a_mem_official: t("memory.aMemOfficial"),
    agent_memory_preview: t("memory.agentMemoryPreview"),
  })[strategy] || strategy;
}

function isSupportedReadingExperiment(value) {
  const config = value?.config || (value?.environment ? value : null);
  if (!config) return isSupportedExperimentSummary(value, ["moltnet_thread", "moltnet_forum"]);
  return config?.simulation?.mode === "read_only"
    && Boolean(config?.environment?.file?.resource_revision_id)
    && ["moltnet_thread", "moltnet_forum"].includes(config?.environment?.type)
    && firstStageMemoryChoices().includes(config?.memory?.strategy)
    && (!value?.display_config?.memory_strategy
      || firstStageMemoryChoices().includes(value.display_config.memory_strategy));
}

function isSupportedExperimentSummary(experiment, runnerTypes) {
  // Lists omit full configurations. Details and the launch API validate the
  // file revision and read-only mode when a candidate is actually selected.
  const types = [experiment?.runner_type, experiment?.display_config?.environment_type].filter(Boolean);
  const strategy = experiment?.display_config?.memory_strategy || experiment?.memory_strategy;
  return types.length > 0 && types.every((type) => runnerTypes.includes(type))
    && (!strategy || (types.includes("tool_selection") ? [...selectableMemoryStrategies(), "specified_memory"] : firstStageMemoryChoices()).includes(strategy));
}

function canLaunchExperiment(experiment) {
  const config = experiment?.config;
  if (!config) return isSupportedExperimentSummary(experiment, ["moltnet_thread", "moltnet_forum", "tool_selection"]);
  if (!isToolSelection(experiment)) return isSupportedReadingExperiment(experiment);
  if (config.validation?.memory_source_mode === "specified") return true;
  const strategies = [config.memory?.strategy, experiment.display_config?.memory_strategy].filter(Boolean);
  return Boolean(config.validation && strategies.length
    && strategies.every((strategy) => selectableMemoryStrategies().includes(strategy)));
}

function isReadOnlyConfig(config) {
  return config?.simulation?.mode === "read_only";
}

function simulationModeLabel(mode) {
  return mode === "read_only" ? t("simulation.readOnly") : t("experiment.historicalMode");
}

async function renderRuns() {
  const query = new URLSearchParams(location.search);
  const groupId = query.get("group_id") || "";
  const groupPayload = await apiGet("/api/platform/experiment-groups?archived=all");
  const groups = normalizeExperimentGroups(groupPayload);
  const groupById = new Map(groups.map((group) => [experimentGroupId(group), group]));
  const url = new URL("/api/platform/runs", location.origin);
  if (groupId) url.searchParams.set("group_id", groupId);
  url.searchParams.set("limit", "100");
  const data = await apiGet(url.pathname + url.search);
  const attachGroup = (runs) => (runs || []).map((run) => {
    const runGroupId = experimentGroupId(run);
    const group = groupById.get(runGroupId);
    return {
      ...run,
      group_id: runGroupId,
      group_name: run.group_name || group?.name || runGroupId,
    };
  });
  const runs = attachGroup(data.runs);
  main.innerHTML = `
    <div class="page">
      ${pageHeader(t("runs.title"), "", "")}
      <div class="toolbar">
        <div class="field grow">
          <label for="runGroupFilter">${t("runs.groupFilterLabel")}</label>
          <select id="runGroupFilter">
            <option value="">${t("runs.allGroups")}</option>
            ${groups.map((group) => {
              const id = experimentGroupId(group);
              const suffix = group.legacy ? ` · ${t("group.legacy")}` : group.archived ? ` · ${t("group.archived")}` : "";
              return `<option value="${escapeHtml(id)}" ${id === groupId ? "selected" : ""}>${escapeHtml(group.name || id)}${escapeHtml(suffix)}</option>`;
            }).join("")}
          </select>
        </div>
        <button class="secondary" id="compareRuns" disabled>${t("runs.compareSelected", {count: 0})}</button>
        <button id="archiveRunExperiments" disabled>${t("runs.archiveSelectedExperiments", {count: 0})}</button>
        <button class="danger" id="deleteSelectedRuns" disabled>${t("runs.deleteSelected", {count: 0})}</button>
      </div>
      <div id="runList">${runsTable(runs, {selectable: true})}</div>
    </div>`;
  document.querySelector("#runGroupFilter").addEventListener("change", (event) => {
    location.href = event.target.value ? `/runs?group_id=${encodeURIComponent(event.target.value)}` : "/runs";
  });
  document.querySelector("#compareRuns").addEventListener("click", compareSelectedRuns);
  bindRunActions(main);
  bindRunComparison(document.querySelector("#runList"), document.querySelector("#compareRuns"));
  bindRunArchiveSelection(document.querySelector("#runList"), document.querySelector("#archiveRunExperiments"));
  bindRunDeleteSelection(document.querySelector("#runList"), document.querySelector("#deleteSelectedRuns"));
  startRunTablePolling(
    "#runList",
    runs,
    async (signal) => attachGroup((
      await apiGet(url.pathname + url.search, {signal})
    ).runs),
    {selectable: true},
  );
}

async function compareSelectedRuns() {
  const runIds = [...document.querySelectorAll('[name="compare_run"]:checked')].map((item) => item.value);
  if (runIds.length < 2) return showToast(t("runs.selectTwo"));
  const query = new URLSearchParams();
  runIds.forEach((runId) => query.append("run_id", runId));
  try {
    const data = await apiGet(`/api/platform/metrics/compare?${query}`);
    dialogTitle.innerHTML = `<h2>${t("runs.metricComparison")}</h2><small>${t("runs.selectedCount", {count: runIds.length})}</small>`;
    dialogBody.innerHTML = comparisonTable(data);
    detailDialog.showModal();
  } catch (error) {
    showToast(error.message);
  }
}

function startRunTablePolling(containerSelector, initialRuns, loadRuns, options = {}) {
  if (!hasActiveRuns(initialRuns)) return;
  let requestInFlight = false;
  pollTimer = window.setInterval(async () => {
    if (requestInFlight) return;
    requestInFlight = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const runs = await loadRuns(controller.signal);
      const container = document.querySelector(containerSelector);
      if (!container) {
        clearInterval(pollTimer);
        return;
      }
      const selected = new Set(
        [...container.querySelectorAll('[name="compare_run"]:checked')].map((item) => item.value),
      );
      container.innerHTML = runsTable(runs, options);
      container.querySelectorAll('[name="compare_run"]').forEach((item) => {
        item.checked = selected.has(item.value);
      });
      bindRunActions(container);
      bindRunComparison(container, document.querySelector("#compareRuns"));
      bindRunArchiveSelection(container, document.querySelector("#archiveRunExperiments"));
      bindRunDeleteSelection(container, document.querySelector("#deleteSelectedRuns"));
      if (!hasActiveRuns(runs)) clearInterval(pollTimer);
    } catch (_) {
      return;
    } finally {
      window.clearTimeout(timeout);
      requestInFlight = false;
    }
  }, 5000);
}

function hasActiveRuns(runs) {
  return (runs || []).some((run) => ["queued", "starting", "running"].includes(run.status));
}

function experimentLifecycleButtons(experiment, {includeLaunch = false, groupId = ""} = {}) {
  const experimentId = escapeHtml(experiment.experiment_id);
  if (experiment.archived) {
    const deleteCount = Number(experiment.delete_experiment_count || 1);
    const deleteRunCount = Number(experiment.delete_run_count ?? experiment.run_count ?? experiment.runs?.length ?? 0);
    const deleteLabel = deleteCount > 1
      ? t("experiment.deleteLinked", {count: formatNumber(deleteCount)})
      : t("experiment.delete");
    const deleteDisabled = experiment.can_delete ? "" : 'aria-disabled="true"';
    const deleteTitle = experiment.can_delete
      ? deleteLabel
      : experimentActionBlockMessage(experiment);
    return `
      <button class="secondary" type="button" data-restore-experiment="${experimentId}">${t("experiment.restore")}</button>
      <button class="danger" type="button" data-delete-experiment="${experimentId}" data-group-id="${escapeHtml(groupId)}" data-delete-count="${deleteCount}" data-run-count="${deleteRunCount}" data-block-message="${experiment.can_delete ? "" : escapeHtml(deleteTitle)}" title="${escapeHtml(deleteTitle)}" ${deleteDisabled}>${deleteLabel}</button>`;
  }
  const archiveDisabled = experiment.can_archive ? "" : 'aria-disabled="true"';
  const archiveTitle = experiment.can_archive
    ? t("experiment.archive")
    : t("experiment.activeRunsBlocked");
  return `
    <button class="secondary" type="button" data-archive-experiment="${experimentId}" data-block-message="${experiment.can_archive ? "" : escapeHtml(archiveTitle)}" title="${escapeHtml(archiveTitle)}" ${archiveDisabled}>${t("experiment.archive")}</button>
    ${groupId ? `<button class="danger" type="button" data-delete-experiment="${experimentId}" data-group-id="${escapeHtml(groupId)}" data-block-message="${escapeHtml(t("experiment.archiveBeforeDelete"))}" title="${escapeHtml(t("experiment.archiveBeforeDelete"))}" aria-disabled="true">${t("experiment.delete")}</button>` : ""}
    ${includeLaunch ? `<button id="launchRun">${t("experiment.launchRun")}</button>` : ""}`;
}

function experimentActionBlockMessage(experiment) {
  const blockingIds = [...new Set([
    ...(experiment.delete_blocking_active_ids || []),
    ...(experiment.delete_blocking_unarchived_ids || []),
    ...(experiment.delete_blocking_invalid_ids || []),
  ])];
  const blockingDetail = blockingIds.length
    ? ` ${t("experiment.blockingExperimentIds", {ids: blockingIds.join(", ")})}`
    : "";
  if (["dependent_experiments", "linked_experiments_not_ready"].includes(experiment.action_block_reason)) {
    return `${t("experiment.linkedExperimentsNotReady")}${blockingDetail}`;
  }
  if (experiment.action_block_reason === "linked_experiments_invalid") {
    return `${t("experiment.linkedExperimentsInvalid")}${blockingDetail}`;
  }
  if (experiment.action_block_reason === "archive_before_delete") {
    return t("experiment.archiveBeforeDelete");
  }
  return `${t("experiment.activeRunsBlocked")}${blockingDetail}`;
}

function bindExperimentBulkArchive(root) {
  const archiveButton = root.querySelector("#archiveSelectedExperiments");
  const countLabel = root.querySelector("#selectedExperimentCount");
  const selectAll = root.querySelector("[data-select-all-experiments]");
  const checkboxes = Array.from(root.querySelectorAll('[name="archive_experiment"]'));
  if (!archiveButton || !countLabel || !selectAll || !checkboxes.length) return;
  const selectable = checkboxes.filter((checkbox) => !checkbox.disabled);
  let submitting = false;

  function updateSelection() {
    const selected = selectable.filter((checkbox) => checkbox.checked);
    countLabel.textContent = t("experiment.selectedForArchive", {count: formatNumber(selected.length)});
    selectAll.checked = selectable.length > 0 && selected.length === selectable.length;
    selectAll.indeterminate = selected.length > 0 && selected.length < selectable.length;
    selectAll.disabled = submitting || selectable.length === 0;
    selectable.forEach((checkbox) => { checkbox.disabled = submitting; });
    archiveButton.disabled = submitting || selected.length === 0;
  }

  selectAll.addEventListener("change", () => {
    const checked = selectAll.checked;
    selectable.forEach((checkbox) => { checkbox.checked = checked; });
    updateSelection();
  });
  selectable.forEach((checkbox) => checkbox.addEventListener("change", updateSelection));
  archiveButton.addEventListener("click", async () => {
    const experimentIds = selectable
      .filter((checkbox) => checkbox.checked)
      .map((checkbox) => checkbox.value);
    if (!experimentIds.length) return;
    if (!window.confirm(t("experiment.bulkArchiveConfirm", {count: formatNumber(experimentIds.length)}))) return;
    submitting = true;
    updateSelection();
    try {
      const result = await apiPost("/api/platform/experiments/archive", {experiment_ids: experimentIds});
      rememberToast(t("experiment.bulkArchivedSuccess", {count: formatNumber(result.archived_count || 0)}));
      location.reload();
    } catch (error) {
      submitting = false;
      updateSelection();
      showToast(t("experiment.bulkArchiveFailed", {message: error.message}));
    }
  });
  updateSelection();
}

function bindExperimentActions(root) {
  root.querySelectorAll("[data-archive-experiment]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (button.getAttribute("aria-disabled") === "true") {
        showToast(button.dataset.blockMessage || t("experiment.activeRunsBlocked"));
        return;
      }
      const experimentId = button.dataset.archiveExperiment;
      if (!window.confirm(t("experiment.archiveConfirm", {id: experimentId}))) return;
      button.disabled = true;
      try {
        await apiPost(`/api/platform/experiments/${encodeURIComponent(experimentId)}/archive`, {archived: true});
        rememberToast(t("experiment.archivedSuccess"));
        location.reload();
      } catch (error) {
        button.disabled = false;
        showToast(t("experiment.archiveFailed", {message: error.message}));
      }
    });
  });
  root.querySelectorAll("[data-restore-experiment]").forEach((button) => {
    button.addEventListener("click", async () => {
      const experimentId = button.dataset.restoreExperiment;
      button.disabled = true;
      try {
        await apiPost(`/api/platform/experiments/${encodeURIComponent(experimentId)}/archive`, {archived: false});
        rememberToast(t("experiment.restoredSuccess"));
        location.reload();
      } catch (error) {
        button.disabled = false;
        showToast(t("experiment.restoreFailed", {message: error.message}));
      }
    });
  });
  root.querySelectorAll("[data-delete-experiment]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (button.getAttribute("aria-disabled") === "true") {
        showToast(button.dataset.blockMessage || t("experiment.activeRunsBlocked"));
        return;
      }
      const experimentId = button.dataset.deleteExperiment;
      const deleteCount = Number(button.dataset.deleteCount || 1);
      const confirmationKey = deleteCount > 1
        ? "experiment.deleteLinkedConfirm"
        : "experiment.deleteConfirm";
      if (!window.confirm(t(confirmationKey, {
        id: experimentId,
        experimentCount: formatNumber(deleteCount),
        dependentCount: formatNumber(Math.max(0, deleteCount - 1)),
        runCount: formatNumber(button.dataset.runCount || 0),
      }))) return;
      button.disabled = true;
      try {
        const groupId = button.dataset.groupId;
        const endpoint = groupId
          ? `/api/platform/experiment-groups/${encodeURIComponent(groupId)}/experiments/${encodeURIComponent(experimentId)}`
          : `/api/platform/experiments/${encodeURIComponent(experimentId)}`;
        const result = await apiDelete(endpoint);
        rememberToast(t(
          Number(result.deleted_experiment_count || 1) > 1
            ? "experiment.deletedLinkedSuccess"
            : "experiment.deletedSuccess",
          {count: formatNumber(result.deleted_experiment_count || 1)},
        ));
        if (groupId) location.reload();
        else location.href = "/experiments?stage=archived";
      } catch (error) {
        button.disabled = false;
        showToast(t("experiment.deleteFailed", {message: error.message}));
      }
    });
  });
}

function bindRunActions(root) {
  root.querySelectorAll("[data-resume-cloud]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await apiPost(`/api/platform/runs/${encodeURIComponent(button.dataset.resumeCloud)}/resume`, {});
        location.reload();
      } catch (error) { button.disabled = false; showToast(error.message); }
    });
  });
  root.querySelectorAll("[data-copy-run]").forEach((button) => {
    button.addEventListener("click", async () => {
      const link = new URL(`/runs/${encodeURIComponent(button.dataset.copyRun)}`, location.origin).href;
      try {
        await copyText(link);
        showToast(t("run.linkCopied"));
      } catch (error) {
        showToast(t("run.copyFailed", {message: error.message}));
      }
    });
  });
  root.querySelectorAll("[data-delete-run]").forEach((button) => {
    button.addEventListener("click", async () => {
      const runId = button.dataset.deleteRun;
      if (!window.confirm(t("run.deleteConfirm", {runId}))) return;
      button.disabled = true;
      try {
        await apiDelete(`/api/platform/runs/${encodeURIComponent(runId)}`);
        showToast(t("run.deleted"));
        if (button.dataset.deleteRedirect) {
          location.href = button.dataset.deleteRedirect;
        } else {
          location.reload();
        }
      } catch (error) {
        button.disabled = false;
        showToast(t("run.deleteFailed", {message: error.message}));
      }
    });
  });
  root.querySelectorAll("[data-interrupt-run]").forEach((button) => {
    button.addEventListener("click", async () => {
      const runId = button.dataset.interruptRun;
      if (!window.confirm(t("run.interruptConfirm", {runId}))) return;
      button.disabled = true;
      button.textContent = t("run.interrupting");
      try {
        await apiPost(`/api/platform/runs/${encodeURIComponent(runId)}/interrupt`, {});
        rememberToast(t("run.interrupted"));
        location.reload();
      } catch (error) {
        button.disabled = false;
        button.textContent = t("run.interrupt");
        showToast(t("run.interruptFailed", {message: error.message}));
      }
    });
  });
}

function bindRunComparison(root, button) {
  if (!root || !button) return;
  const checkboxes = Array.from(root.querySelectorAll('[name="compare_run"]'));
  const selectAll = root.querySelector("[data-select-all-runs]");
  const update = () => {
    const count = checkboxes.filter((checkbox) => checkbox.checked).length;
    button.disabled = count < 2;
    button.textContent = t("runs.compareSelected", {count});
    if (selectAll) {
      selectAll.checked = checkboxes.length > 0 && count === checkboxes.length;
      selectAll.indeterminate = count > 0 && count < checkboxes.length;
    }
  };
  checkboxes.forEach((checkbox) => {
    checkbox.addEventListener("change", update);
  });
  selectAll?.addEventListener("change", () => {
    const checked = selectAll.checked;
    checkboxes.forEach((checkbox) => {
      checkbox.checked = checked;
    });
    checkboxes.forEach((checkbox) => {
      checkbox.dispatchEvent(new Event("change", {bubbles: true}));
    });
  });
  update();
}

function bindRunArchiveSelection(root, button) {
  if (!root || !button) return;
  const experimentIds = () => [...new Set(
    [...root.querySelectorAll('[name="compare_run"]:checked')]
      .filter((checkbox) => checkbox.dataset.experimentArchived !== "true")
      .map((checkbox) => checkbox.dataset.experimentId)
      .filter(Boolean),
  )];
  const update = () => {
    const count = experimentIds().length;
    button.disabled = button.dataset.submitting === "true" || count === 0;
    button.textContent = t("runs.archiveSelectedExperiments", {count: formatNumber(count)});
  };
  root.querySelectorAll('[name="compare_run"]').forEach((checkbox) => {
    checkbox.addEventListener("change", update);
  });
  if (button.dataset.archiveBound !== "true") {
    button.dataset.archiveBound = "true";
    button.addEventListener("click", async () => {
      const selectedIds = experimentIds();
      if (!selectedIds.length || button.dataset.submitting === "true") return;
      if (!window.confirm(t("experiment.bulkArchiveConfirm", {count: formatNumber(selectedIds.length)}))) return;
      button.dataset.submitting = "true";
      update();
      try {
        const result = await apiPost("/api/platform/experiments/archive", {experiment_ids: selectedIds});
        rememberToast(t("experiment.bulkArchivedSuccess", {count: formatNumber(result.archived_count || 0)}));
        location.reload();
      } catch (error) {
        delete button.dataset.submitting;
        update();
        showToast(t("experiment.bulkArchiveFailed", {message: error.message}));
      }
    });
  }
  update();
}

function bindRunDeleteSelection(root, button) {
  if (!root || !button) return;
  const selectedCheckboxes = () => [
    ...root.querySelectorAll('[name="compare_run"]:checked'),
  ];
  const update = () => {
    const selected = selectedCheckboxes();
    const hasActiveRun = selected.some(
      (checkbox) => checkbox.dataset.runCanDelete !== "true",
    );
    const submitting = button.dataset.submitting === "true";
    button.disabled = submitting || selected.length === 0 || hasActiveRun;
    button.textContent = t(
      submitting ? "runs.deletingSelected" : "runs.deleteSelected",
      {count: formatNumber(selected.length)},
    );
    button.title = hasActiveRun ? t("runs.bulkDeleteActiveBlocked") : "";
  };
  root.querySelectorAll('[name="compare_run"]').forEach((checkbox) => {
    checkbox.addEventListener("change", update);
  });
  if (button.dataset.deleteBound !== "true") {
    button.dataset.deleteBound = "true";
    button.addEventListener("click", async () => {
      const selected = selectedCheckboxes();
      if (!selected.length || button.dataset.submitting === "true") return;
      if (selected.some((checkbox) => checkbox.dataset.runCanDelete !== "true")) {
        showToast(t("runs.bulkDeleteActiveBlocked"));
        return;
      }
      const runIds = selected.map((checkbox) => checkbox.value);
      if (!window.confirm(t("runs.bulkDeleteConfirm", {count: formatNumber(runIds.length)}))) return;
      button.dataset.submitting = "true";
      update();
      try {
        const result = await apiPost("/api/platform/runs/delete", {run_ids: runIds});
        rememberToast(t("runs.bulkDeleted", {count: formatNumber(result.deleted_count || 0)}));
        location.reload();
      } catch (error) {
        delete button.dataset.submitting;
        update();
        showToast(t("runs.bulkDeleteFailed", {message: error.message}));
      }
    });
  }
  update();
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const input = document.createElement("textarea");
  input.value = value;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.appendChild(input);
  input.select();
  const copied = document.execCommand("copy");
  input.remove();
  if (!copied) throw new Error(t("run.clipboardUnavailable"));
}

async function renderRun(runId) {
  const run = await apiGet(`/api/platform/runs/${encodeURIComponent(runId)}`);
  if (isToolSelection(run)) return renderValidationRun(run);
  const readOnly = isReadOnlyConfig(run.config);
  const memoryPreview = run.config?.memory?.strategy === "agent_memory_preview" || run.manifest?.preview_only;
  const environmentType = run.manifest?.environment_type || run.runner_type || "moltnet_thread";
  const environmentResource = environmentType === "moltnet_forum" ? "forum" : "thread";
  const [environmentData, readingData] = await Promise.all([
    apiGet(`/api/platform/runs/${encodeURIComponent(runId)}/${environmentResource}?comments_limit=200`),
    readOnly ? apiGet(`/api/platform/runs/${encodeURIComponent(runId)}/rounds?limit=1`) : null,
  ]);
  let readingRound = readingData?.items?.[0] || null;
  let previewCompletedPosts = run.manifest?.completed_posts;
  main.innerHTML = `
    <div class="page">
      ${pageHeader(escapeHtml(run.run_id), escapeHtml(run.experiment_name), `
        <a class="button secondary" href="/experiments/${encodeURIComponent(run.experiment_id)}">${t("run.backToExperiment")}</a>
        <a class="button secondary" href="/memories?run_id=${encodeURIComponent(run.run_id)}">${t("memoryWorkspace.title")}</a>
        <button class="secondary" type="button" data-copy-run="${escapeHtml(run.run_id)}">${t("run.copyLink")}</button>
        <button id="resumeCloudRun" type="button" data-resume-cloud="${escapeHtml(run.run_id)}" ${run.can_resume_cloud || run.can_resume_memory_accounts ? "" : "hidden disabled"}>${t(run.can_resume_memory_accounts ? "memoryWorkspace.resumeRun" : "cloud.resume")}</button>
        <button class="danger" id="interruptRun" type="button" data-interrupt-run="${escapeHtml(run.run_id)}" ${run.can_interrupt ? "" : "disabled"}>${t("run.interrupt")}</button>
        <button class="danger" id="deleteRun" type="button" data-delete-run="${escapeHtml(run.run_id)}" data-delete-redirect="/runs${experimentGroupId(run) ? `?group_id=${encodeURIComponent(experimentGroupId(run))}` : ""}" ${run.can_delete ? "" : "disabled"}>${t("run.delete")}</button>
      `)}
      <div class="run-layout">
        <div class="run-main">
          <section class="run-panel" id="runProgressPanel"></section>
          ${readOnly && !memoryPreview ? `<section class="run-panel" id="runReadingResult">${renderReadOnlyResult(readingRound, run)}</section>` : ""}
          ${!readOnly ? "" : `<section class="run-panel" id="memoryResultPanel">
            <h2>${t(memoryPreview ? "memory.agentMemoryPreview" : "memoryResult.title")}</h2>
            <div id="memoryResultBody">${emptyState(t("memoryResult.loading"))}</div>
          </section>`}
          <section class="run-panel">
            <h2>${readOnly ? t("run.readPosts") : t("run.discussion")}</h2>
            <div id="runThread">${renderRunEnvironment(environmentType, environmentData)}</div>
          </section>
          <section class="run-panel">
            <h2>${t("run.events")}</h2>
            <div id="runEvents"></div>
          </section>
        </div>
        <aside class="run-side">
          <section class="run-panel">
            <h2>${t("run.metrics")}</h2>
            <div id="runMetrics"></div>
          </section>
          <section class="run-panel">
            <h2>${t("run.worker")}</h2>
            <div id="runWorker"></div>
          </section>
          <section class="run-panel">
            <h2>${t("run.frozenConfig")}</h2>
            <div id="runConfig"></div>
          </section>
          <section class="run-panel">
            <h2>${t("run.log")}</h2>
            <pre id="runLog"></pre>
          </section>
        </aside>
      </div>
    </div>`;
  bindRunActions(main);
  updateRunPanels(run);
  if (readOnly) loadMemoryResult(runId);
  if (["queued", "starting", "running"].includes(run.status) || run.screen_active) {
    pollTimer = window.setInterval(async () => {
      try {
        const latest = await apiGet(`/api/platform/runs/${encodeURIComponent(runId)}`);
        updateRunPanels(latest);
        const resumeCloud = document.querySelector("#resumeCloudRun");
        if (resumeCloud) {
          resumeCloud.hidden = !(latest.can_resume_cloud || latest.can_resume_memory_accounts);
          resumeCloud.disabled = !(latest.can_resume_cloud || latest.can_resume_memory_accounts);
          resumeCloud.textContent = t(latest.can_resume_memory_accounts ? "memoryWorkspace.resumeRun" : "cloud.resume");
        }
        if (readOnly && !memoryPreview && (!readingRound || !["queued", "starting", "running"].includes(latest.status))) {
          const data = await apiGet(`/api/platform/runs/${encodeURIComponent(runId)}/rounds?limit=1`);
          readingRound = data.items?.[0] || null;
          document.querySelector("#runReadingResult").innerHTML = renderReadOnlyResult(readingRound, latest);
        }
        if (memoryPreview && (previewCompletedPosts !== latest.manifest?.completed_posts
            || !["queued", "starting", "running"].includes(latest.status))) {
          previewCompletedPosts = latest.manifest?.completed_posts;
          loadMemoryResult(runId);
        }
        if (latest.manifest?.latest_round !== run.manifest?.latest_round) {
          run.manifest = latest.manifest;
          if (readOnly) loadMemoryResult(runId);
          const updatedEnvironment = await apiGet(
            `/api/platform/runs/${encodeURIComponent(runId)}/${environmentResource}?comments_limit=200`,
          );
          document.querySelector("#runThread").innerHTML = renderRunEnvironment(environmentType, updatedEnvironment);
        }
        if (!["queued", "starting", "running"].includes(latest.status) && !latest.screen_active) {
          clearInterval(pollTimer);
        }
      } catch (_) {
        return;
      }
    }, 2000);
  }
}

function renderAmemNote(note) {
  const fields = [
    ["id", "noteId"], ["content", "originalContent"], ["context", "context"],
    ["keywords", "keywords"], ["tags", "tags"], ["links", "links"],
    ["timestamp", "timestamp"], ["last_accessed", "lastAccessed"],
    ["category", "category"], ["retrieval_count", "retrievalCount"],
    ["evolution_history", "officialHistory"],
  ];
  const valueText = (value) => typeof value === "string" ? value : JSON.stringify(value ?? null, null, 2);
  return `<dl class="amem-note-fields">${fields.map(([key, label]) => `<div>
    <dt>${t(`memoryResult.${label}`)}</dt><dd><pre>${escapeHtml(valueText(note[key]))}</pre></dd>
  </div>`).join("")}</dl>
  <details><summary>${t("memoryResult.embedding")} (${Array.isArray(note.embedding) ? note.embedding.length : 0})</summary>
    <pre>${escapeHtml(valueText(note.embedding))}</pre></details>
  <details><summary>${t("memoryResult.fullNoteJson")}</summary><pre>${escapeHtml(JSON.stringify(note, null, 2))}</pre></details>`;
}

function renderMemoryPreviewResult(target, data, items, loadMore) {
  target.innerHTML = `<p class="validation-notice">${t("memoryPreview.help")}</p>
    <p>${t("memoryPreview.progress", {completed: data.total, total: data.post_count, facts: data.fact_count})}
      · ${t("memoryPreview.counts", {save: data.save_decisions, skip: data.skip_decisions, failed: data.failed_decisions})}</p>
    <details><summary>${t("memoryPreview.promptSources")}</summary>
      <p>Hermes: ${escapeHtml(data.hermes_revision || "")}</p>
      ${(data.prompt_sources || []).map((url) => `<p><a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a></p>`).join("")}
    </details>
    <div class="memory-result-list">${items.map((item) => `<article class="memory-result-item">
      <h3>${escapeHtml(item.source_post_id)} · ${t(!item.ok ? "memoryPreview.failed" : item.decision === "save" ? "memoryPreview.save" : "memoryPreview.skip")}</h3>
      ${item.error ? `<pre class="error-text">${escapeHtml(item.error)}</pre>` : ""}
      ${(item.facts || []).map((fact) => `<p><strong>${escapeHtml(fact.target)}</strong></p><pre>${escapeHtml(fact.content)}</pre>`).join("")}
      <details><summary>${t("memoryPreview.rawOutput")}</summary><pre>${escapeHtml(item.response_content || "")}</pre></details>
      ${item.reasoning_content ? `<details><summary>${t("memoryPreview.reasoningOutput")}</summary><pre>${escapeHtml(item.reasoning_content)}</pre></details>` : ""}
      <details><summary>${t("memoryPreview.request")}</summary><pre>${escapeHtml(JSON.stringify(item.request, null, 2))}</pre></details>
      <details><summary>${t("memoryPreview.record")}</summary><pre>${escapeHtml(JSON.stringify(item, null, 2))}</pre></details>
    </article>`).join("")}</div>
    ${data.has_more ? `<button type="button" id="loadMoreMemoryPreview">${t("memoryPreview.more")}</button>` : ""}`;
  target.querySelector("#loadMoreMemoryPreview")?.addEventListener("click", loadMore);
}

async function loadMemoryResult(runId) {
  const target = document.querySelector("#memoryResultBody");
  if (!target) return;
  const state = {items: [], calls: [], total: 0, callsTotal: 0};
  async function load(more = false) {
    try {
      const data = await apiGet(`/api/platform/runs/${encodeURIComponent(runId)}/memory-result?limit=100&offset=${more ? state.items.length : 0}&calls_limit=30&calls_offset=${more ? state.calls.length : 0}`);
      if (!target.isConnected) return;
      if (!more) { state.items = []; state.calls = []; }
      state.items.push(...(data.items || []));
      state.calls.push(...(data.model_calls || []));
      state.total = data.total || 0;
      state.callsTotal = data.calls_total || 0;
      if (data.preview_only) {
        renderMemoryPreviewResult(target, data, state.items, () => load(true));
        return;
      }
      if (typeof renderCloudRunMemoryResult === "function"
          && (memoryProvider(data.strategy) || (data.records || []).some((record) => memoryProvider(record.strategy)))) {
        renderCloudRunMemoryResult(target, data, runId);
        return;
      }
      if (!data.available) {
        target.innerHTML = emptyState(t("memoryResult.unavailable"));
        return;
      }
      const exports = Object.entries(data.exports || {}).map(([agent, value]) =>
        `<p class="validation-notice">${escapeHtml(agent)}: ${escapeHtml(value.completeness || value.error || "-")} (${Number(value.item_count || 0)})</p>`).join("");
      target.innerHTML = `${exports}
        <h3>${t("memoryResult.finalMemories")} (${state.total})</h3>
        ${state.items.length ? `<div class="memory-result-list">${state.items.map((item) => `
          <article class="memory-result-item">
            <strong>${escapeHtml(item.id || "-")}</strong> <small>${escapeHtml(item.kind || memoryStrategyLabel(item.strategy || ""))}</small>
            ${item.atomic_note ? renderAmemNote(item.atomic_note) : `<pre>${escapeHtml(typeof item.content === "string" ? item.content : JSON.stringify(item.content ?? "", null, 2))}</pre>`}
            ${item.details?.reasoning_process ? `<h4>${t("memoryResult.memoryRationale")}</h4><pre>${escapeHtml(item.details.reasoning_process)}</pre>` : ""}
            <details><summary>${t("memoryResult.sources")} (${(item.sources || []).length})</summary>
              ${(item.sources || []).map((source) => `<pre>${escapeHtml(JSON.stringify(source, null, 2))}</pre>`).join("") || `<p>${t("memoryResult.unknown")}</p>`}
              ${(item.unresolved_source_ids || []).length ? `<pre>${escapeHtml(JSON.stringify(item.unresolved_source_ids, null, 2))}</pre>` : ""}
            </details>
            ${item.atomic_note ? `<details><summary>${t("memoryResult.observedVersions")} (${(item.observed_versions || []).length})</summary>
              ${(item.observed_versions || []).length ? `<pre>${escapeHtml(JSON.stringify(item.observed_versions, null, 2))}</pre>` : `<p>${t("memoryResult.unknown")}</p>`}
            </details>` : `<details><summary>${t("memoryResult.history")} (${(item.history || []).length})</summary>
              ${(item.history || []).length ? `<pre>${escapeHtml(JSON.stringify(item.history, null, 2))}</pre>` : `<p>${t("memoryResult.unknown")}</p>`}
            </details>
            <details><summary>${t("memoryResult.metadata")}</summary><pre>${escapeHtml(JSON.stringify(item.details || {}, null, 2))}</pre></details>`}
          </article>`).join("")}</div>` : emptyState(t("memoryResult.noMemories"))}
        ${state.items.length < state.total ? `<button class="secondary compact" type="button" data-memory-more>${t("memoryResult.more")}</button>` : ""}
        <h3>${t("memoryResult.modelCalls")} (${state.callsTotal})</h3>
        ${state.calls.map((call, index) => `<details class="memory-result-item"><summary>${index + 1}. ${escapeHtml(call.agent_id || "-")} / ${escapeHtml(call.purpose || "-")}</summary>
          <h4>${t("memoryResult.reasoning")}</h4><pre>${escapeHtml(call.reasoning_content || t("memoryResult.noReasoning"))}</pre>
          <h4>${t("memoryResult.allToolCalls")}</h4>${(call.tool_calls || []).map((tool, toolIndex) => `<article class="memory-result-item"><strong>${toolIndex + 1}. ${escapeHtml(tool.name || "-")}</strong><pre>${escapeHtml(JSON.stringify(tool.arguments ?? {}, null, 2))}</pre></article>`).join("") || `<p>${t("memoryResult.noToolCalls")}</p>`}
          <details><summary>${t("memoryResult.modelOutput")}</summary><pre>${escapeHtml(JSON.stringify({response_content: call.response_content, tool_calls: call.tool_calls, error: call.error}, null, 2))}</pre></details>
          <details><summary>${t("memoryResult.request")}</summary><pre>${escapeHtml(JSON.stringify(call.request, null, 2))}</pre></details>
        </details>`).join("")}
        ${state.calls.length < state.callsTotal ? `<button class="secondary compact" type="button" data-call-more>${t("memoryResult.moreCalls")}</button>` : ""}`;
      target.querySelector("[data-memory-more]")?.addEventListener("click", () => load(true));
      target.querySelector("[data-call-more]")?.addEventListener("click", () => load(true));
    } catch (error) {
      if (target.isConnected) target.innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;
    }
  }
  await load();
}

function renderReadOnlyResult(round, run) {
  const execution = round?.executions?.find((item) => item.action === "read_thread");
  const result = execution?.execution?.result;
  if (!result) return `<h2>${t("run.readingResult")}</h2><p>${
    ["queued", "starting", "running"].includes(run.status)
      ? t("run.readingWaiting") : t("run.readingNoRecord")
  }</p>`;
  const memoryOutcome = !execution.ok ? t("run.readingMemoryFailed")
    : result.memory_updated ? t("run.readingMemoryUpdated") : t("run.readingMemoryUnchanged");
  return `<div class="section-heading">
    <h2>${t("run.readingResult")}</h2>
    ${statusBadge(execution.ok ? "completed" : "failed")}
  </div>
  <dl class="summary-list">
    ${summaryItem(t("editor.readOnlyAgent"), execution.agent_id || run.config?.simulation?.read_only_agent_id || "-")}
    ${summaryItem(t("run.readBodyCount"), result.read_post_count ?? "-")}
    ${summaryItem(t("run.readCommentCount"), result.read_comment_count ?? "-")}
    ${summaryItem(t("run.readCharacterCount"), result.read_character_count ?? "-")}
    ${summaryItem(t("validation.memoryRevision"), `${result.memory_revision_before ?? "-"} → ${result.memory_revision_after ?? "-"}`)}
    ${summaryItem(t("run.readingMemoryOutcome"), memoryOutcome)}
  </dl>
  
  ${execution.error ? `<p class="error-text">${escapeHtml(execution.error)}</p>` : ""}
  ${renderMemoryProviderResponse(execution)}`;
}

function renderMemoryProviderResponse(execution) {
  const diagnostic = execution.memory_provider_response;
  if (!diagnostic) {
    return String(execution.error || "").startsWith("memory provider returned invalid JSON")
      || String(execution.error || "").startsWith("memory provider must return a JSON object")
      ? `<p class="validation-notice">${t("run.memoryResponseMissing")}</p>` : "";
  }
  const {response_content, parsed_content, raw_response, ...metadata} = diagnostic;
  return `<details class="memory-response">
    <summary>${t("run.memoryProviderResponse")}</summary>
    <h3>${t("run.memoryResponseMetadata")}</h3>
    <pre>${escapeHtml(JSON.stringify(metadata, null, 2))}</pre>
    <h3>${t("run.memoryResponseContent")}</h3>
    <pre>${escapeHtml(response_content || t("run.memoryResponseEmpty"))}</pre>
    <details>
      <summary>${t("run.memoryResponseRaw")}</summary>
      <pre>${escapeHtml(JSON.stringify(raw_response, null, 2))}</pre>
    </details>
  </details>`;
}

function runProgressValues(run) {
  const validation = isToolSelection(run);
  const manifest = run.manifest || {};
  const completed = Number(validation
    ? run.progress?.completed_queries ?? manifest.completed_queries ?? run.progress?.completed_rounds ?? 0
    : run.progress?.completed_rounds ?? manifest.completed_rounds?.length ?? metricValue(run.metrics, "run.completed_rounds", 0));
  const target = Number(validation
    ? run.progress?.target_queries ?? manifest.query_count ?? run.progress?.target_rounds ?? validationFileCounts(run.config?.validation).queries
    : run.progress?.target_rounds ?? manifest.target_rounds ?? metricValue(run.metrics, "run.target_rounds", 0));
  return {
    completed: Number.isFinite(completed) && completed >= 0 ? completed : 0,
    target: Number.isFinite(target) && target > 0 ? target : 0,
  };
}

function formatTargetHitRate(value) {
  if (value === undefined || value === null || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return new Intl.NumberFormat(i18n.getLocale(), {
    style: "percent", maximumFractionDigits: 1,
  }).format(Math.max(0, Math.min(1, number)));
}

function renderTargetOutcomeSummary(manifest, queryCount = 0) {
  const fields = [
    manifest?.target_configured_queries,
    manifest?.target_hits,
    manifest?.target_misses,
    manifest?.target_hit_rate,
  ];
  if (fields.every((value) => value === undefined || value === null)) return "";
  const total = Number(manifest.query_count ?? queryCount ?? 0);
  return `<div class="target-outcome-summary">
    <div><span>${t("validation.targetsConfiguredLabel")}</span><strong>${formatNumber(manifest.target_configured_queries ?? 0)} / ${formatNumber(total)}</strong></div>
    <div><span>${t("validation.hits")}</span><strong>${formatNumber(manifest.target_hits ?? 0)}</strong></div>
    <div><span>${t("validation.misses")}</span><strong>${formatNumber(manifest.target_misses ?? 0)}</strong></div>
    <div><span>${t("validation.hitRate")}</span><strong>${escapeHtml(formatTargetHitRate(manifest.target_hit_rate))}</strong></div>
  </div>`;
}

function renderValidationCategoryStatistics(manifest = {}) {
  const categoryStatistics = manifest.category_statistics;
  const categories = categoryStatistics && typeof categoryStatistics === "object"
    ? Object.entries(categoryStatistics).filter(([, value]) => value && typeof value === "object") : [];
  const rows = [
    {label: t("validation.overall"), statistics: manifest, overall: true},
    ...categories.map(([key, statistics]) => ({
      label: statistics.functional_group || key || t("validation.uncategorized"), statistics,
    })),
  ];
  return `<p class="validation-notice">${t("validation.categoryStatisticsHelp")}</p>
    <div class="table-wrap"><table class="validation-category-statistics">
      <thead><tr>
        <th scope="col">${t("validation.functionalGroup")}</th>
        <th scope="col">${t("validation.queryCount")}</th>
        <th scope="col">${t("validation.completedQueries")}</th>
        <th scope="col">${t("validation.evaluatedQueries")}</th>
        <th scope="col">${t("validation.hits")}</th>
        <th scope="col">${t("validation.misses")}</th>
        <th scope="col">${t("validation.hitRate")}</th>
        <th scope="col">${t("validation.failedActions")}</th>
      </tr></thead>
      <tbody>${rows.map(({label, statistics, overall}) => {
        const evaluated = statistics.target_evaluated_queries
          ?? (Number(statistics.target_hits || 0) + Number(statistics.target_misses || 0));
        return `<tr${overall ? ' class="statistics-overall"' : ""}>
          <th scope="row">${escapeHtml(label)}</th>
          <td>${formatNumber(statistics.query_count ?? 0)}</td>
          <td>${formatNumber(statistics.completed_queries ?? 0)}</td>
          <td>${formatNumber(evaluated)}</td>
          <td>${formatNumber(statistics.target_hits ?? 0)}</td>
          <td>${formatNumber(statistics.target_misses ?? 0)}</td>
          <td>${escapeHtml(formatTargetHitRate(statistics.target_hit_rate))}</td>
          <td>${formatNumber(statistics.failed_actions ?? 0)}</td>
        </tr>`;
      }).join("")}</tbody>
    </table></div>`;
}

async function renderValidationRun(run) {
  const runId = run.run_id;
  main.innerHTML = `<div class="page">
    ${pageHeader(escapeHtml(runId), escapeHtml(run.experiment_name), `
      <a class="button secondary" href="/experiments/${encodeURIComponent(run.experiment_id)}">${t("run.backToExperiment")}</a>
      <button class="secondary" type="button" data-copy-run="${escapeHtml(runId)}">${t("run.copyLink")}</button>
      <button class="secondary" id="resumeMemoryAccountRun" type="button" data-resume-cloud="${escapeHtml(runId)}" ${run.can_resume_memory_accounts ? "" : "hidden disabled"}>${t("memoryWorkspace.resumeRun")}</button>
      <a class="button secondary" href="/memories?run_id=${encodeURIComponent(run.config?.validation?.source_run_id || "")}">${t("memoryWorkspace.title")}</a>
      <button class="danger" id="interruptRun" type="button" data-interrupt-run="${escapeHtml(runId)}" ${run.can_interrupt ? "" : "disabled"}>${t("run.interrupt")}</button>
      <button class="danger" id="deleteRun" type="button" data-delete-run="${escapeHtml(runId)}" data-delete-redirect="/runs${experimentGroupId(run) ? `?group_id=${encodeURIComponent(experimentGroupId(run))}` : ""}" ${run.can_delete ? "" : "disabled"}>${t("run.delete")}</button>
    `)}
    <p class="validation-stage-label"><span class="tag blue">${t("validation.phaseTwo")}</span></p>
    <section class="run-panel validation-statistics-panel"><h2>${t("validation.categoryStatistics")}</h2><div id="validationCategoryStatistics"></div></section>
    <div class="run-layout validation-run-layout">
      <div class="run-main">
        <section class="run-panel" id="runProgressPanel"></section>
        <section class="run-panel"><h2>${t("memoryWorkspace.accountActivity")}</h2><div id="memoryAccountRunActivity"></div></section>
        <section class="run-panel">
          ${sectionHeading(t("validation.selections"), `<button class="secondary compact" type="button" id="refreshSelections">${t("validation.refresh")}</button>`)}
          <p class="validation-notice">${t("validation.resultsHelp")}</p>
          <p id="selectionStatus" class="post-file-status" role="status" aria-live="polite"></p>
          <div id="selectionResults">${emptyState(t("validation.selectionsLoading"))}</div>
          <div class="pager" id="selectionPager">
            <span id="selectionPageLabel"></span>
            <button type="button" class="secondary compact" id="selectionPrevious" disabled>${t("common.previous")}</button>
            <button type="button" class="secondary compact" id="selectionNext" disabled>${t("common.next")}</button>
          </div>
        </section>
        <section class="run-panel"><h2>${t("run.events")}</h2><div id="runEvents"></div></section>
      </div>
      <aside class="run-side">
        <section class="run-panel"><h2>${t("run.metrics")}</h2><div id="runMetrics"></div></section>
        <section class="run-panel"><h2>${t("run.frozenConfig")}</h2><div id="runConfig"></div></section>
        <section class="run-panel"><h2>${t("run.worker")}</h2><div id="runWorker"></div></section>
        <section class="run-panel"><h2>${t("run.log")}</h2><pre id="runLog"></pre></section>
      </aside>
    </div>
  </div>`;
  bindRunActions(main);
  updateRunPanels(run);
  if (typeof updateMemoryAccountRunActivity === "function") updateMemoryAccountRunActivity(run);
  const state = {offset: 0, limit: 50, total: 0, hasMore: false, items: [], loading: false};
  const previous = document.querySelector("#selectionPrevious");
  const next = document.querySelector("#selectionNext");
  const refresh = document.querySelector("#refreshSelections");
  const results = document.querySelector("#selectionResults");
  const status = document.querySelector("#selectionStatus");
  async function loadSelections(offset = state.offset) {
    if (state.loading || !results.isConnected) return;
    state.loading = true;
    previous.disabled = true;
    next.disabled = true;
    refresh.disabled = true;
    try {
      const data = await apiGet(`/api/platform/runs/${encodeURIComponent(runId)}/selections?limit=${state.limit}&offset=${offset}`);
      if (!results.isConnected) return;
      state.items = data.items || [];
      state.offset = offset;
      state.total = data.total || 0;
      state.hasMore = Boolean(data.has_more);
      results.innerHTML = selectionsTable(state.items);
      document.querySelector("#selectionPageLabel").textContent = t("validation.selectionPage", {
        first: state.items.length ? offset + 1 : 0,
        last: offset + state.items.length, total: state.total,
      });
      status.textContent = "";
      status.classList.remove("has-error");
    } catch (error) {
      status.classList.add("has-error");
      status.textContent = t("validation.selectionsFailed", {message: error.message});
    } finally {
      state.loading = false;
      previous.disabled = state.offset === 0;
      next.disabled = !state.hasMore;
      refresh.disabled = false;
    }
  }
  results.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-selection-detail]");
    if (!button) return;
    const item = state.items[Number(button.dataset.selectionDetail)];
    if (!item) return;
    button.disabled = true;
    try {
      const detail = await apiGet(`/api/platform/runs/${encodeURIComponent(runId)}/selections/${Number(item.query_index)}`);
      openSelectionDetail(detail);
    } catch (error) {
      status.classList.add("has-error");
      status.textContent = t("validation.selectionsFailed", {message: error.message});
    } finally {
      button.disabled = false;
    }
  });
  previous.addEventListener("click", () => loadSelections(Math.max(0, state.offset - state.limit)));
  next.addEventListener("click", () => loadSelections(state.offset + state.limit));
  refresh.addEventListener("click", () => loadSelections());
  await loadSelections();
  if (hasActiveRuns([run]) || run.screen_active) {
    let polling = false;
    pollTimer = window.setInterval(async () => {
      if (polling || !results.isConnected) return;
      polling = true;
      try {
        const latest = await apiGet(`/api/platform/runs/${encodeURIComponent(runId)}`);
        updateRunPanels(latest);
        if (typeof updateMemoryAccountRunActivity === "function") updateMemoryAccountRunActivity(latest);
        await loadSelections();
        if (!hasActiveRuns([latest]) && !latest.screen_active) clearInterval(pollTimer);
      } catch (error) {
        status.classList.add("has-error");
        status.textContent = t("validation.selectionsFailed", {message: error.message});
      } finally {
        polling = false;
      }
    }, 2000);
  }
}

function selectionDisplayValue(value) {
  if (value === null || value === undefined || value === "") return "-";
  return typeof value === "object" ? (value.name || value.server_id || value.tool_name || value.id || JSON.stringify(value)) : String(value);
}

function selectionTargetOutcome(item) {
  const status = item.evaluation_status;
  const configured = item.target_configured ?? Boolean(item.target_server && item.target_tool);
  if (!configured || status === "not_configured") {
    return {key: "not-configured", label: t("validation.targetNotConfigured")};
  }
  if (status === "hit" || item.target_hit === true) return {key: "hit", label: t("validation.targetHit")};
  if (status === "miss" || item.target_hit === false) return {key: "miss", label: t("validation.targetMiss")};
  const hit = item.status === "completed"
    && item.selected_server === item.target_server && item.selected_tool === item.target_tool;
  return {key: hit ? "hit" : "miss", label: t(hit ? "validation.targetHit" : "validation.targetMiss")};
}

function targetOutcomeBadge(item) {
  const outcome = selectionTargetOutcome(item);
  return `<span class="status-badge target-${outcome.key}">${escapeHtml(outcome.label)}</span>`;
}

function selectionsTable(items) {
  if (!items.length) return emptyState(t("validation.noSelections"));
  return `<div class="table-wrap"><table class="selections-table">
    <thead><tr><th>${t("validation.queryLabel")}</th><th>${t("validation.targetTool")}</th><th>${t("validation.selectedTool")}</th><th>${t("validation.targetOutcome")}</th><th>${t("validation.arguments")}</th><th>${t("table.status")}</th><th>${t("table.actions")}</th></tr></thead>
    <tbody>${items.map((item, index) => `<tr>
      <td><strong>${escapeHtml(item.query_id)}</strong><p class="selection-query">${escapeHtml(truncate(item.query, 200))}</p></td>
      <td><strong>${escapeHtml(selectionDisplayValue(item.target_tool))}</strong><small>${escapeHtml(selectionDisplayValue(item.target_server))}</small></td>
      <td><strong>${escapeHtml(selectionDisplayValue(item.selected_tool))}</strong><small>${escapeHtml(selectionDisplayValue(item.selected_server))}</small></td>
      <td>${targetOutcomeBadge(item)}</td>
      <td><code class="selection-arguments">${escapeHtml(truncate(JSON.stringify(item.arguments ?? {}), 150))}</code></td>
      <td>${statusBadge(item.status)}${item.error ? `<small class="error-text">${escapeHtml(truncate(selectionDisplayValue(item.error), 180))}</small>` : ""}</td>
      <td><button type="button" class="secondary compact" data-selection-detail="${index}">${t("validation.details")}</button></td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function openSelectionDetail(item) {
  dialogTitle.innerHTML = `<h2>${t("validation.selectionDetail")}</h2><small>${escapeHtml(item.query_id)}</small>`;
  dialogBody.innerHTML = `<div class="selection-detail">
    <div class="detail-meta">
      ${detailMeta(t("validation.agentId"), item.agent_id)}
      ${detailMeta(t("validation.memoryRevision"), item.memory_revision ?? "-")}
      ${detailMeta(t("table.status"), statusLabel(item.status))}
    </div>
    <section><h3>${t("validation.queryLabel")}</h3><pre>${escapeHtml(item.query || "")}</pre></section>
    <dl class="summary-list">
      ${summaryItem(t("validation.targetServer"), selectionDisplayValue(item.target_server))}
      ${summaryItem(t("validation.targetTool"), selectionDisplayValue(item.target_tool))}
      ${summaryItem(t("validation.selectedServer"), selectionDisplayValue(item.selected_server))}
      ${summaryItem(t("validation.selectedTool"), selectionDisplayValue(item.selected_tool))}
      <div><dt>${t("validation.targetOutcome")}</dt><dd>${targetOutcomeBadge(item)}</dd></div>
      ${item.target_alias ? summaryItem(t("validation.targetAlias"), item.target_alias) : ""}
      ${item.selected_alias ? summaryItem(t("validation.selectedAlias"), item.selected_alias) : ""}
    </dl>
    <section><h3>${t("validation.arguments")}</h3><pre>${escapeHtml(JSON.stringify(item.arguments ?? {}, null, 2))}</pre></section>
    ${item.memory_read_audit && typeof memoryReadAuditMarkup === "function" ? memoryReadAuditMarkup(item.memory_read_audit) : ""}
    ${item.memory_context ? `<section><h3>${t("memoryResult.deliveredMemory")}</h3>
      ${(item.memory_context.items || []).map((memory, index) => `<article class="memory-result-item"><strong>${index + 1}${memory.atomic_note ? `. ${t("memoryResult.atomicNote")}` : ""}</strong>
        <h4>${t("memoryResult.deliveredLine")}</h4><pre>${escapeHtml(memory.rendered_line || "")}</pre>
        ${memory.line_truncated ? `<p class="validation-notice">${t("memoryResult.partialDelivery")}</p>` : ""}
        ${memory.atomic_note ? `<p class="validation-notice">${t("memoryResult.snapshotOnly")}</p>${renderAmemNote(memory.atomic_note)}` : ""}
        <details><summary>${t("memoryResult.sources")}</summary><pre>${escapeHtml(JSON.stringify(memory.origin_event || memory.source_event || {}, null, 2))}</pre></details>
        ${memory.atomic_note ? `<details><summary>${t("memoryResult.retrievalInfo")}</summary><pre>${escapeHtml(JSON.stringify(memory.source_event?.metadata || {}, null, 2))}</pre></details>` : ""}
      </article>`).join("") || `<p>${t("memoryResult.noMemories")}</p>`}
      <details><summary>${t("memoryResult.exactContext")}</summary><pre>${escapeHtml(item.memory_context.delivered_memory || "")}</pre></details>
      ${item.memory_context.memory_suffix ? `<p>${t("memoryResult.memorySuffix")}</p><pre>${escapeHtml(item.memory_context.memory_suffix)}</pre>` : ""}
    </section>` : ""}
    <section><h3>${t("memoryResult.reasoning")}</h3><pre>${escapeHtml(item.reasoning_content || item.response_content || t("memoryResult.noReasoning"))}</pre></section>
    <section><h3>${t("memoryResult.allToolCalls")}</h3>${(item.tool_calls || []).map((call, index) => `<article class="memory-result-item"><strong>${index + 1}. ${escapeHtml(call.name || "-")}</strong><pre>${escapeHtml(JSON.stringify(call.arguments ?? {}, null, 2))}</pre></article>`).join("") || `<p>${t("memoryResult.noToolCalls")}</p>`}</section>
    ${item.request ? `<details><summary>${t("memoryResult.request")}</summary><pre>${escapeHtml(JSON.stringify(item.request, null, 2))}</pre></details>` : ""}
    ${item.raw_response ? `<details><summary>${t("memoryResult.modelOutput")}</summary><pre>${escapeHtml(JSON.stringify(item.raw_response, null, 2))}</pre></details>` : ""}
    ${item.error ? `<section><h3>${t("validation.error")}</h3><pre>${escapeHtml(selectionDisplayValue(item.error))}</pre></section>` : ""}
    ${item.tool_result !== undefined && item.tool_result !== null ? `<details><summary>${t("validation.toolResult")}</summary><pre>${escapeHtml(JSON.stringify(item.tool_result, null, 2))}</pre></details>` : ""}
  </div>`;
  detailDialog.showModal();
}

function updateRunPanels(run) {
  const manifest = run.manifest || {};
  const validation = isToolSelection(run);
  const readOnly = isReadOnlyConfig(run.config);
  const {completed, target} = runProgressValues(run);
  const interruptButton = document.querySelector("#interruptRun");
  const deleteButton = document.querySelector("#deleteRun");
  if (deleteButton) {
    deleteButton.disabled = !run.can_delete;
    deleteButton.title = run.can_delete ? t("run.delete") : t("run.deleteActiveBlocked");
  }
  if (interruptButton) {
    interruptButton.disabled = !run.can_interrupt;
    interruptButton.title = run.can_interrupt
      ? t("run.interrupt") : t("run.interruptInactiveBlocked");
  }
  const memoryProgress = manifest.memory_event_progress || run.memory_event_progress;
  const memoryProgressNotice = renderMemoryEventProgress(memoryProgress)
    + renderMemoryAgentProgress(manifest.memory_event_progress_by_agent || run.memory_event_progress_by_agent);
  document.querySelector("#runProgressPanel").innerHTML = `
    <div class="section-heading">
      <div><h2>${t("run.liveProgress")}</h2></div>
      ${statusBadge(manifest.status || run.status)}
    </div>
    <div class="progress-line">
      <progress value="${completed}" max="${Math.max(1, target)}"></progress>
      <strong>${completed} / ${target || "-"}${validation ? ` ${t("validation.queryUnit")}` : readOnly ? ` ${t("run.readingPassUnit")}` : ""}</strong>
    </div>
    ${memoryProgressNotice}
    ${renderMemoryWriteErrors(manifest.memory_write_errors)}
    ${validation && manifest.current_query_id ? `<p class="validation-notice">${escapeHtml(t("validation.currentQuery", {queryId: manifest.current_query_id}))}</p>` : ""}
    ${validation ? renderTargetOutcomeSummary(manifest, target) : ""}
    ${run.error || manifest.error ? `<p class="error-text">${escapeHtml(run.error || manifest.error)}</p>` : ""}`;
  document.querySelector("#runEvents").innerHTML = renderEvents(run.events || []);
  document.querySelector("#runMetrics").innerHTML = validation
    ? renderTargetOutcomeSummary(manifest, target) + renderMetrics(run.metrics || {})
    : renderMetrics(run.metrics || {});
  const categoryStatisticsPanel = document.querySelector("#validationCategoryStatistics");
  if (categoryStatisticsPanel) categoryStatisticsPanel.innerHTML = renderValidationCategoryStatistics(manifest);
  document.querySelector("#runWorker").innerHTML = `
    <dl class="summary-list">
      ${summaryItem(t("run.screen"), run.screen_session || "-")}
      ${summaryItem(t("run.session"), run.screen_active ? t("run.active") : t("run.closed"))}
      ${summaryItem(t("run.started"), formatDate(run.started_at || run.created_at))}
      ${summaryItem(t("run.output"), relativePath(run.output_dir))}
    </dl>
    ${run.screen_attach_command ? `<pre>${escapeHtml(run.screen_attach_command)}</pre>` : ""}`;
  const config = run.config || {};
  const memory = config.memory || {};
  document.querySelector("#runConfig").innerHTML = validation ? validationConfigSummary(config) : `
    <dl class="summary-list">
      ${summaryItem(t("editor.simulationMode"), simulationModeLabel(config.simulation?.mode))}
      ${readOnly ? summaryItem(t("editor.readOnlyAgent"), config.simulation?.read_only_agent_id || "-") : ""}
      ${summaryItem(t("summary.memory"), memoryStrategyLabel(memory.strategy || "langmem_official"))}
      ${memory.model ? summaryItem(t("editor.memoryModel"), memory.model) : ""}
      ${summaryItem(t("run.memoryOptions"), formatConfigValue(memory.options || {}))}
      ${summaryItem(t("experiment.agentCount"), config.agents?.count ?? "-")}
      ${summaryItem(readOnly ? t("run.readingPasses") : t("run.targetRounds"), config.simulation?.rounds ?? target ?? "-")}
    </dl>`;
  document.querySelector("#runLog").textContent = run.log_tail || t("run.waitingForWorker");
}

function runConfigurationLabel(run) {
  const config = run.display_config || {};
  const filename = config.post_filename || t(config.post_source === "database" ? "runs.databasePosts" : "runs.unknownConfig");
  const strategy = config.memory_strategy ? memoryStrategyLabel(config.memory_strategy) : t("runs.unknownConfig");
  const hidden = typeof config.hide_speaker === "boolean" ? t(config.hide_speaker ? "common.yes" : "common.no") : t("runs.unknownConfig");
  return `<a href="/runs/${encodeURIComponent(run.run_id)}"><strong>${escapeHtml(filename)}</strong></a>
    <small>${t("runs.memoryMechanism")}: ${escapeHtml(strategy)}</small>
    <small>${t("runs.hideAttackerIdentity")}: ${escapeHtml(hidden)}</small>
    ${config.hide_speaker === false ? `<small>${t("editor.speakerAsJson")}: ${typeof config.speaker_as_json === "boolean" ? t(config.speaker_as_json ? "common.yes" : "common.no") : t("runs.unknownConfig")}</small>` : ""}
    <small class="run-id">${escapeHtml(run.run_id)}</small>`;
}

function runsTable(runs, options = {}) {
  if (!runs?.length) return emptyState(t("empty.runs"));
  return `<div class="table-wrap"><table class="runs-table">
    <thead><tr>
      ${options.selectable ? `<th class="select-column"><label class="compare-check"><input type="checkbox" data-select-all-runs aria-label="${escapeHtml(t("runs.selectAll"))}" /><span>${t("table.select")}</span></label></th>` : ""}
      <th class="run-column">${t("runs.postConfiguration")}</th><th class="experiment-column">${t("table.experiment")}</th><th class="status-column">${t("table.status")}</th>
      <th class="progress-column">${t("table.progress")}</th><th class="delta-column">${runs.some(isToolSelection) ? t("validation.outcome") : t("table.delta")}</th><th class="created-column">${t("common.created")}</th><th class="actions-column">${t("table.actions")}</th>
    </tr></thead>
    <tbody>${runs.map((run) => {
      const metric = isToolSelection(run) ? t("validation.queriesProcessed", {count: runProgressValues(run).completed}) : metricValue(run.metrics, "comments.delta");
      return `<tr>
        ${options.selectable ? `<td class="select-column"><label class="compare-check"><input type="checkbox" name="compare_run" value="${escapeHtml(run.run_id)}" data-experiment-id="${escapeHtml(run.experiment_id)}" data-experiment-archived="${run.experiment_archived_at ? "true" : "false"}" data-run-can-delete="${run.can_delete ? "true" : "false"}" aria-label="${escapeHtml(t("runs.selectRun", {runId: run.run_id}))}" /><span>${t("table.select")}</span></label></td>` : ""}
        <td class="run-column">${runConfigurationLabel(run)}<div class="mobile-run-progress">${runProgress(run)}</div></td>
        <td class="experiment-column"><a href="/experiments/${encodeURIComponent(run.experiment_id)}">${escapeHtml(run.experiment_name || run.experiment_id)}</a>${experimentGroupId(run) ? `<small>${t("runs.groupName")}: ${experimentGroupLink(run)}</small>` : ""}<small>${isToolSelection(run) ? t("validation.phaseTwoShort") : t("validation.phaseOneShort")}${run.experiment_archived_at ? ` · ${t("experiment.archived")}` : ""}</small></td>
        <td class="status-column">${statusBadge(run.status)}</td>
        <td class="progress-column">${runProgress(run)}</td>
        <td class="delta-column">${metric === undefined ? "-" : escapeHtml(metric)}</td>
        <td class="created-column nowrap">${formatDate(run.created_at)}</td>
        <td class="actions-column">${runActions(run)}</td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
}

function runProgress(run) {
  const {completed: safeCompleted, target: safeTarget} = runProgressValues(run);
  const label = `${safeCompleted} / ${safeTarget || "-"}${isToolSelection(run) ? ` ${t("validation.queryUnit")}` : ""}`;
  const memoryProgress = run.memory_event_progress || run.manifest?.memory_event_progress;
  return `<div class="table-progress">
    <progress value="${Math.min(safeCompleted, safeTarget || 1)}" max="${safeTarget || 1}" aria-label="${escapeHtml(t("run.progressLabel", {progress: label}))}"></progress>
    <span>${escapeHtml(label)}</span>
    ${renderMemoryEventProgress(memoryProgress, {compact: true})}
  </div>`;
}

function renderMemoryEventProgress(progress, {compact = false} = {}) {
  if (!progress) return "";
  if (progress.status === "skipped") return `<small>${t("run.memoryUpdateSkipped")}</small>`;
  const completed = Math.max(0, Number(progress.completed_events) || 0);
  const total = Math.max(0, Number(progress.total_events) || 0);
  const skipped = Math.max(0, Number(progress.skipped_events) || 0);
  const label = skipped
    ? t("run.memoryEventProgressWithSkips", {completed, skipped, total: total || "-"})
    : t("run.memoryEventProgress", {completed, total: total || "-"});
  const statusKey = {
    running: "run.memoryUpdateRunning", completed: "run.memoryUpdateCompleted", failed: "run.memoryUpdateFailed",
    pending: "run.memoryUpdatePending",
    completed_with_errors: "run.memoryUpdateCompletedWithErrors",
  }[progress.status];
  const status = statusKey ? ` · ${t(statusKey)}` : "";
  if (compact) return `<small class="${progress.status === "failed" ? "error-text" : ""}">${escapeHtml(label + status)}</small>`;
  const details = [];
  if (progress.strategy) details.push(memoryStrategyLabel(progress.strategy));
  if (progress.agent_id) details.push(t("run.memoryProgressAgent", {agentId: progress.agent_id}));
  if (progress.status === "running" && progress.progress_granularity === "batch" && progress.batch_events) {
    details.push(t("run.memoryBatchProgress", {count: progress.batch_events}));
  }
  if (progress.last_completed_event_id) {
    details.push(t("run.lastMemoryEvent", {eventId: progress.last_completed_event_id}));
  }
  if (progress.current_event_id) {
    details.push(t("run.currentMemoryEvent", {eventId: progress.current_event_id}));
  }
  if (progress.rolled_back) details.push(t("run.memoryUpdateRolledBack", {count: progress.processed_events ?? 0}));
  if (progress.remote_state_may_have_changed) details.push(t("run.memoryRemoteStateMayChanged"));
  return `<div class="memory-event-progress validation-notice" role="status" aria-live="polite">
    <strong class="${progress.status === "failed" ? "error-text" : ""}">${escapeHtml(label + status)}</strong>
    <progress value="${Math.min(completed + skipped, total || 1)}" max="${total || 1}" aria-label="${escapeHtml(label)}"></progress>
    ${details.map((detail) => `<small>${escapeHtml(detail)}</small>`).join("")}
  </div>`;
}

function renderMemoryWriteErrors(errors) {
  if (!Array.isArray(errors) || !errors.length) return "";
  return `<section class="memory-write-errors">
    <h3>${escapeHtml(t("run.memoryWriteErrors", {count: errors.length}))}</h3>
    <p>${escapeHtml(t("run.memoryWriteErrorsHint"))}</p>
    ${errors.map((item) => {
      const event = item.source_event || {};
      return `<details>
        <summary>${escapeHtml(event.event_id || "-")} · ${escapeHtml(t("run.memoryWriteAttempts", {attempts: item.attempts, retries: item.retries}))}</summary>
        <dl class="summary-list">
          ${summaryItem(t("run.memoryWriteReader"), item.agent_id || "-")}
          ${summaryItem(t("run.memoryWriteAuthor"), event.actor_id || "-")}
          ${summaryItem(t("run.memoryWriteThread"), event.thread_id || "-")}
        </dl>
        <p class="error-text">${escapeHtml(item.error || "-")}</p>
        <pre>${escapeHtml(event.content || "-")}</pre>
      </details>`;
    }).join("")}
  </section>`;
}

function renderMemoryAgentProgress(byAgent) {
  const entries = byAgent && typeof byAgent === "object" ? Object.entries(byAgent) : [];
  if (entries.length < 2) return "";
  return `<details class="memory-agent-progress"><summary>${t("run.memoryProgressByAgent")}</summary>
    <ul>${entries.map(([agentId, progress]) => `<li><strong>${escapeHtml(agentId)}</strong>${renderMemoryEventProgress(progress, {compact: true})}</li>`).join("")}</ul>
  </details>`;
}

function runActions(run) {
  const runId = escapeHtml(run.run_id);
  const deleteDisabled = run.can_delete ? "" : "disabled";
  const deleteTitle = run.can_delete ? t("run.delete") : t("run.deleteActiveBlocked");
  return `<div class="run-actions">
    <button class="secondary compact" type="button" data-copy-run="${runId}">${t("run.copyLink")}</button>
    ${run.can_interrupt ? `<button class="danger compact" type="button" data-interrupt-run="${runId}">${t("run.interrupt")}</button>` : ""}
    <button class="danger compact" type="button" data-delete-run="${runId}" title="${escapeHtml(deleteTitle)}" ${deleteDisabled}>${t("run.delete")}</button>
  </div>`;
}

function experimentIdLinks(ids) {
  return ids.map((id) => (
    `<a href="/experiments/${encodeURIComponent(id)}">${escapeHtml(id)}</a>`
  )).join(", ");
}

function experimentAssociationLinks(experiment) {
  const validation = experiment.config?.validation || {};
  const memorySource = validation.memory_source || {};
  const sourceIds = [...new Set(
    (experiment.source_experiment_ids || [
      validation.source_experiment_id,
      memorySource.experiment_id,
    ])
      .filter((value) => typeof value === "string" && value.trim())
      .map((value) => value.trim().toLowerCase())
      .filter((value) => value !== experiment.experiment_id),
  )];
  const dependentIds = [...new Set(experiment.dependent_experiment_ids || [])];
  const items = [];
  if (sourceIds.length) {
    items.push(`${t("experiment.linkedStageOne")}: ${experimentIdLinks(sourceIds)}`);
  }
  if (dependentIds.length) {
    items.push(`${t("experiment.linkedStageTwo")}: ${experimentIdLinks(dependentIds)}`);
  }
  return items.map((item) => `<small class="experiment-association">${item}</small>`).join("");
}

function experimentsTable(experiments, options = {}) {
  if (!experiments?.length) return emptyState(t("empty.experiments"));
  return `<div class="table-wrap"><table class="experiments-table">
    <thead><tr>${options.selectable ? `<th class="select-column"><input type="checkbox" data-select-all-experiments aria-label="${escapeHtml(t("experiment.selectAllForArchive"))}" /></th>` : ""}<th>${t("table.experiment")}</th><th>${t("table.runner")}</th>${options.management ? `<th>${t("table.lifecycle")}</th>` : ""}<th>${t("table.configuration")}</th><th>${t("runs.title")}</th><th>${t("table.latest")}</th><th class="actions-column">${t("table.actions")}</th></tr></thead>
    <tbody>${experiments.map((item) => `<tr>
      ${options.selectable ? `<td class="select-column"><input type="checkbox" name="archive_experiment" value="${escapeHtml(item.experiment_id)}" aria-label="${escapeHtml(t("experiment.selectForArchive", {id: item.experiment_id}))}" title="${escapeHtml(item.can_archive ? t("experiment.archive") : experimentActionBlockMessage(item))}" ${item.can_archive ? "" : "disabled"} /></td>` : ""}
      <td><a href="/experiments/${encodeURIComponent(item.experiment_id)}"><strong>${escapeHtml(item.name)}</strong></a><small>${escapeHtml(item.experiment_id)}</small>${experimentGroupId(item) ? `<small>${t("group.title")}: ${experimentGroupLink(item)}</small>` : ""}${experimentAssociationLinks(item)}</td>
      <td><span class="tag blue">${isToolSelection(item) ? t("validation.phaseTwoShort") : t("validation.phaseOneShort")}</span><small>${escapeHtml(item.runner_type)}</small></td>
      ${options.management ? `<td>${experimentLifecycleBadge(item)}</td>` : ""}
      <td><small>${escapeHtml(relativePath(item.config_path))}</small></td>
      <td>${formatNumber(item.run_count || 0)}</td>
      <td>${item.latest_run_status ? statusBadge(item.latest_run_status) : "-"}</td>
      <td class="actions-column"><div class="experiment-actions"><a class="button secondary" href="/experiments/${encodeURIComponent(item.experiment_id)}">${t("common.open")}</a>${options.management ? experimentLifecycleButtons(item, {groupId: options.groupId || ""}) : ""}</div></td>
    </tr>`).join("")}</tbody></table></div>`;
}

function experimentLifecycleBadge(experiment) {
  return experiment.archived
    ? `<span class="tag archived">${t("experiment.archived")}</span>`
    : `<span class="tag active">${t("experiment.active")}</span>`;
}

function renderRunEnvironment(environmentType, data) {
  return environmentType === "moltnet_forum" ? renderForum(data) : renderThread(data);
}

function renderForum(data) {
  if (!data?.available || !data.forum) return emptyState(t("empty.environment"));
  const forum = data.forum;
  const posts = Array.isArray(forum.posts) ? forum.posts : [];
  const grouped = new Map((forum.communities || []).map((community) => [community, []]));
  posts.forEach((post) => {
    if (!grouped.has(post.community)) grouped.set(post.community, []);
    grouped.get(post.community).push(post);
  });
  const communities = [...grouped.entries()].map(([community, communityPosts]) => `
    <section class="forum-community">
      <header class="forum-community-header">
        <h3>${escapeHtml(community)}</h3>
        <span>${t("forum.postSummary", {count: formatNumber(communityPosts.length)})}</span>
      </header>
      <div class="forum-posts">${communityPosts.map((post) => `
        <article class="forum-thread">
          ${renderThread({
            available: true,
            thread: {post, comments: post.comments || []},
            truncated_comments: Number(post.truncated_comments || 0) + Number(post.api_truncated_comments || 0),
          })}
        </article>`).join("") || emptyState(t("empty.environment"))}</div>
    </section>`).join("");
  const omitted = Number(forum.truncated_posts || 0) + Number(data.api_truncated_posts || 0);
  return `<div class="forum-view">
    <div class="forum-overview">
      <strong>${t("forum.totalPosts", {count: formatNumber(forum.total_post_count || posts.length)})}</strong>
      <span>${t("forum.totalComments", {count: formatNumber(forum.total_comment_count || 0)})}</span>
    </div>
    ${communities || emptyState(t("empty.environment"))}
    ${omitted ? `<p class="empty-state">${t("forum.omitted", {count: formatNumber(omitted)})}</p>` : ""}
  </div>`;
}

function renderThread(data) {
  if (!data?.available || !data.thread) return emptyState(t("empty.environment"));
  const thread = data.thread;
  const post = thread.post;
  const comments = Array.isArray(thread.comments) ? thread.comments : [];
  const records = comments.map((comment) => ({comment, children: []}));
  const byId = new Map(records.map((record) => [String(record.comment.comment_id), record]));
  const roots = [];
  records.forEach((record) => {
    const parentId = record.comment.parent_comment_id;
    const parent = parentId === null || parentId === undefined
      ? null
      : byId.get(String(parentId));
    if (parent && parent !== record) parent.children.push(record);
    else roots.push(record);
  });
  const rendered = new Set();
  const renderBranch = (record, depth) => {
    const id = String(record.comment.comment_id);
    if (rendered.has(id)) return "";
    rendered.add(id);
    return renderThreadComment(record.comment, byId, depth)
      + record.children.map((child) => renderBranch(child, depth + 1)).join("");
  };
  let commentHtml = roots.map((record) => renderBranch(record, 0)).join("");
  commentHtml += records.map((record) => renderBranch(record, 0)).join("");
  const postTone = authorTone(post.agent_id || post.display_name);
  return `<div class="thread-view">
    <div class="thread-root author-tone-${postTone}">
      <div class="thread-root-author">
        <span class="comment-avatar">${escapeHtml(authorInitials(post.display_name || post.agent_id || "-"))}</span>
        <span class="thread-author">${escapeHtml(post.display_name || post.agent_id || "-")}</span>
        <span class="source-label historical">${t("thread.historical")}</span>
      </div>
      <h3>${escapeHtml(post.title || t("common.untitled"))}</h3>
      <p>${escapeHtml(post.body || "")}</p>
    </div>
    <div class="thread-comments">${commentHtml || emptyState(t("empty.replies"))}</div>
    ${data.truncated_comments ? `<p class="empty-state">${t("thread.omitted", {count: formatNumber(data.truncated_comments)})}</p>` : ""}
  </div>`;
}

function renderThreadComment(comment, byId, depth) {
  const isHistorical = comment.source === "moltnet";
  const parent = comment.parent_comment_id === null || comment.parent_comment_id === undefined
    ? null
    : byId.get(String(comment.parent_comment_id))?.comment;
  const author = comment.display_name || comment.agent_id || "-";
  const replyLabel = parent
    ? t("thread.replyTo", {author: parent.display_name || parent.agent_id || "-", id: parent.comment_id})
    : comment.parent_comment_id !== null && comment.parent_comment_id !== undefined
      ? t("thread.replyToComment", {id: comment.parent_comment_id})
      : "";
  return `<article class="thread-comment depth-${Math.min(depth, 6)} ${isHistorical ? "is-historical" : "is-simulated"} author-tone-${authorTone(comment.agent_id || author)}">
    <div class="comment-accent" aria-hidden="true"></div>
    <div class="comment-content">
      <header>
        <span class="comment-avatar">${escapeHtml(authorInitials(author))}</span>
        <strong>${escapeHtml(author)}</strong>
        <span class="source-label ${isHistorical ? "historical" : "simulated"}">${isHistorical ? t("thread.historical") : t("thread.simulated")}</span>
        <time>${formatDate(comment.created_at)}</time>
      </header>
      ${replyLabel ? `<div class="reply-to">${escapeHtml(replyLabel)}</div>` : ""}
      <p>${escapeHtml(comment.body || "")}</p>
    </div>
  </article>`;
}

function authorTone(value) {
  let hash = 0;
  for (const char of String(value || "")) hash = ((hash << 5) - hash + char.codePointAt(0)) | 0;
  return Math.abs(hash) % 6;
}

function authorInitials(value) {
  const parts = String(value || "-").split(/[\s_-]+/).filter(Boolean);
  return parts.slice(0, 2).map((part) => Array.from(part)[0]).join("").toUpperCase() || "-";
}

function renderEvents(events) {
  if (!events.length) return emptyState(t("empty.events"));
  return `<div class="event-list">${events.slice().reverse().map((event) => `<div class="event-row">
    <time>${formatDate(event.at)}</time>
    <span><strong>${escapeHtml(event.event || "event")}</strong> ${escapeHtml(eventSummary(event))}</span>
  </div>`).join("")}</div>`;
}

function renderMetrics(metrics) {
  const entries = Object.entries(metrics).filter(([key]) => key !== "validation.category_statistics");
  if (!entries.length) return emptyState(t("empty.metrics"));
  return `<div class="metric-grid">${entries.map(([key, record]) => `<div class="metric-item${(record.value !== null && typeof record.value === "object") || String(record.value ?? "").length > 22 ? " metric-item-wide" : ""}">
    <span>${escapeHtml(key)}</span><strong>${escapeHtml(formatNamedMetric(key, record.value))}</strong>
  </div>`).join("")}</div>`;
}

function comparisonTable(data) {
  if (!data.metric_keys.length) return emptyState(t("empty.comparison"));
  return `<div class="table-wrap"><table class="compare-table">
    <thead><tr><th>${t("table.metric")}</th>${data.runs.map((run) => `<th>${escapeHtml(run.run_id)}</th>`).join("")}</tr></thead>
    <tbody>${data.metric_keys.map((key) => `<tr><td><strong>${escapeHtml(key)}</strong></td>${data.runs.map((run) => `<td>${escapeHtml(formatNamedMetric(key, run.metrics?.[key]?.value))}</td>`).join("")}</tr>`).join("")}</tbody>
  </table></div>`;
}

function pageHeader(title, description, actions) {
  return `<header class="page-header"><div><h1>${title}</h1>${description ? `<p>${description}</p>` : ""}</div>${actions ? `<div class="page-actions">${actions}</div>` : ""}</header>`;
}

function sectionHeading(title, action = "") { return `<div class="section-heading"><div><h2>${title}</h2></div>${action}</div>`; }
function statItem(label, value, note, valueId = "", noteId = "") { return `<div class="stat-item"><span>${label}</span><strong ${valueId ? `id="${valueId}"` : ""}>${value}</strong><small ${noteId ? `id="${noteId}"` : ""}>${note}</small></div>`; }
function statusBadge(status) { return `<span class="status-badge ${escapeHtml(status || "")}">${escapeHtml(statusLabel(status))}</span>`; }
function statusLabel(status) {
  return ({
    paused: t("status.paused"),
    queued: t("status.queued"),
    starting: t("status.starting"),
    running: t("status.running"),
    completed: t("status.completed"),
    completed_with_errors: t("status.completedWithErrors"),
    failed: t("status.failed"),
    interrupted: t("status.interrupted"),
    not_run: t("status.notRun"),
  })[status] || status || "-";
}
function emptyState(message) { return `<div class="empty-state"><strong>${escapeHtml(message)}</strong></div>`; }
function configNavigation(sections) {
  const links = (advanced) => sections
    .filter((section) => Boolean(section.advanced) === advanced)
    .map((section) => `<a href="#config-${section.id}" ${section.stage ? `data-experiment-stage="${section.stage}"` : ""}>${escapeHtml(section.title)}</a>`)
    .join("");
  return `<aside class="config-nav" aria-label="${escapeHtml(t("editor.navigation"))}">
    <strong>${t("editor.basic")}</strong>
    ${links(false)}
    <strong data-config-advanced-heading>${t("editor.advanced")}</strong>
    ${links(true)}
  </aside>`;
}

function configSection(section) {
  const stage = section.stage ? `data-experiment-stage="${section.stage}"` : "";
  if (section.advanced) {
    return `<details class="config-section config-section-collapsible" id="config-${section.id}" ${stage}>
      <summary><span>${escapeHtml(section.title)}</span><span class="tag">${t("editor.advanced")}</span></summary>
      <div class="config-section-body">${section.body}</div>
    </details>`;
  }
  return `<section class="config-section" id="config-${section.id}" ${stage}>
    <header><h2>${escapeHtml(section.title)}</h2></header>${section.body}
  </section>`;
}

function textField(name, label, value, help = "", attrs = "", classes = "") { return `<div class="form-field ${classes}"><label for="${name}">${label}</label><input id="${name}" name="${name}" value="${escapeHtml(value ?? "")}" ${attrs} />${help ? `<small>${escapeHtml(help)}</small>` : ""}</div>`; }
function numberField(name, label, value, min = "", step = "1") { return `<div class="form-field"><label for="${name}">${label}</label><input id="${name}" name="${name}" type="number" value="${escapeHtml(value ?? "")}" ${min !== "" ? `min="${min}"` : ""} step="${step}" /></div>`; }
function memoryNumberField(name, label, value, strategies, min = 1, step = 1, max = "") { return `<div class="form-field" data-memory-strategies="${strategies}"><label for="${name}">${label}</label><input id="${name}" name="${name}" type="number" value="${escapeHtml(value ?? "")}" min="${min}" ${max !== "" ? `max="${max}"` : ""} step="${step}" /></div>`; }
function memoryTextField(name, label, value, strategies) { return `<div class="form-field wide" data-memory-strategies="${strategies}"><label for="${name}">${label}</label><textarea id="${name}" name="${name}">${escapeHtml(value ?? "")}</textarea></div>`; }
function memoryCheckboxField(name, label, checkedValue, strategies) { return `<div class="form-field" data-memory-strategies="${strategies}"><div class="checkbox-field"><input name="${name}" id="${name}" type="checkbox" ${checkedValue ? "checked" : ""} /><label for="${name}">${label}</label></div></div>`; }
function textareaField(name, label, value, classes = "") { return `<div class="form-field ${classes}"><label for="${name}">${label}</label><textarea id="${name}" name="${name}">${escapeHtml(value ?? "")}</textarea></div>`; }
function selectField(name, label, value, options) { return `<div class="form-field"><label for="${name}">${label}</label><select id="${name}" name="${name}">${options.map(([key, text]) => `<option value="${key}" ${key === value ? "selected" : ""}>${text}</option>`).join("")}</select></div>`; }

function selectedExperimentModel(config) {
  const configured = get(config, "backend.model", "");
  return EXPERIMENT_MODELS.includes(configured) ? configured : EXPERIMENT_MODELS[0];
}

function defaultValidationConnectionForModel(model) {
  const validation = platformSettingsCache.validation;
  const selected = validation.model_connections?.[model];
  return clone(selected && selected.mode !== "inherit"
    ? selected : validation.backend_connection);
}

function selectedMemoryModel(config) {
  if (FIXED_LOCAL_MEMORY_STRATEGIES.has(get(config, "memory.strategy", ""))) return FIXED_LOCAL_MEMORY_MODEL;
  const configured = get(config, "memory.model", "") || get(config, "backend.model", "");
  return EXPERIMENT_MODELS.includes(configured) ? configured : EXPERIMENT_MODELS[0];
}

function experimentModelField(
  name, value, {label = t("validation.model"), help = t("validation.modelHelp"), strategies = "", models = EXPERIMENT_MODELS} = {},
) {
  const selected = models.includes(value) ? value : models[0];
  const helpId = `${name}.help`;
  const strategyAttribute = strategies ? ` data-memory-strategies="${strategies}"` : "";
  return `<div class="form-field"${strategyAttribute}>
    <label for="${name}">${label}</label>
    <select id="${name}" name="${name}" required aria-describedby="${helpId}">
      ${models.map((model) => `<option value="${model}" ${model === selected ? "selected" : ""}>${model}</option>`).join("")}
    </select>
    <small id="${helpId}">${help}</small>
  </div>`;
}
function checkboxField(name, label, checkedValue) { return `<div class="form-field"><div class="checkbox-field"><input name="${name}" id="${name}" type="checkbox" ${checkedValue ? "checked" : ""} /><label for="${name}">${label}</label></div></div>`; }
function summaryItem(key, value) { return `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value ?? "-")}</dd></div>`; }
function detailMeta(key, value) { return `<div><span>${escapeHtml(key)}</span><strong>${escapeHtml(value ?? "-")}</strong></div>`; }

function fieldValue(form, name) { return form.elements[name]?.value?.trim() ?? ""; }
function checked(form, name) { return Boolean(form.elements[name]?.checked); }
function numberValue(form, name, fallback) { const value = Number(fieldValue(form, name)); return Number.isFinite(value) ? value : fallback; }
function integerValue(form, name, fallback) { const value = Number.parseInt(fieldValue(form, name), 10); return Number.isFinite(value) ? value : fallback; }
function get(value, path, fallback) { const result = path.split(".").reduce((current, key) => current?.[key], value); return result === undefined ? fallback : result; }
function clone(value) { return JSON.parse(JSON.stringify(value)); }
function relativePath(path) { return String(path || "").replace(/^.*\/agent_community\//, ""); }
function truncate(value, length) { value = String(value || ""); return value.length > length ? `${value.slice(0, length)}...` : value; }


function metricValue(metrics, key, fallback) { const record = metrics?.[key]; return record ? record.value : fallback; }
function formatMetric(value) { if (value === undefined || value === null) return "-"; if (typeof value === "number") return Number.isInteger(value) ? formatNumber(value) : value.toFixed(2); return typeof value === "object" ? JSON.stringify(value) : String(value); }
function formatNamedMetric(key, value) { return /(^|[._])target_hit_rate$/.test(key) ? formatTargetHitRate(value) : formatMetric(value); }
function formatConfigValue(value) { return typeof value === "object" ? JSON.stringify(value) : String(value ?? "-"); }
function eventSummary(event) { const copy = {...event}; delete copy.at; delete copy.event; return Object.entries(copy).map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : value}`).join(" · "); }
function formatNumber(value) { return new Intl.NumberFormat(i18n.getLocale()).format(Number(value || 0)); }
function formatDate(value) { if (!value) return "-"; const date = new Date(value); return Number.isNaN(date.valueOf()) ? escapeHtml(value) : date.toLocaleString(i18n.getLocale(), {hour12: false}); }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[char]); }

async function apiGet(path, options = {}) { return apiRequest(path, {...options, method: "GET"}); }
async function apiPost(path, payload) { return apiRequest(path, {method: "POST", body: JSON.stringify(payload)}); }
async function apiPut(path, payload) { return apiRequest(path, {method: "PUT", body: JSON.stringify(payload)}); }
async function apiDelete(path) { return apiRequest(path, {method: "DELETE"}); }
async function apiRequest(path, options) {
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  let payload;
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) throw new Error(payload.error?.message || `HTTP ${response.status}`);
  return payload;
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 3200);
}

function rememberToast(message) {
  try {
    sessionStorage.setItem(pendingToastKey, message);
  } catch (_) {
    return;
  }
}

function showPendingToast() {
  try {
    const message = sessionStorage.getItem(pendingToastKey);
    if (!message) return;
    sessionStorage.removeItem(pendingToastKey);
    showToast(message);
  } catch (_) {
    return;
  }
}

function renderError(error) {
  main.innerHTML = `<div class="error-state"><strong>${t("error.load")}</strong><span>${escapeHtml(error.message)}</span><button onclick="location.reload()">${t("common.retry")}</button></div>`;
}
function renderNotFound() { main.innerHTML = `<div class="error-state"><strong>${t("error.notFound")}</strong><a class="button" href="/">${t("error.overview")}</a></div>`; }
