/* ZIP transfers use binary uploads/downloads rather than base64 JSON. */
function experimentTransferActions(groupId = "") {
  return `<button type="button" class="secondary" data-export-experiments="${escapeHtml(groupId)}">${t("transfer.export")}</button>
    <button type="button" class="secondary" data-import-experiments="${escapeHtml(groupId)}">${t("transfer.import")}</button>`;
}

async function transferResponse(response) {
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error?.message || `HTTP ${response.status}`);
  }
  return response;
}

function bindExperimentTransfers(root) {
  root.querySelectorAll("[data-export-experiments]").forEach((button) => {
    button.addEventListener("click", () => showExperimentExport(button.dataset.exportExperiments).catch((e) => showToast(e.message)));
  });
  root.querySelectorAll("[data-import-experiments]").forEach((button) => {
    button.addEventListener("click", () => showExperimentImport(button.dataset.importExperiments).catch((e) => showToast(e.message)));
  });
}

async function showExperimentExport(groupId) {
  const payload = await apiGet(`/api/platform/experiments?archived=all${groupId ? `&group_id=${encodeURIComponent(groupId)}` : ""}`);
  const experiments = payload.experiments || [];
  dialogTitle.innerHTML = `<h2>${t("transfer.export")}</h2>`;
  dialogBody.innerHTML = `<form id="experimentExportForm" class="transfer-export-form">
    <p class="transfer-help">${t("transfer.exportHelp")}</p>
    <div class="transfer-selection-toolbar">
      <label class="transfer-select-all"><input type="checkbox" id="transferSelectAll" ${groupId ? "checked" : ""}> <span>${t("transfer.selectAll")}</span></label>
      <span id="transferSelectionCount" class="transfer-selection-count" role="status"></span>
    </div>
    <div class="transfer-choices">${experiments.map((item) => `<label class="transfer-choice"><input type="checkbox" name="transfer_experiment" value="${escapeHtml(item.experiment_id)}" ${groupId ? "checked" : ""}><span class="transfer-choice-copy"><strong>${escapeHtml(item.name || item.experiment_id)}</strong><small>${escapeHtml(item.experiment_id)} · ${escapeHtml(item.group_name || item.group_id)}</small></span></label>`).join("")}</div>
    <p id="transferExportStatus" role="status"></p>
    <button type="submit" ${experiments.length ? "" : "disabled"}>${t("transfer.download")}</button>
  </form>`;
  if (!detailDialog.open) detailDialog.showModal();
  const form = dialogBody.querySelector("form");
  const choices = Array.from(form.querySelectorAll('[name="transfer_experiment"]'));
  const all = form.querySelector("#transferSelectAll");
  const updateSelection = () => {
    const selected = choices.filter((item) => item.checked).length;
    all.checked = choices.length > 0 && selected === choices.length;
    all.indeterminate = selected > 0 && !all.checked;
    all.disabled = choices.length === 0;
    form.querySelector("#transferSelectionCount").textContent = t("transfer.selectedCount", {selected, total: choices.length});
  };
  all.addEventListener("change", () => {
    choices.forEach((choice) => { choice.checked = all.checked; });
    updateSelection();
  });
  choices.forEach((choice) => choice.addEventListener("change", updateSelection));
  updateSelection();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const ids = choices.filter((item) => item.checked).map((item) => item.value);
    const status = form.querySelector("#transferExportStatus");
    if (!ids.length) { status.textContent = t("transfer.chooseExperiments"); return; }
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    status.textContent = t("transfer.exporting");
    try {
      const response = await transferResponse(await fetch("/api/platform/experiment-bundles/export", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(groupId && ids.length === experiments.length ? {group_id: groupId} : {experiment_ids: ids}),
      }));
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url; link.download = `experiments-${groupId || "selection"}.zip`;
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 30000);
      status.textContent = t("transfer.exported");
    } catch (error) { status.textContent = error.message; }
    finally { submit.disabled = false; }
  });
}

