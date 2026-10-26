/* Memory inventories, provider accounts and explicit cloud transfer jobs. */
let memoryWorkspaceTimer = null;
let memoryWorkspaceDialogVersion = 0;

function memoryProvider(value) {
  return ["mem0", "mem0_platform_v3"].includes(value) ? "mem0"
    : ["hindsight", "hindsight_cloud"].includes(value) ? "hindsight"
    : ["memos", "memos_cloud"].includes(value) ? "memos" : "";
}

function memoryProviderName(provider) { return ({mem0: "Mem0", hindsight: "Hindsight", memos: "MemOS"})[memoryProvider(provider)] || provider; }

function memoryAccountId(account) { return String(account.account_id || account.id || ""); }
function memoryAccountIsDefault(account) {
  return account.is_default === true || account.export_only === true || ["env-mem0_platform_v3", "env-hindsight_cloud", "env-memos_cloud"].includes(memoryAccountId(account));
}
function memoryAccountName(account) {
  const name = account.name || "";
  return memoryAccountIsDefault(account) && ["", "\u9ed8\u8ba4\u8d26\u53f7", "Default (.env)", "Default account"].includes(name)
    ? t("memoryWorkspace.defaultAccount") : name || memoryAccountId(account);
}
function memoryAccountHasKey(account) { return account.credential_available !== false; }
function memoryAccountMask(account) { return memoryAccountHasKey(account) ? account.masked_key || account.api_key_masked || account.masked_api_key || t("memoryWorkspace.keySaved") : t("memoryWorkspace.keyMissing"); }
function memoryAccountDisplayKey(account) { return typeof account.api_key === "string" && account.api_key ? account.api_key : memoryAccountMask(account); }
function memoryErrorText(error) {
  if (!error || typeof error !== "object") return String(error || "");
  const message = error.message || error.detail || error.error;
  const parts = [error.category || error.code, typeof message === "string" ? message : ""].filter(Boolean);
  return parts.length ? parts.join(": ") : JSON.stringify(error);
}
function memoryPullState(record) {
  const state = record.pull_status || record.pull_state || "not_fetched";
  return ({pending: "queued", fetching: "running", fetched: "complete", completed: "complete",
    not_pulled: "not_fetched", unavailable: "not_fetched"})[state] || state;
}
function memoryStateLabel(state) {
  const known = ["not_fetched", "queued", "running", "complete", "completed", "partial", "failed", "cancelled", "active", "disabled", "quota_exhausted", "cooldown", "verified", "ready", "paused", "local", "unchecked", "invalid_credentials", "forbidden", "verifying", "importing", "processing", "prepared", "unknown", "unverified"];
  return known.includes(state) ? t(`memoryWorkspace.state.${state}`) : String(state || "-");
}
function memoryRecordParams(record, extra = {}) {
  const params = new URLSearchParams(extra);
  if (record.agent_id) params.set("agent_id", record.agent_id);
  if (record.round_index !== undefined && record.round_index !== null) params.set("round_index", record.round_index);
  return params;
}
function memoryRecordPayload(record) {
  return {run_id: record.run_id, agent_id: record.agent_id,
    ...(record.round_index !== undefined && record.round_index !== null ? {round_index: record.round_index} : {})};
}
function memoryRecordMeta(record) {
  return `<dl class="summary-list memory-record-meta">
    ${summaryItem(t("validation.sourceRun"), record.run_id || "-")}
    ${summaryItem(t("validation.agent"), record.agent_id || "-")}
    ${summaryItem(t("validation.memoryRound"), record.round_index ?? "-")}
    ${summaryItem(t("summary.memory"), memoryStrategyLabel(record.strategy || ""))}
    ${summaryItem(t("memoryWorkspace.account"), record.account_id || "-")}
    ${summaryItem(t("memoryWorkspace.scope"), typeof record.scope === "object" ? JSON.stringify(record.scope) : record.scope || "-")}
    ${summaryItem(t("memoryWorkspace.writeRecords"), record.write_count ?? "-")}
    ${summaryItem(t("memoryWorkspace.pullStatus"), memoryStateLabel(memoryPullState(record)))}
    ${summaryItem(t("memoryWorkspace.fetchedAt"), record.fetched_at || "-")}
    ${summaryItem(t("memoryWorkspace.snapshotHash"), record.snapshot_sha256 || "-")}
  </dl>`;
}
function memoryRecordRows(records) {
  return records.map((record, index) => {
    const cloud = Boolean(memoryProvider(record.strategy));
    return `<tr><td><a href="/runs/${encodeURIComponent(record.run_id)}">${escapeHtml(record.run_id)}</a>
      <small class="memory-table-secondary">${escapeHtml(record.experiment_id || "")} · ${escapeHtml(record.group_id || "")}</small></td>
      <td>${escapeHtml(memoryStrategyLabel(record.strategy))}</td><td>${escapeHtml(record.agent_id || "-")}</td>
      <td>${escapeHtml(record.round_index ?? "-")}</td><td>${escapeHtml(record.account_id || "-")}</td>
      <td>${cloud ? escapeHtml(memoryStateLabel(memoryPullState(record))) : t("memoryWorkspace.localSnapshot")}
        <small class="memory-table-secondary">${record.total === null || record.total === undefined ? t("memoryWorkspace.unknownCount") : t("memoryWorkspace.itemCount", {count: formatNumber(record.total)})}</small></td>
      <td class="memory-row-actions"><button type="button" class="secondary compact" data-memory-view="${index}">${t(cloud ? "memoryWorkspace.record" : "memoryWorkspace.view")}</button>
        ${cloud ? `<button type="button" class="compact" data-memory-pull="${index}">${t("memoryWorkspace.pull")}</button>` : ""}</td></tr>`;
  }).join("");
}

