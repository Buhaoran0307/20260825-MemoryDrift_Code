/* Transfers and merges are reviewed before applying the same selection. */
function experimentGroupOperationBlock(group, includeActive = true) {
  if (group?.legacy || group?.is_legacy || experimentGroupId(group) === "legacy") return t("groupOps.legacyBlocked");
  if (group?.archived) return t("groupOps.archivedBlocked");
  if (includeActive && Number(group?.active_run_count || 0) > 0) return t("experiment.activeRunsBlocked");
  return "";
}

function experimentGroupOperationActions(group, experiments) {
  const reason = experimentGroupOperationBlock(group, false) || (!experiments.length ? t("groupOps.noExperiments") : "");
  return ["move", "copy"].map((mode) => `<button type="button" class="secondary" data-group-transfer="${mode}" title="${escapeHtml(reason || t(`groupOps.${mode}`))}" ${reason ? "disabled" : ""}>${t(`groupOps.${mode}`)}</button>`).join("");
}

function bindExperimentGroupOperations(root, group, experiments) {
  root.querySelectorAll("[data-group-transfer]").forEach((button) => {
    button.addEventListener("click", () => showExperimentGroupTransfer(group, experiments, button.dataset.groupTransfer).catch((error) => showToast(error.message)));
  });
}

function bindExperimentGroupMerge(root, groups) {
  const button = root.querySelector("#mergeSelectedGroups");
  if (!button) return;
  const choices = [...root.querySelectorAll('[name="group_batch_delete"]')];
  const selectedGroups = () => groups.filter((group) => choices.some((input) => !input.disabled && input.checked && input.value === experimentGroupId(group)));
  const update = () => {
    const selected = selectedGroups();
    const reason = selected.map((group) => experimentGroupOperationBlock(group)).find(Boolean);
    button.disabled = root.dataset.groupOperationBusy === "true" || selected.length < 2 || Boolean(reason);
    button.title = reason || t(selected.length < 2 ? "groupOps.mergeSelect" : "groupOps.mergeHelp");
  };
  choices.forEach((input) => input.addEventListener("change", update));
  root.querySelector("#groupDeleteSelectAll")?.addEventListener("change", update);
  root.addEventListener("group-selection-updated", update);
  button.addEventListener("click", () => {
    const selected = selectedGroups();
    if (root.dataset.groupOperationBusy === "true" || selected.length < 2 || selected.some((group) => experimentGroupOperationBlock(group))) return;
    showExperimentGroupMerge(selected);
  });
  update();
}

function experimentGroupDestinationOptions(groups) {
  return groups.map((group) => `<option value="${escapeHtml(experimentGroupId(group))}">${escapeHtml(group.name || experimentGroupId(group))} · ${escapeHtml(experimentGroupId(group))}</option>`).join("");
}

function experimentGroupReview(result, operation) {
  const requested = new Set(result.requested_experiment_ids || []);
  const included = result.included_experiment_ids || result.experiment_ids || [];
  const dependencies = included.filter((id) => !requested.has(id));
  return `<strong>${escapeHtml(t("groupOps.review", {
    experiments: formatNumber(result.experiment_count || 0), runs: formatNumber(result.run_count || 0), dependencies: formatNumber(result.dependency_count || 0),
  }))}</strong>
    ${dependencies.length ? `<p>${t("groupOps.includedDependencies")}</p><ul>${dependencies.map((id) => `<li>${escapeHtml(id)}</li>`).join("")}</ul>` : ""}
    ${operation === "merge" ? `<p>${t("groupOps.mergeHelp")}</p>` : ""}`;
}