async function showExperimentImport(groupId) {
  const [payload, resourcePayload] = await Promise.all([
    apiGet("/api/platform/experiment-groups?archived=all"),
    apiGet("/api/platform/resources?include_content=false"),
  ]);
  const groups = normalizeExperimentGroups(payload).filter((item) => !item.archived && !item.legacy && experimentGroupId(item) !== "legacy");
  dialogTitle.innerHTML = `<h2>${t("transfer.import")}</h2>`;
  dialogBody.innerHTML = `<form id="experimentImportForm" class="transfer-form">
    <p>${t("transfer.importHelp")}</p>
    <label>${t("transfer.file")}<input type="file" name="bundle" accept=".zip,application/zip" required></label>
    <label>${t("transfer.destination")}<select name="group_id"><option value="">${t("transfer.newGroup")}</option>${groups.map((item) => `<option value="${escapeHtml(experimentGroupId(item))}" ${experimentGroupId(item) === groupId ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label>
    <fieldset id="transferNewGroup"><label>${t("group.id")}<input name="new_group_id" pattern="[a-z0-9][a-z0-9_-]{0,63}" maxlength="64"></label><label>${t("transfer.groupName")}<input name="new_group_name"></label>
      <details><summary>${t("transfer.defaultResources")}</summary>
      ${["queries", "mcp"].map((role) => `<label>${t(`group.${role}`)}<select name="${role === "queries" ? "query" : "mcp"}_revision_id"><option value="">${t("transfer.fromBundle")}</option>${resourceRevisionOptions((resourcePayload.resources || []).filter((resource) => resource.kind === role))}</select></label>`).join("")}</details>
    </fieldset>
    <p>${t("transfer.cloudNote")}</p>
    <p id="transferImportStatus" role="status"></p>
    <button type="submit">${t("transfer.preview")}</button>
    <button type="button" id="transferApply" disabled>${t("transfer.apply")}</button>
  </form>`;
  if (!detailDialog.open) detailDialog.showModal();
  const form = dialogBody.querySelector("form");
  const status = form.querySelector("#transferImportStatus");
  const preview = form.querySelector('[type="submit"]');
  const apply = form.querySelector("#transferApply");
  let reviewed = null;
  let revision = 0;
  function changed() {
    revision += 1;
    reviewed = null; apply.disabled = true; status.textContent = "";
    const isNew = !form.elements.group_id.value;
    form.querySelector("#transferNewGroup").hidden = !isNew;
    form.elements.new_group_id.required = isNew;
    form.elements.new_group_name.required = isNew;
  }
  form.addEventListener("input", changed); changed();
  async function upload(dryRun, query, file) {
    query.set("dry_run", String(dryRun));
    return (await transferResponse(await fetch(`/api/platform/experiment-bundles/import?${query}`, {
      method: "POST", headers: {"Content-Type": "application/zip"}, body: file,
    }))).json();
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); reviewed = null; apply.disabled = true; preview.disabled = true;
    const query = new URLSearchParams();
    if (form.elements.group_id.value) query.set("group_id", form.elements.group_id.value);
    else {
      query.set("new_group_id", form.elements.new_group_id.value.trim());
      query.set("new_group_name", form.elements.new_group_name.value.trim());
      for (const field of ["query_revision_id", "mcp_revision_id"]) {
        if (form.elements[field].value) query.set(field, form.elements[field].value);
      }
    }
    const file = form.elements.bundle.files[0];
    const checkedRevision = revision;
    status.textContent = t("transfer.checking");
    try {
      const result = await upload(true, query, file);
      if (checkedRevision !== revision) return;
      reviewed = {query, file};
      status.textContent = t("transfer.review", {experiments: result.experiment_count, runs: result.run_count, dependencies: result.dependency_count});
      apply.disabled = false;
    } catch (error) { status.textContent = error.message; }
    finally { preview.disabled = false; }
  });
  apply.addEventListener("click", async () => {
    if (!reviewed) return;
    apply.disabled = true; preview.disabled = true;
    status.textContent = t("transfer.importing");
    try {
      const result = await upload(false, reviewed.query, reviewed.file);
      rememberToast(t("transfer.imported", {count: result.experiment_count}));
      location.href = `/experiment-groups/${encodeURIComponent(result.group_id)}`;
    } catch (error) { status.textContent = error.message; apply.disabled = false; }
    finally { preview.disabled = false; }
  });
}