async function renderMemoryWorkspace() {
  const params = new URLSearchParams(location.search);
  const limit = 50;
  let offset = 0;
  let nextOffset = limit;
  main.innerHTML = `<div class="page memory-workspace">${pageHeader(t("memoryWorkspace.title"), t("memoryWorkspace.help"), `<a class="button secondary" href="/memory-accounts">${t("memoryWorkspace.accounts")}</a>`)}
    <form id="memoryFilters" class="section form-grid memory-filter-grid">
      ${selectField("strategy", t("summary.memory"), params.get("strategy") || "", [["", t("memoryWorkspace.allStrategies")], ["langmem_official", "Langmem"], ["a_mem_official", "Amem"], ["hindsight_cloud", "Hindsight"], ["mem0_platform_v3", "Mem0"], ["memos_cloud", "MemOS"]])}
      ${textField("group_id", t("memoryWorkspace.group"), params.get("group_id") || "")}
      ${textField("run_id", t("validation.sourceRun"), params.get("run_id") || "")}
      ${textField("agent_id", t("validation.agent"), params.get("agent_id") || "")}
      ${numberField("round_index", t("validation.memoryRound"), params.get("round_index") || "", 0)}
      <div class="memory-filter-actions"><button type="submit">${t("memoryWorkspace.filter")}</button><button type="button" class="secondary" id="memoryRefresh">${t("memoryWorkspace.refresh")}</button></div>
    </form><section class="section"><div id="memoryRecords" aria-live="polite"></div>
      <div class="pager"><span id="memoryPage"></span><button class="secondary compact" id="memoryPrevious">${t("common.previous")}</button><button class="secondary compact" id="memoryNext">${t("common.next")}</button></div></section>
    <section class="section"><h2>${t("memoryWorkspace.jobs")}</h2><div id="memoryWorkspaceJobs"></div></section></div>`;
  const form = main.querySelector("#memoryFilters");
  const result = main.querySelector("#memoryRecords");
  const previous = main.querySelector("#memoryPrevious");
  const next = main.querySelector("#memoryNext");
  let request = 0;
  async function load() {
    const requestId = ++request;
    const query = new URLSearchParams({limit, offset});
    for (const name of ["strategy", "group_id", "run_id", "agent_id", "round_index"]) {
      if (form.elements[name].value.trim()) query.set(name, form.elements[name].value.trim());
    }
    result.innerHTML = emptyState(t("common.loading"));
    previous.disabled = next.disabled = true;
    try {
      const payload = await apiGet(`/api/platform/memories?${query}`);
      if (requestId !== request || !result.isConnected) return;
      const records = payload.items || payload.records || [];
      result.innerHTML = records.length ? `<div class="table-wrap"><table class="memory-record-table"><thead><tr>
        <th>${t("validation.sourceRun")}</th><th>${t("summary.memory")}</th><th>${t("validation.agent")}</th><th>${t("validation.memoryRound")}</th><th>${t("memoryWorkspace.account")}</th><th>${t("memoryWorkspace.pullStatus")}</th><th>${t("memoryWorkspace.actions")}</th>
        </tr></thead><tbody>${memoryRecordRows(records)}</tbody></table></div>` : emptyState(t("memoryWorkspace.noRecords"));
      result.querySelectorAll("[data-memory-view]").forEach((button) => button.addEventListener("click", () => openMemoryRecord(records[Number(button.dataset.memoryView)])));
      result.querySelectorAll("[data-memory-pull]").forEach((button) => button.addEventListener("click", () => openMemoryPull(records[Number(button.dataset.memoryPull)], load)));
      main.querySelector("#memoryPage").textContent = t("memoryWorkspace.page", {page: Math.floor(offset / limit) + 1, total: formatNumber(payload.total || 0)});
      previous.disabled = offset === 0;
      nextOffset = Number(payload.next_offset ?? offset + limit);
      next.disabled = payload.has_more !== undefined ? !payload.has_more : nextOffset >= Number(payload.total || 0);
      await refreshMemoryJobs(main.querySelector("#memoryWorkspaceJobs"), query.get("run_id") || "", load);
    } catch (error) {
      if (requestId === request && result.isConnected) result.innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;
    }
  }
  form.addEventListener("submit", (event) => {event.preventDefault(); offset = 0; load();});
  main.querySelector("#memoryRefresh").addEventListener("click", load);
  previous.addEventListener("click", () => {offset = Math.max(0, offset - limit); load();});
  next.addEventListener("click", () => {offset = nextOffset; load();});
  await load();
}