async function showExperimentGroupTransfer(group, experiments, mode) {
  const reason = experimentGroupOperationBlock(group, false);
  if (reason) { showToast(reason); return; }
  const sourceGroupId = experimentGroupId(group);
  const payload = await apiGet("/api/platform/experiment-groups?archived=all");
  const targets = normalizeExperimentGroups(payload).filter((item) => experimentGroupId(item) !== sourceGroupId && !experimentGroupOperationBlock(item, false));
  const available = experiments.filter((item) => !item.archived && Number(item.active_run_count || 0) === 0);
  dialogTitle.innerHTML = `<h2>${t(`groupOps.${mode}`)}</h2><small>${escapeHtml(group.name || sourceGroupId)}</small>`;
  dialogBody.innerHTML = `<form id="groupOperationForm" class="group-operation-form">
    <p class="transfer-help">${t(mode === "copy" ? "groupOps.copyHelp" : "groupOps.moveHelp")}</p>
    <label class="group-operation-destination">${t("groupOps.destination")}<select name="target_group_id" required ${targets.length ? "" : "disabled"}><option value="">${t("groupOps.chooseDestination")}</option>${experimentGroupDestinationOptions(targets)}</select></label>
    ${targets.length ? "" : `<p class="archived-notice">${t("groupOps.noTargets")}</p>`}
    <div class="transfer-selection-toolbar"><label class="transfer-select-all"><input type="checkbox" id="groupOperationSelectAll"><span>${t("transfer.selectAll")}</span></label><span id="groupOperationSelectionCount" class="transfer-selection-count" role="status"></span></div>
    <div class="transfer-choices">${experiments.map((item) => {
      const blocked = item.archived ? t("groupOps.archivedExperimentBlocked") : Number(item.active_run_count || 0) > 0 ? t("experiment.activeRunsBlocked") : "";
      return `<label class="transfer-choice" title="${escapeHtml(blocked)}"><input type="checkbox" name="group_operation_experiment" value="${escapeHtml(item.experiment_id)}" ${blocked ? "disabled" : ""}><span class="transfer-choice-copy"><strong>${escapeHtml(item.name || item.experiment_id)}</strong><small>${escapeHtml(item.experiment_id)} · ${t(experimentListStage(item) === "tool_selection" ? "group.stageTwo" : "group.stageOne")}${blocked ? ` · ${escapeHtml(blocked)}` : ""}</small></span></label>`;
    }).join("")}</div>
    <div id="groupOperationStatus" class="group-operation-review" role="status" aria-live="polite"></div>
    <div class="group-operation-actions"><button type="submit" ${targets.length && available.length ? "" : "disabled"}>${t("groupOps.preview")}</button><button type="button" id="groupOperationApply" disabled>${t(`groupOps.apply${mode === "copy" ? "Copy" : "Move"}`)}</button></div>
  </form>`;
  if (!detailDialog.open) detailDialog.showModal();
  const form = dialogBody.querySelector("form");
  const choices = [...form.querySelectorAll('[name="group_operation_experiment"]')].filter((input) => !input.disabled);
  const all = form.querySelector("#groupOperationSelectAll");
  const updateSelection = () => {
    const selected = choices.filter((input) => input.checked).length;
    all.checked = choices.length > 0 && selected === choices.length;
    all.indeterminate = selected > 0 && selected < choices.length;
    all.disabled = choices.length === 0;
    form.querySelector("#groupOperationSelectionCount").textContent = t("transfer.selectedCount", {selected, total: choices.length});
  };
  all.addEventListener("change", () => { choices.forEach((input) => { input.checked = all.checked; }); updateSelection(); });
  choices.forEach((input) => input.addEventListener("change", updateSelection));
  updateSelection();
  bindExperimentGroupReview(form, {
    operation: "transfer", mode, enabled: targets.length > 0 && available.length > 0,
    request: () => ({source_group_id: sourceGroupId, target_group_id: form.elements.target_group_id.value, experiment_ids: choices.filter((input) => input.checked).map((input) => input.value), mode}),
    validate: (request) => !request.target_group_id ? t("groupOps.chooseDestination") : !request.experiment_ids.length ? t("transfer.chooseExperiments") : "",
  });
}

function showExperimentGroupMerge(groups) {
  dialogTitle.innerHTML = `<h2>${t("groupOps.merge")}</h2>`;
  dialogBody.innerHTML = `<form id="groupOperationForm" class="group-operation-form">
    <p class="transfer-help">${t("groupOps.mergeHelp")}</p>
    <ul class="group-operation-sources">${groups.map((group) => `<li><strong>${escapeHtml(group.name || experimentGroupId(group))}</strong><small>${escapeHtml(experimentGroupId(group))} · ${t("groupOps.groupSummary", {experiments: formatNumber(group.experiment_count || 0), runs: formatNumber(group.run_count || 0)})}</small></li>`).join("")}</ul>
    <label class="group-operation-destination">${t("groupOps.mergeDestination")}<select name="target_group_id" required>${experimentGroupDestinationOptions(groups)}</select></label>
    <div id="groupOperationStatus" class="group-operation-review" role="status" aria-live="polite"></div>
    <div class="group-operation-actions"><button type="submit">${t("groupOps.preview")}</button><button type="button" id="groupOperationApply" disabled>${t("groupOps.applyMerge")}</button></div>
  </form>`;
  if (!detailDialog.open) detailDialog.showModal();
  const form = dialogBody.querySelector("form");
  bindExperimentGroupReview(form, {
    operation: "merge", enabled: true,
    request: () => ({group_ids: groups.map(experimentGroupId), target_group_id: form.elements.target_group_id.value}),
    validate: (request) => !request.target_group_id ? t("groupOps.chooseDestination") : "",
  });
}

function bindExperimentGroupReview(form, options) {
  const status = form.querySelector("#groupOperationStatus");
  const preview = form.querySelector('[type="submit"]');
  const apply = form.querySelector("#groupOperationApply");
  let reviewed = null;
  let revision = 0;
  let busy = false;
  const resetReview = () => {
    revision += 1;
    reviewed = null;
    apply.disabled = true;
    status.textContent = "";
  };
  form.addEventListener("input", resetReview);
  form.addEventListener("change", resetReview);
  const current = () => form.isConnected !== false && detailDialog.open;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !options.enabled) return;
    reviewed = null; apply.disabled = true;
    const request = options.request();
    const invalid = options.validate(request);
    if (invalid) { status.textContent = invalid; return; }
    const checkedRevision = revision;
    busy = true; preview.disabled = true;
    status.textContent = t("groupOps.checking");
    try {
      const result = await apiPost(`/api/platform/experiment-groups/${options.operation}`, {...request, dry_run: true});
      if (!current() || checkedRevision !== revision) return;
      reviewed = {request, revision, planToken: result.plan_token};
      status.innerHTML = experimentGroupReview(result, options.operation);
      apply.disabled = false;
    } catch (error) {
      if (current() && checkedRevision === revision) status.textContent = error.message;
    } finally { busy = false; preview.disabled = !options.enabled; }
  });
  apply.addEventListener("click", async () => {
    if (busy || !current() || !reviewed || reviewed.revision !== revision) return;
    if (JSON.stringify(options.request()) !== JSON.stringify(reviewed.request)) { resetReview(); return; }
    const request = reviewed.request;
    const planToken = reviewed.planToken;
    reviewed = null;
    busy = true; apply.disabled = true; preview.disabled = true;
    const inputs = [...form.querySelectorAll("input, select")].filter((input) => !input.disabled);
    inputs.forEach((input) => { input.disabled = true; });
    status.textContent = t("groupOps.applying");
    try {
      const result = await apiPost(`/api/platform/experiment-groups/${options.operation}`, {...request, dry_run: false, plan_token: planToken});
      rememberToast(t(options.operation === "merge" ? "groupOps.merged" : options.mode === "copy" ? "groupOps.copied" : "groupOps.moved", {count: formatNumber(result.experiment_count || 0)}));
      location.href = `/experiment-groups/${encodeURIComponent(result.target_group_id || result.group_id || request.target_group_id)}`;
    } catch (error) {
      if (current()) status.textContent = error.message;
    } finally {
      busy = false;
      inputs.forEach((input) => { input.disabled = false; });
      preview.disabled = !options.enabled;
    }
  });
}