function memoryItemCanSync(item, provider) {
  if (!String(item.id || item.memory_id || "")) return false;
  if (provider !== "hindsight") return true;
  const kind = String(item.type || item.memory_type || item.details?.type || item.details?.memory_type || "").toLowerCase();
  return ["world", "experience"].includes(kind);
}
function memoryItemCard(item, selectable = false, selected = false, disabled = false) {
  const identifier = String(item.id || item.memory_id || "");
  const body = item.content ?? item.memory ?? item.text ?? "";
  return `<article class="memory-result-item">${selectable ? `<label class="memory-item-choice ${disabled ? "is-unavailable" : ""}"><input type="checkbox" data-memory-item="${escapeHtml(identifier)}" ${selected ? "checked" : ""} ${disabled ? "disabled" : ""}>${escapeHtml(identifier || "-")}</label>` : `<strong>${escapeHtml(identifier || "-")}</strong>`}
    ${item.atomic_note ? renderAmemNote(item.atomic_note) : `<pre>${escapeHtml(typeof body === "string" ? body : JSON.stringify(body, null, 2))}</pre>`}
    <details><summary>${t("memoryResult.metadata")}</summary><pre>${escapeHtml(JSON.stringify(item.details || item, null, 2))}</pre></details>
    ${(item.sources || []).length ? `<details><summary>${t("memoryResult.sources")}</summary><pre>${escapeHtml(JSON.stringify(item.sources, null, 2))}</pre></details>` : ""}
    ${(item.history || item.observed_versions || []).length ? `<details><summary>${t("memoryResult.history")}</summary><pre>${escapeHtml(JSON.stringify(item.history || item.observed_versions, null, 2))}</pre></details>` : ""}</article>`;
}
async function memoryRecordDetails(record, offset = 0) {
  return apiGet(`/api/platform/runs/${encodeURIComponent(record.run_id)}/memory-result?${memoryRecordParams(record, {limit: 100, offset, calls_limit: 1})}`);
}
async function openMemoryRecord(record) {
  record = {...record};
  const version = ++memoryWorkspaceDialogVersion;
  dialogTitle.innerHTML = `<h2>${t("memoryWorkspace.title")}</h2><small>${escapeHtml(record.run_id)} · ${escapeHtml(record.agent_id || "")}</small>`;
  dialogBody.innerHTML = emptyState(t("common.loading"));
  if (!detailDialog.open) detailDialog.showModal();
  const cloud = Boolean(memoryProvider(record.strategy));
  let offset = 0;
  async function load() {
    try {
      const data = await memoryRecordDetails(record, offset);
      if (!detailDialog.open || version !== memoryWorkspaceDialogVersion) return;
      const combined = {...record, ...data, strategy: data.strategy || record.strategy};
      const items = data.items || [];
      const state = memoryPullState(combined);
      dialogBody.innerHTML = `${Array.isArray(data.available_rounds) && data.available_rounds.length > 1 ? `<div class="form-field"><label for="memoryDetailRound">${t("validation.memoryRound")}</label><select id="memoryDetailRound">${data.available_rounds.map((round) => `<option value="${Number(round)}" ${Number(round) === Number(combined.round_index) ? "selected" : ""}>${Number(round)}</option>`).join("")}</select></div>` : ""}${memoryRecordMeta(combined)}
        ${cloud ? `<p class="validation-notice">${t("memoryWorkspace.cloudRecordHelp")}</p><div class="memory-toolbar"><button id="memoryPullDetail">${t("memoryWorkspace.pull")}</button>
          <button class="secondary" id="memorySyncDetail" ${state === "complete" && combined.account_id ? "" : "disabled"}>${t(memoryProvider(combined.strategy) === "memos" ? "memoryWorkspace.connectExisting" : "memoryWorkspace.sync")}</button></div>${state === "complete" && !combined.account_id ? `<p class="validation-notice">${t("memoryWorkspace.sourceAccountRequired")}</p>` : ""}` : ""}
        ${cloud && state === "not_fetched" ? emptyState(t("memoryWorkspace.notFetchedHelp")) : items.length ? `<h3>${t("memoryResult.finalMemories")} (${formatNumber(data.total || 0)})</h3><div class="memory-result-list">${items.map((item) => memoryItemCard(item)).join("")}</div>` : emptyState(t(cloud && state !== "complete" ? "memoryWorkspace.unavailableContent" : "memoryWorkspace.emptyInventory"))}
        ${data.error ? `<p class="error-text">${escapeHtml(data.error)}</p>` : ""}
        <div class="pager"><button class="secondary compact" id="memoryDetailPrevious" ${offset ? "" : "disabled"}>${t("common.previous")}</button><button class="secondary compact" id="memoryDetailNext" ${offset + items.length < Number(data.total || 0) ? "" : "disabled"}>${t("common.next")}</button></div>
        ${cloud ? `<h3>${t(memoryProvider(combined.strategy) === "memos" ? "memoryWorkspace.existingBindings" : "memoryWorkspace.replicas")}</h3><div id="memoryReplicaList"></div><h3>${t("memoryWorkspace.jobs")}</h3><div id="memoryDetailJobs"></div>` : ""}`;
      dialogBody.querySelector("#memoryDetailPrevious").addEventListener("click", () => {offset = Math.max(0, offset - 100); load();});
      dialogBody.querySelector("#memoryDetailNext").addEventListener("click", () => {offset += 100; load();});
      dialogBody.querySelector("#memoryDetailRound")?.addEventListener("change", (event) => {record.round_index = Number(event.currentTarget.value); offset = 0; load();});
      dialogBody.querySelector("#memoryPullDetail")?.addEventListener("click", () => openMemoryPull(combined, () => openMemoryRecord(combined)));
      dialogBody.querySelector("#memorySyncDetail")?.addEventListener("click", () => openMemorySync(combined));
      if (cloud) {
        const replicas = await apiGet(`/api/platform/memory-replicas?${new URLSearchParams({source_run_id: record.run_id, agent_id: record.agent_id})}`);
        if (!detailDialog.open || version !== memoryWorkspaceDialogVersion) return;
        const target = dialogBody.querySelector("#memoryReplicaList");
        if (target) target.innerHTML = memoryReplicaRows(replicas.replicas || []);
        await refreshMemoryJobs(dialogBody.querySelector("#memoryDetailJobs"), record.run_id, load);
      }
    } catch (error) { if (detailDialog.open && version === memoryWorkspaceDialogVersion) dialogBody.innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`; }
  }
  await load();
}
function memoryReplicaRows(replicas) {
  return replicas.length ? `<ul class="memory-replica-list">${replicas.map((replica) => `<li><strong>${escapeHtml(replica.account_id || "-")}</strong> · ${escapeHtml(memoryStateLabel(replica.status))} · ${replica.verified ? t("memoryWorkspace.verified") : t("memoryWorkspace.unverified")} · ${t(replica.binding_mode === "shared_scope" ? "memoryWorkspace.sharedScopeBinding" : replica.copied === false || replica.binding_mode === "verified_existing_scope" ? "memoryWorkspace.existingScopeBinding" : replica.complete === true ? "memoryWorkspace.fullCopy" : "memoryWorkspace.partialCopy")}<small>${escapeHtml(replica.snapshot_sha256 || replica.content_sha256 || "")}</small></li>`).join("")}</ul>` : emptyState(t("memoryWorkspace.noReplicas"));
}
function memoryAccountChoices(accounts, provider, selected = "", exclude = "") {
  return accounts.filter((account) => memoryProvider(account.provider || account.strategy) === provider && account.enabled !== false && memoryAccountId(account) !== exclude)
    .map((account) => `<option value="${escapeHtml(memoryAccountId(account))}" ${memoryAccountId(account) === selected ? "selected" : ""}>${escapeHtml(memoryAccountName(account))} · ${escapeHtml(memoryStateLabel(account.status || "active"))}</option>`).join("");
}
async function openMemoryPull(record, after = () => {}) {
  const version = ++memoryWorkspaceDialogVersion;
  const wasOpen = detailDialog.open;
  try {
    const {accounts = []} = await apiGet("/api/platform/memory-accounts");
    if (version !== memoryWorkspaceDialogVersion || (wasOpen && !detailDialog.open)) return;
    dialogTitle.innerHTML = `<h2>${t("memoryWorkspace.pull")}</h2>`;
    dialogBody.innerHTML = `<form id="memoryPullForm">${memoryRecordMeta(record)}<p>${t("memoryWorkspace.pullHelp")}</p>
      <div class="form-field"><label for="memoryPullAccount">${t("memoryWorkspace.sourceAccount")}</label><select id="memoryPullAccount" name="account_id" required><option value="">${t("memoryWorkspace.chooseAccount")}</option>${memoryAccountChoices(accounts, memoryProvider(record.strategy), record.account_id)}</select></div>
      <p><a href="/memory-accounts">${t("memoryWorkspace.accounts")}</a></p><p id="memoryPullError" class="error-text" role="alert"></p><button type="submit">${t("memoryWorkspace.startPull")}</button></form>`;
    if (!detailDialog.open) detailDialog.showModal();
    const form = dialogBody.querySelector("form");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        const {job} = await apiPost("/api/platform/memories/pull", {...memoryRecordPayload(record), account_id: form.elements.account_id.value});
        if (version !== memoryWorkspaceDialogVersion || !detailDialog.open) return;
        await showMemoryJob(job, after);
      } catch (error) {form.querySelector("#memoryPullError").textContent = error.message; button.disabled = false;}
    });
  } catch (error) {showToast(error.message);}
}
async function openMemorySync(record) {
  if (memoryProvider(record.strategy) === "memos") return openMemoryConnectExisting(record);
  const version = ++memoryWorkspaceDialogVersion;
  dialogTitle.innerHTML = `<h2>${t("memoryWorkspace.sync")}</h2>`;
  dialogBody.innerHTML = emptyState(t("common.loading"));
  if (!detailDialog.open) detailDialog.showModal();
  try {
    const [{accounts = []}, first] = await Promise.all([apiGet("/api/platform/memory-accounts"), memoryRecordDetails(record)]);
    if (version !== memoryWorkspaceDialogVersion || !detailDialog.open) return;
    if (memoryPullState({...record, ...first}) !== "complete") throw new Error(t("memoryWorkspace.completePullRequired"));
    if (!(record.account_id || first.account_id)) throw new Error(t("memoryWorkspace.sourceAccountRequired"));
    const targets = accounts.filter((account) => memoryProvider(account.provider || account.strategy) === memoryProvider(record.strategy) && !memoryAccountIsDefault(account) && account.enabled !== false && memoryAccountId(account) !== record.account_id);
    let items = first.items || [];
    const picked = new Set();
    dialogBody.innerHTML = `<form id="memorySyncForm">${memoryRecordMeta(record)}<p class="validation-notice">${t("memoryWorkspace.syncHelp")}</p>${memoryProvider(record.strategy) === "hindsight" ? `<p class="validation-notice">${t("memoryWorkspace.hindsightSelectionHelp")}</p>` : ""}
      <fieldset class="memory-sync-targets"><legend>${t("memoryWorkspace.targetAccounts")}</legend>${targets.map((account) => `<label class="memory-item-choice"><input type="checkbox" name="target_account_id" value="${escapeHtml(memoryAccountId(account))}">${escapeHtml(memoryAccountName(account))} · ${escapeHtml(memoryStateLabel(account.status || "active"))}</label>`).join("") || `<p>${t("memoryWorkspace.noTargetAccounts")}</p>`}</fieldset>
      <fieldset class="memory-sync-selection"><legend>${t("memoryWorkspace.memoriesToSync")}</legend>
        <label class="memory-item-choice"><input name="selection_mode" type="radio" value="all" checked>${t("memoryWorkspace.allMemories", {count: formatNumber(first.total || 0)})}</label>
        <label class="memory-item-choice"><input name="selection_mode" type="radio" value="selected">${t("memoryWorkspace.selectedMemories")}</label>
        <div id="memorySyncItems" class="memory-result-list memory-sync-items"></div><button class="secondary compact" type="button" id="memorySyncMore">${t("memoryResult.more")}</button><p id="memorySelectedCount" role="status"></p></fieldset>
      <p id="memorySyncError" class="error-text" role="alert"></p><button type="submit">${t("memoryWorkspace.startSync")}</button></form>`;
    const form = dialogBody.querySelector("form");
    function drawItems() {
      const target = form.querySelector("#memorySyncItems");
      target.innerHTML = items.map((item) => memoryItemCard(item, true, picked.has(String(item.id || item.memory_id)), !memoryItemCanSync(item, memoryProvider(record.strategy)))).join("");
      target.querySelectorAll("[data-memory-item]").forEach((input) => input.addEventListener("change", () => {
        if (input.checked) picked.add(input.dataset.memoryItem); else picked.delete(input.dataset.memoryItem);
        form.querySelector('[name="selection_mode"][value="selected"]').checked = true;
        form.querySelector("#memorySelectedCount").textContent = t("memoryWorkspace.selectedCount", {count: picked.size});
      }));
      form.querySelector("#memorySyncMore").hidden = items.length >= Number(first.total || 0);
    }
    drawItems();
    form.querySelector("#memorySyncMore").addEventListener("click", async (event) => {
      event.currentTarget.disabled = true;
      try { const page = await memoryRecordDetails(record, items.length); items.push(...(page.items || [])); drawItems(); }
      catch (error) {form.querySelector("#memorySyncError").textContent = error.message;}
      finally {form.querySelector("#memorySyncMore").disabled = false;}
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const error = form.querySelector("#memorySyncError");
      const selectedTargets = [...form.querySelectorAll('[name="target_account_id"]:checked')].map((input) => input.value);
      const selectedOnly = form.querySelector('[name="selection_mode"]:checked').value === "selected";
      if (!selectedTargets.length || (selectedOnly && !picked.size)) {error.textContent = t("memoryWorkspace.syncSelectionRequired"); return;}
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        const {job} = await apiPost("/api/platform/memories/sync", {...memoryRecordPayload(record), source_account_id: record.account_id || first.account_id,
          target_account_ids: selectedTargets, ...(selectedOnly ? {selected_ids: [...picked]} : {})});
        if (version !== memoryWorkspaceDialogVersion || !detailDialog.open) return;
        await showMemoryJob(job, () => openMemoryRecord(record));
      } catch (failure) {error.textContent = failure.message; button.disabled = false;}
    });
  } catch (error) {if (version === memoryWorkspaceDialogVersion && detailDialog.open) dialogBody.innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;}
}

function memoryConnectExistingMarkup(record, accounts) {
  const targets = accounts.filter((account) => memoryProvider(account.provider || account.strategy) === "memos"
    && !memoryAccountIsDefault(account) && account.enabled !== false && memoryAccountHasKey(account));
  const scope = typeof record.scope === "object" ? record.scope?.user_id || "" : record.scope || "";
  return `<form id="memoryConnectExistingForm">${memoryRecordMeta(record)}<p class="validation-notice">${t("memoryWorkspace.connectExistingHelp")}</p>
    <div class="form-field"><label for="memoryConnectAccount">${t("memoryWorkspace.targetAccounts")}</label><select id="memoryConnectAccount" name="account_id" required><option value="">${t("memoryWorkspace.chooseAccount")}</option>${memoryAccountChoices(targets, "memos")}</select></div>
    ${textField("physical_scope", "user_id", scope, t("memoryWorkspace.existingUserIdHelp"), "required")}
    <p id="memoryConnectError" class="error-text" role="alert"></p><button type="submit" ${targets.length ? "" : "disabled"}>${t("memoryWorkspace.connectExisting")}</button></form>`;
}

async function openMemoryConnectExisting(record) {
  const version = ++memoryWorkspaceDialogVersion;
  dialogTitle.innerHTML = `<h2>${t("memoryWorkspace.connectExisting")}</h2>`;
  dialogBody.innerHTML = emptyState(t("common.loading"));
  if (!detailDialog.open) detailDialog.showModal();
  try {
    const {accounts = []} = await apiGet("/api/platform/memory-accounts");
    if (version !== memoryWorkspaceDialogVersion || !detailDialog.open) return;
    dialogBody.innerHTML = memoryConnectExistingMarkup(record, accounts);
    const form = dialogBody.querySelector("form");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        const {job} = await apiPost("/api/platform/memories/connect-existing", {...memoryRecordPayload(record),
          account_id: form.elements.account_id.value, physical_scope: form.elements.physical_scope.value.trim()});
        if (version !== memoryWorkspaceDialogVersion || !detailDialog.open) return;
        await showMemoryJob(job, () => openMemoryRecord(record));
      } catch (error) {form.querySelector("#memoryConnectError").textContent = error.message; button.disabled = false;}
    });
  } catch (error) {if (version === memoryWorkspaceDialogVersion && detailDialog.open) dialogBody.innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;}
}

function memoryJobActive(job) { return ["queued", "running", "starting", "pending", "verifying"].includes(job.status); }
function memoryJobId(job) { return String(job.job_id || job.id || ""); }
function memoryJobMarkup(job) {
  const id = memoryJobId(job);
  const historyOnly = job.history_only || job.copied_from_job_id;
  const targets = job.targets || job.target_results || [];
  const rows = Array.isArray(targets) ? targets : Object.entries(targets).map(([account_id, value]) => ({account_id, ...value}));
  return `<article class="memory-job-card"><strong>${escapeHtml(id)}</strong> <span class="tag">${escapeHtml(memoryStateLabel(job.status))}</span>
    <p>${escapeHtml(job.kind || job.type || "")} · ${escapeHtml(job.run_id || job.source_run_id || "")} · ${escapeHtml(job.agent_id || "")}</p>
    ${job.total_count !== undefined || job.total !== undefined ? `<p>${t("memoryWorkspace.progress", {completed: formatNumber(job.completed_count ?? job.completed ?? 0), total: formatNumber(job.total_count ?? job.total)})}</p>` : ""}
    ${job.error ? `<p class="error-text">${escapeHtml(memoryErrorText(job.error))}</p>` : ""}
    ${rows.length ? `<ul>${rows.map((target) => `<li>${escapeHtml(target.account_id)} · ${escapeHtml(memoryStateLabel(target.status))}${target.verified ? ` · ${t("memoryWorkspace.verified")}` : ""}${target.error ? `<p class="error-text">${escapeHtml(memoryErrorText(target.error))}</p>` : ""}</li>`).join("")}</ul>` : ""}
    <div class="memory-toolbar">${historyOnly ? `<span class="muted">${t("memoryWorkspace.copiedHistory")}</span>` : memoryJobActive(job) ? `<button class="secondary compact" data-memory-job-cancel="${escapeHtml(id)}">${t("memoryWorkspace.cancelJob")}</button>` : ["failed", "partial", "cancelled"].includes(job.status) ? `<button class="secondary compact" data-memory-job-retry="${escapeHtml(id)}">${t("common.retry")}</button>` : ""}</div></article>`;
}
function bindMemoryJobActions(target, refresh) {
  for (const action of ["retry", "cancel"]) target.querySelectorAll(`[data-memory-job-${action}]`).forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {await apiPost(`/api/platform/memory-jobs/${encodeURIComponent(button.getAttribute(`data-memory-job-${action}`))}/${action}`, {}); await refresh();}
    catch (error) {showToast(error.message); button.disabled = false;}
  }));
}
async function refreshMemoryJobs(target, runId = "", after = () => {}) {
  if (!target || !target.isConnected) return;
  clearTimeout(memoryWorkspaceTimer);
  try {
    const {jobs = []} = await apiGet(`/api/platform/memory-jobs${runId ? `?run_id=${encodeURIComponent(runId)}` : ""}`);
    if (!target.isConnected) return;
    const wasActive = target.memoryHadActiveJobs === true;
    target.memoryHadActiveJobs = jobs.some(memoryJobActive);
    target.innerHTML = jobs.length ? jobs.map(memoryJobMarkup).join("") : emptyState(t("memoryWorkspace.noJobs"));
    bindMemoryJobActions(target, () => refreshMemoryJobs(target, runId, after));
    if (jobs.some(memoryJobActive)) memoryWorkspaceTimer = setTimeout(() => refreshMemoryJobs(target, runId, after), 2500);
    else if (wasActive) await after();
  } catch (error) {if (target.isConnected) target.innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;}
}
async function showMemoryJob(job, after = () => {}) {
  ++memoryWorkspaceDialogVersion;
  clearTimeout(memoryWorkspaceTimer);
  dialogTitle.innerHTML = `<h2>${t("memoryWorkspace.jobs")}</h2>`;
  dialogBody.innerHTML = `<div id="memoryActiveJob"></div><button class="secondary" id="memoryJobBack">${t("memoryWorkspace.backToMemory")}</button>`;
  dialogBody.querySelector("#memoryJobBack").addEventListener("click", () => {detailDialog.close(); after();});
  const target = dialogBody.querySelector("#memoryActiveJob");
  let wasActive = memoryJobActive(job);
  async function refresh(fetch = true) {
    try {
      if (fetch) { const payload = await apiGet(`/api/platform/memory-jobs/${encodeURIComponent(memoryJobId(job))}`); job = payload.job || payload; }
      if (!target.isConnected || !detailDialog.open) return;
      target.innerHTML = memoryJobMarkup(job);
      bindMemoryJobActions(target, refresh);
      if (memoryJobActive(job)) memoryWorkspaceTimer = setTimeout(refresh, 2000);
      else if (wasActive) {wasActive = false; showToast(memoryStateLabel(job.status));}
    } catch (error) {if (target.isConnected) target.innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;}
  }
  await refresh(false);
}

function memoryAccountCards(accounts) {
  return accounts.map((account, index) => `<article class="memory-account-card"><header><h3>${escapeHtml(memoryAccountName(account))}</h3><span class="tag">${escapeHtml(memoryStateLabel(account.enabled === false ? "disabled" : account.status || "active"))}</span></header>
    <p class="memory-account-role">${t(memoryAccountIsDefault(account) ? "memoryWorkspace.defaultRole" : "memoryWorkspace.experimentRole")}</p>
    <dl class="summary-list">${summaryItem(t("memoryWorkspace.account"), memoryAccountId(account))}<div><dt>${escapeHtml(t("memoryWorkspace.apiKey"))}</dt><dd class="memory-account-key">${escapeHtml(memoryAccountDisplayKey(account))}</dd></div></dl>
    <p class="muted">${t("memoryWorkspace.quotaUnknown")}</p>${account.last_error ? `<p class="error-text">${escapeHtml(memoryErrorText(account.last_error))}</p>` : ""}
    <div class="memory-toolbar"><button class="secondary compact" data-memory-account-edit="${index}">${t(memoryAccountIsDefault(account) && !memoryAccountHasKey(account) ? "memoryWorkspace.setupDefaultKey" : "resource.edit")}</button><button class="secondary compact" data-memory-account-check="${index}" ${memoryAccountHasKey(account) ? "" : "disabled"}>${t("memoryWorkspace.check")}</button><button class="secondary compact" data-memory-account-toggle="${index}">${t(account.enabled === false ? "memoryWorkspace.enable" : "memoryWorkspace.disable")}</button>${["quota_exhausted", "cooldown", "disabled"].includes(account.status) ? `<button class="secondary compact" data-memory-account-reset="${index}">${t("memoryWorkspace.resetStatus")}</button>` : ""}${!memoryAccountIsDefault(account) ? `<button class="secondary compact" data-memory-account-inventory="${index}">${t("memoryWorkspace.accountExperiments")}</button>` : ""}</div></article>`).join("");
}

function memoryInventoryExperiments(items) {
  const groups = new Map();
  for (const item of items) {
    const id = item.experiment_id || item.run_id;
    if (!groups.has(id)) groups.set(id, {id, name: item.experiment_name || item.run_name || id, items: []});
    groups.get(id).items.push(item);
  }
  return [...groups.values()].map((group) => ({...group,
    importable: group.items.some((item) => item.importable === true),
    importableCount: group.items.filter((item) => item.importable === true).length}));
}

function memoryInventoryRows(groups, selectable = false, existingScopes = false) {
  return groups.map((group, index) => `<article class="memory-experiment-inventory">
    ${selectable ? `<label class="memory-item-choice ${group.importable ? "" : "is-unavailable"}"><input type="checkbox" name="memory_experiment" value="${index}" ${group.importable ? "" : "disabled"}>${escapeHtml(group.name)} <small>${escapeHtml(group.id)}</small></label>` : `<h3>${escapeHtml(group.name)} <small>${escapeHtml(group.id)}</small></h3>`}
    ${selectable && !group.importable ? `<p class="muted">${t("memoryWorkspace.experimentUnavailable")}</p>` : ""}
    ${selectable && group.importable && group.importableCount < group.items.length ? `<p class="validation-notice">${t("memoryWorkspace.partialExperimentHelp", {count: group.importableCount})}</p>` : ""}
    <details><summary>${t("memoryWorkspace.experimentSources", {count: group.items.length})}</summary>
    <ul>${group.items.map((item) => `<li><a href="/runs/${encodeURIComponent(item.run_id)}">${escapeHtml(item.run_id)}</a> · ${escapeHtml(item.agent_id || "-")} · ${t("validation.memoryRound")} ${escapeHtml(item.round_index ?? "-")} · ${item.total === null || item.total === undefined ? t("memoryWorkspace.unknownCount") : t("memoryWorkspace.itemCount", {count: formatNumber(item.total)})}
      ${selectable ? ` · ${t(item.importable === true ? "memoryWorkspace.sourceReady" : "memoryWorkspace.sourceExcluded")}` : ` · ${t(item.available_for_stage_two === true ? "memoryWorkspace.availableForStageTwo" : existingScopes ? "memoryWorkspace.noVerifiedBinding" : "memoryWorkspace.noVerifiedCopy")}`}</li>`).join("")}</ul></details></article>`).join("");
}

function memoryInventoryImportPayload(form, groups) {
  const chosen = [...form.querySelectorAll('[name="memory_experiment"]:checked')].map((input) => groups[Number(input.value)]);
  const sources = new Map();
  for (const group of chosen) {
    if (!group?.importable) continue;
    for (const record of group.items) {
      if (record.importable !== true) continue;
      const source = memoryRecordPayload(record);
      sources.set(JSON.stringify(source), source);
    }
  }
  return {sources: [...sources.values()], target_account_ids: [...form.querySelectorAll('[name="target_account_id"]:checked')].map((input) => input.value)};
}

async function renderMemoryAccountInventory(account, accounts, target, after = () => {}) {
  target.innerHTML = emptyState(t("common.loading"));
  const data = await apiGet(`/api/platform/memory-accounts/${encodeURIComponent(memoryAccountId(account))}/memories`);
  if (!target.isConnected) return;
  account = {...account, ...data.account};
  const groups = memoryInventoryExperiments(data.items || []);
  const memos = memoryProvider(account.provider || account.strategy) === "memos";
  const selectable = memoryAccountIsDefault(account) && !memos;
  const targets = accounts.filter((candidate) => !memoryAccountIsDefault(candidate) && candidate.enabled !== false && memoryAccountHasKey(candidate)
    && memoryProvider(candidate.provider || candidate.strategy) === memoryProvider(account.provider || account.strategy));
  target.innerHTML = `<h3>${t("memoryWorkspace.accountExperiments")}</h3><p class="muted">${t(memos ? "memoryWorkspace.memosPoolHelp" : selectable ? "memoryWorkspace.defaultInventoryHelp" : "memoryWorkspace.accountInventoryHelp")}</p>
    ${memos ? `<p class="validation-notice">${t("memoryWorkspace.connectExistingHelp")}</p>` : ""}
    ${selectable && !memoryAccountHasKey(account) ? `<p class="validation-notice">${t("memoryWorkspace.defaultKeyRequired")}</p>` : ""}
    ${!groups.length ? emptyState(t("memoryWorkspace.noAccountExperiments")) : selectable ? `<form class="memory-account-import-form">
      <div class="memory-experiment-inventory-list">${memoryInventoryRows(groups, true)}</div>
      <fieldset class="memory-sync-targets"><legend>${t("memoryWorkspace.targetAccounts")}</legend>${targets.map((candidate) => `<label class="memory-item-choice"><input type="checkbox" name="target_account_id" value="${escapeHtml(memoryAccountId(candidate))}">${escapeHtml(memoryAccountName(candidate))}</label>`).join("") || `<p>${t("memoryWorkspace.noTargetAccounts")}</p>`}</fieldset>
      <p class="memory-account-import-error error-text" role="alert"></p><button type="submit" ${targets.length && account.enabled !== false && memoryAccountHasKey(account) ? "" : "disabled"}>${t("memoryWorkspace.importExperiments")}</button></form>` : `<div class="memory-experiment-inventory-list">${memoryInventoryRows(groups, false, memos)}</div>`}`;
  if (memos) {
    const sources = groups.flatMap((group) => group.items);
    target.insertAdjacentHTML("beforeend", sources.map((record, index) => `<div class="memory-toolbar"><span>${escapeHtml(record.run_id)} · ${escapeHtml(record.agent_id || "")}</span><button type="button" class="secondary compact" data-memory-connect-existing="${index}">${t("memoryWorkspace.connectExisting")}</button></div>`).join(""));
    target.querySelectorAll("[data-memory-connect-existing]").forEach((button) => button.addEventListener("click", () => openMemoryConnectExisting({...sources[Number(button.dataset.memoryConnectExisting)], strategy: "memos_cloud"})));
  }
  const form = target.querySelector("form");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = memoryInventoryImportPayload(form, groups);
    const error = form.querySelector(".memory-account-import-error");
    if (!payload.sources.length || !payload.target_account_ids.length) {error.textContent = t("memoryWorkspace.importSelectionRequired"); return;}
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    error.textContent = "";
    try {
      const {jobs = []} = await apiPost(`/api/platform/memory-accounts/${encodeURIComponent(memoryAccountId(account))}/import`, payload);
      await showMemoryImportJobs(jobs, after);
    } catch (failure) {error.textContent = failure.message; button.disabled = false;}
  });
}

async function openMemoryAccountInventory(account, accounts) {
  const version = ++memoryWorkspaceDialogVersion;
  dialogTitle.innerHTML = `<h2>${escapeHtml(memoryAccountName(account))} · ${t("memoryWorkspace.accountExperiments")}</h2>`;
  dialogBody.innerHTML = '<div id="memoryAccountInventoryDetail"></div>';
  if (!detailDialog.open) detailDialog.showModal();
  const target = dialogBody.querySelector("#memoryAccountInventoryDetail");
  try {await renderMemoryAccountInventory(account, accounts, target);}
  catch (error) {if (version === memoryWorkspaceDialogVersion && detailDialog.open) target.innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;}
}

async function showMemoryImportJobs(jobs, after) {
  ++memoryWorkspaceDialogVersion;
  clearTimeout(memoryWorkspaceTimer);
  dialogTitle.innerHTML = `<h2>${t("memoryWorkspace.jobs")}</h2>`;
  dialogBody.innerHTML = `<div id="memoryImportJobs"></div><button class="secondary" id="memoryImportJobsBack">${t("memoryWorkspace.backToAccounts")}</button>`;
  if (!detailDialog.open) detailDialog.showModal();
  const target = dialogBody.querySelector("#memoryImportJobs");
  const ids = new Set(jobs.map(memoryJobId));
  const load = async () => {
    if (!target.isConnected || !detailDialog.open) return;
    try {
      const payload = await apiGet("/api/platform/memory-jobs");
      if (!target.isConnected || !detailDialog.open) return;
      jobs = (payload.jobs || []).filter((job) => ids.has(memoryJobId(job)));
      target.innerHTML = jobs.map(memoryJobMarkup).join("") || emptyState(t("memoryWorkspace.noJobs"));
      bindMemoryJobActions(target, load);
      if (jobs.some(memoryJobActive)) memoryWorkspaceTimer = setTimeout(load, 2500);
    } catch (error) {if (target.isConnected) target.innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;}
  };
  target.innerHTML = jobs.map(memoryJobMarkup).join("");
  bindMemoryJobActions(target, load);
  if (jobs.some(memoryJobActive)) memoryWorkspaceTimer = setTimeout(load, 2500);
  dialogBody.querySelector("#memoryImportJobsBack").addEventListener("click", () => {clearTimeout(memoryWorkspaceTimer); detailDialog.close(); after();});
}

async function renderMemoryAccounts() {
  main.innerHTML = `<div class="page memory-workspace">${pageHeader(t("memoryWorkspace.accounts"), t("memoryWorkspace.accountsHelp"), `<a class="button secondary" href="/memories">${t("memoryWorkspace.title")}</a>`)}<div id="memoryAccountList"></div></div>`;
  const target = main.querySelector("#memoryAccountList");
  async function load() {
    if (!target.isConnected) return;
    try {
      const [{accounts: listedAccounts = [], next_account_names = {}}, {accounts: displayKeys = []}] = await Promise.all([
        apiGet("/api/platform/memory-accounts"),
        apiPost("/api/platform/memory-accounts/display-keys", {}),
      ]);
      if (!target.isConnected) return;
      const keyByAccount = new Map(displayKeys.map((account) => [memoryAccountId(account), account.api_key]));
      const accounts = listedAccounts.map((account) => ({...account, api_key: keyByAccount.get(memoryAccountId(account))}));
      target.innerHTML = ["hindsight", "mem0", "memos"].map((provider) => {
        const group = accounts.filter((account) => memoryProvider(account.provider || account.strategy) === provider);
        const defaults = group.filter(memoryAccountIsDefault);
        return `<section class="section" data-memory-provider="${provider}"><div class="section-heading"><h2>${memoryProviderName(provider)}</h2><button data-memory-account-add="${provider}">${t("memoryWorkspace.addAccount")}</button></div><p class="muted">${t(provider === "memos" ? "memoryWorkspace.connectExistingHelp" : "memoryWorkspace.sharedAccountsHelp")}</p><div class="memory-account-grid">${memoryAccountCards(group) || emptyState(t("memoryWorkspace.noAccounts"))}</div>${defaults.map((account) => `<div class="memory-default-inventory" data-memory-default-inventory="${escapeHtml(memoryAccountId(account))}"></div>`).join("")}</section>`;
      }).join("");
      target.querySelectorAll("[data-memory-account-add]").forEach((button) => button.addEventListener("click", () => {
        const provider = button.dataset.memoryAccountAdd;
        const canonical = ({mem0: "mem0_platform_v3", hindsight: "hindsight_cloud", memos: "memos_cloud"})[provider];
        openMemoryAccountEditor({provider, name: next_account_names[canonical] || next_account_names[provider] || ""}, load);
      }));
      for (const section of target.querySelectorAll("section")) {
        const provider = section.querySelector("[data-memory-account-add]").dataset.memoryAccountAdd;
        const group = accounts.filter((account) => memoryProvider(account.provider || account.strategy) === provider);
        section.querySelectorAll("[data-memory-account-edit]").forEach((button) => button.addEventListener("click", () => openMemoryAccountEditor(group[Number(button.dataset.memoryAccountEdit)], load)));
        section.querySelectorAll("[data-memory-account-inventory]").forEach((button) => button.addEventListener("click", () => openMemoryAccountInventory(group[Number(button.dataset.memoryAccountInventory)], accounts)));
        for (const action of ["check", "toggle", "reset"]) section.querySelectorAll(`[data-memory-account-${action}]`).forEach((button) => button.addEventListener("click", async () => {
          const account = group[Number(button.getAttribute(`data-memory-account-${action}`))];
          button.disabled = true;
          try {
            await apiPost(`/api/platform/memory-accounts/${encodeURIComponent(memoryAccountId(account))}${action === "check" ? "/check" : ""}`, action === "check" ? {} : action === "reset" ? {status: "active", enabled: true} : {enabled: account.enabled === false});
            if (action === "check") showToast(t("memoryWorkspace.checkFinished"));
            await load();
          } catch (error) {showToast(error.message); button.disabled = false;}
        }));
      }
      await Promise.all([...target.querySelectorAll("[data-memory-default-inventory]")].map(async (inventory) => {
        const account = accounts.find((candidate) => memoryAccountId(candidate) === inventory.dataset.memoryDefaultInventory);
        try {await renderMemoryAccountInventory(account, accounts, inventory, load);}
        catch (error) {if (inventory.isConnected) inventory.innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;}
      }));
    } catch (error) {target.innerHTML = `<p class="error-text">${escapeHtml(error.message)}</p>`;}
  }
  await load();
}
function memoryAccountEditorMarkup(account) {
  const existing = Boolean(memoryAccountId(account));
  return `<form id="memoryAccountForm" autocomplete="off"><div class="form-grid">
    ${memoryAccountIsDefault(account) ? `<div class="wide"><p>${t("memoryWorkspace.defaultAccount")} · ${t("memoryWorkspace.defaultRole")}</p><p class="muted">${t("memoryWorkspace.defaultKeyHelp")}</p></div>` : textField("name", t("memoryWorkspace.name"), account.name || "", t("memoryWorkspace.accountNameHelp"), 'maxlength="120"')}
    <div class="form-field"><label for="memoryAccountKey">${t("memoryWorkspace.apiKey")}</label><input id="memoryAccountKey" name="api_key" type="password" autocomplete="new-password" spellcheck="false" ${existing && memoryAccountHasKey(account) ? "" : "required"}><small>${t(existing && memoryAccountHasKey(account) ? "memoryWorkspace.keepKey" : "memoryWorkspace.keyHelp")}</small></div>
    </div><p id="memoryAccountError" class="error-text" role="alert"></p><button type="submit">${t("memoryWorkspace.save")}</button></form>`;
}
function memoryAccountPayload(form, account) {
  const key = form.elements.api_key.value.trim();
  return {provider: memoryProvider(account.provider || account.strategy), ...(!memoryAccountIsDefault(account) ? {name: form.elements.name.value.trim()} : {}), ...(key ? {api_key: key} : {})};
}
function openMemoryAccountEditor(account, after) {
  ++memoryWorkspaceDialogVersion;
  dialogTitle.innerHTML = `<h2>${t(memoryAccountId(account) ? "resource.edit" : "memoryWorkspace.addAccount")} · ${memoryProviderName(account.provider || account.strategy)}</h2>`;
  dialogBody.innerHTML = memoryAccountEditorMarkup(account);
  if (!detailDialog.open) detailDialog.showModal();
  const form = dialogBody.querySelector("form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    try {
      const payload = memoryAccountPayload(form, account);
      await apiPost(`/api/platform/memory-accounts${memoryAccountId(account) ? `/${encodeURIComponent(memoryAccountId(account))}` : ""}`, payload);
      form.elements.api_key.value = "";
      detailDialog.close();
      dialogBody.replaceChildren();
      showToast(t("memoryWorkspace.accountSaved"));
      await after();
    } catch (error) {form.querySelector("#memoryAccountError").textContent = error.message; button.disabled = false;}
  });
  detailDialog.addEventListener("close", () => {form.elements.api_key.value = "";}, {once: true});
}

function renderCloudRunMemoryResult(target, data, runId) {
  const records = data.records?.length ? data.records : [{...data, run_id: runId}];
  target.innerHTML = `<p class="validation-notice">${t("memoryWorkspace.cloudRecordHelp")}</p>` + records.map((record, index) =>
    `<article class="memory-result-item">${memoryRecordMeta({...record, run_id: runId})}
      <div class="memory-toolbar"><button class="secondary compact" data-run-memory-view="${index}">${t("memoryWorkspace.record")}</button><button class="compact" data-run-memory-pull="${index}">${t("memoryWorkspace.pull")}</button></div></article>`).join("");
  target.querySelectorAll("[data-run-memory-view]").forEach((button) => button.addEventListener("click", () => openMemoryRecord({...records[Number(button.dataset.runMemoryView)], run_id: runId})));
  target.querySelectorAll("[data-run-memory-pull]").forEach((button) => button.addEventListener("click", () => openMemoryPull({...records[Number(button.dataset.runMemoryPull)], run_id: runId}, () => loadMemoryResult(runId))));
}

function bindMemoryAccountPool(form, config) {
  const field = form.querySelector("#memoryAccountPoolFields");
  if (!field) return;
  form.memoryAccountPoolState = {key: "", request: 0};
  field.querySelector("#memoryAccountPoolRefresh").addEventListener("click", () => {
    form.memoryAccountPoolState.key = "";
    syncMemoryAccountPool(form, form.memorySourceState?.selected, form.elements["validation.source_agent_id"].value);
  });
}

function memoryPoolEligibleReplicas(replicas, source, agentId) {
  return replicas.filter((replica) => !memoryAccountIsDefault(replica) && replica.verified === true && replica.complete === true
    && Boolean(source.snapshot_sha256) && replica.snapshot_sha256 === source.snapshot_sha256
    && (!replica.source_run_id || replica.source_run_id === source.run_id)
    && (!replica.agent_id || replica.agent_id === agentId)
    && (replica.round_index === undefined || replica.round_index === null || replica.round_index === source.source_round)
    && !["failed", "partial", "cancelled"].includes(replica.status));
}

async function syncMemoryAccountPool(form, source, agentId) {
  const field = form.querySelector("#memoryAccountPoolFields");
  const state = form.memoryAccountPoolState;
  if (!field || !state) return;
  const cloud = Boolean(memoryProvider(source?.memory_strategy));
  const memos = memoryProvider(source?.memory_strategy) === "memos";
  field.hidden = !cloud;
  const help = field.querySelector("#memoryAccountPoolHelp");
  if (help) help.textContent = t(memos ? "memoryWorkspace.memosPoolHelp" : "memoryWorkspace.poolHelp");
  if (!cloud || !source?.run_id || !agentId) return;
  const key = JSON.stringify([source.run_id, source.source_round, agentId]);
  if (key === state.key) return;
  state.key = key;
  const request = ++state.request;
  const target = field.querySelector("#memoryAccountPoolChoices");
  const status = field.querySelector("#memoryAccountPoolStatus");
  status.textContent = t("memoryWorkspace.loadingReplicas");
  target.replaceChildren();
  try {
    const [{accounts = []}, {replicas = []}, details] = await Promise.all([
      apiGet("/api/platform/memory-accounts"),
      apiGet(`/api/platform/memory-replicas?${new URLSearchParams({source_run_id: source.run_id, agent_id: agentId})}`),
      apiGet(`/api/platform/runs/${encodeURIComponent(source.run_id)}/memory-result?${memoryRecordParams({agent_id: agentId, round_index: source.source_round}, {limit: 1, calls_limit: 1})}`),
    ]);
    if (request !== state.request || !field.isConnected) return;
    const snapshot = source.snapshot_sha256 || (source.agents || []).find((agent) => agent.agent_id === agentId)?.snapshot_sha256 || details.snapshot_sha256;
    const eligible = memoryPoolEligibleReplicas(replicas, {...source, snapshot_sha256: snapshot}, agentId);
    const candidates = accounts.filter((account) => !memoryAccountIsDefault(account)
      && memoryProvider(account.provider || account.strategy) === memoryProvider(source.memory_strategy)
      && account.enabled !== false && memoryAccountHasKey(account) && !["disabled", "quota_exhausted", "cooldown", "invalid_credentials", "forbidden"].includes(account.status)
      && eligible.some((replica) => replica.account_id === memoryAccountId(account)
        && (replica.credential_revision === undefined || replica.credential_revision === account.credential_revision)));
    target.innerHTML = candidates.length ? `<ul class="memory-shared-pool">${candidates.map((account) => `<li>${escapeHtml(memoryAccountName(account))} · ${t("memoryWorkspace.verified")} · ${escapeHtml(memoryStateLabel(account.status || "active"))}</li>`).join("")}</ul>` : `<p class="validation-notice">${t(memos ? "memoryWorkspace.noExistingBindings" : "memoryWorkspace.noSharedCopies")}</p>`;
    status.textContent = t("memoryWorkspace.poolReady", {count: candidates.length});
  } catch (error) {
    if (request === state.request && field.isConnected) {status.textContent = error.message; state.key = "";}
  }
}

function updateMemoryAccountRunActivity(run) {
  const resume = document.querySelector("#resumeMemoryAccountRun");
  if (resume) {resume.hidden = !run.can_resume_memory_accounts; resume.disabled = !run.can_resume_memory_accounts;}
  const target = document.querySelector("#memoryAccountRunActivity");
  if (!target) return;
  const manifest = run.manifest || {};
  const audit = manifest.memory_read_audit || run.memory_read_audit || {};
  const activity = manifest.memory_account_switches || manifest.account_switches || run.memory_account_switches || audit.attempts || [];
  const current = manifest.memory_account_id || manifest.current_memory_account_id || run.memory_account_id || audit.account_id;
  target.innerHTML = `${current ? `<p>${t("memoryWorkspace.currentAccount")}: ${escapeHtml(current)}</p>` : ""}
    ${activity.length ? `<ul>${activity.map((entry) => `<li>${escapeHtml(entry.at || entry.timestamp || "")} · ${entry.from_account_id || entry.from ? `${escapeHtml(entry.from_account_id || entry.from)} → ` : ""}${escapeHtml(entry.to_account_id || entry.to || entry.account_id || "-")} · ${escapeHtml(memoryStateLabel(entry.status))} ${escapeHtml(entry.reason || entry.error || entry.failure || "")}</li>`).join("")}</ul>` : `<p class="muted">${t("memoryWorkspace.noAccountActivity")}</p>`}
    ${run.memory_accounts_resume_error ? `<p class="error-text">${escapeHtml(run.memory_accounts_resume_error)}</p>` : ""}`;
}

function memoryReadAuditMarkup(audit) {
  const visible = Object.fromEntries(["account_id", "replica_id", "physical_scope", "snapshot_sha256", "source_snapshot_sha256", "revision_hash", "memory_set_id"]
    .filter((key) => audit[key] !== undefined).map((key) => [key, audit[key]]));
  visible.attempts = (audit.attempts || []).map((entry) => Object.fromEntries(["account_id", "status", "failure", "reason", "at"]
    .filter((key) => entry[key] !== undefined).map((key) => [key, entry[key]])));
  return `<section><h3>${t("memoryWorkspace.accountActivity")}</h3><pre>${escapeHtml(JSON.stringify(visible, null, 2))}</pre></section>`;
}
