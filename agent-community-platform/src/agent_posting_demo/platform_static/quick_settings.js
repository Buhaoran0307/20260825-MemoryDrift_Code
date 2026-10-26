/* Focused defaults and quick configuration screens for new experiments. */
const quickWords = {
  en: {
    settings: "Default settings", settingsHelp: "Applies to future experiments only. Saved experiments and run snapshots keep their values.",
    quick: "Quick experiment setup", quickHelp: "Create configurations in this group, with the option to start them immediately.",
    stage: "Stage", stageOne: "Stage one: read posts", stageTwo: "Stage two: select MCP",
    group: "Experiment group", postsResource: "Registered posts file", posts: "Posts to read (multiple allowed)",
    reader: "Reading agent", strategy: "Memory strategy", memoryModel: "Memory management model",
    fixedMemoryModel: "Stage-one local memory uses deepseek-v4-flash. Mem0/Hindsight manage their hosted models; stage-two models are configured separately.",
    query: "Query dataset", mcp: "MCP dataset", mapping: "Target mapping", sourceExperiment: "Stage-one experiment",
    sourceRun: "Source run", sourceAgent: "Source agent", secondModel: "Stage-two model",
    name: "Experiment name", nameHelp: "Generated from the selection when empty; you can edit it.",
    save: "Save configuration", saved: "Defaults saved", noSources: "No completed usable memory snapshot is available.",
    memoryModels: "Memory management models (choose several)", decisionModels: "Decision models (choose several)",
    createOnly: "Create configurations", createAndLaunch: "Create and start", selectModel: "Select at least one model.",
    chooseSourceRun: "Choose a completed first-stage source run.",
    autoNameBatch: "Each configuration gets its model name when several models are selected.",
    createdCount: "Created {count} configurations", startedCount: "Started {count} runs", failedCount: "{count} failed",
    choose: "Choose", noPosts: "No posts in this resource.", noGroup: "Choose an active experiment group first.",
    noMapping: "Register a target mapping in Resources first.", request: "Model requests", reading: "Stage-one reading",
    memory: "Stage-one memory strategies", validation: "Stage-two selection and retrieval", output: "Results",
    connection: "Stage-two model connection", modelConnection: "Dedicated connection", generalConnection: "Use the general stage-two connection above", modelConnectionHelp: "Affects only future stage-two experiments for this model. Stage-one memory models still use the general connection.", position: "API-key reminder position", root: "Output directory rule",
    rootHelp: "Use {experiment_id} as the new experiment ID placeholder.", saveThread: "Save post snapshots each round",
    saveMemory: "Save memory snapshots each round", hideSpeaker: "Hide speaker", speakerAsJson: "Show speaker and content as JSON", requireIdentity: "Require reader identity",
    inherit: "Use the source run connection", environment: "Read current .env", custom: "Specify API URL",
    apiBaseEnv: "API URL environment variable", apiKeyEnv: "Credential environment variable", apiBase: "API URL",
    first: "Start of memory", middle: "Middle of memory", last: "End of memory", message: "Separate message", query_suffix: "After query in same message",
    concurrency: "Concurrency", timeout_seconds: "Request timeout (seconds)", min_request_interval_seconds: "Minimum request interval (seconds)",
    max_transient_retries: "Retries", retry_backoff_seconds: "Initial retry wait (seconds)", max_retry_wait_seconds: "Maximum retry wait (seconds)",
    temperature: "Temperature", max_tokens: "Maximum output tokens", query_limit: "Query limit", max_steps: "Processing steps",
    embedding_model: "Embedding model", embedding_dimensions: "Embedding dimensions", enable_deletes: "Allow memory deletion", custom_instructions: "Custom instructions",
    evo_threshold: "Evolution threshold", request_timeout_seconds: "Service timeout (seconds)",
    response_tolerance_enabled: "A-MEM response tolerance",
    response_validation_max_attempts: "Content repair attempts (including initial request)",
    event_timeout_seconds: "Event timeout (seconds)", event_poll_interval_seconds: "Event poll interval (seconds)",
    processing_timeout_seconds: "Processing timeout (seconds)", poll_interval_seconds: "Poll interval (seconds)", max_read_retries: "Read retries",
    target_bank: "Target bank", retrieval_top_k: "Retrieval count", search_threshold: "Search threshold", search_distance: "Search distance",
    rerank: "Rerank", rewrite_query: "Rewrite query", aggregate: "Aggregate", recall_budget: "Recall budget",
    recall_max_tokens: "Recall token limit", context_max_chars: "Context character limit", merged_max_items: "Maximum merged items",
  },
};

function qw(key) { return quickWords[i18n.getLocale()]?.[key] || quickWords.en[key] || key; }
function quickStrategyName(strategy) { return strategy === "agent_memory_preview" ? memoryStrategyLabel(strategy) : ({langmem_official: "Langmem", a_mem_official: "Amem", mem0_platform_v3: "Mem0", hindsight_cloud: "Hindsight", memos_cloud: "MemOS"})[strategy] || strategy; }
function quickOption(value, label, selected = false) { return `<option value="${escapeHtml(value)}" ${selected ? "selected" : ""}>${escapeHtml(label)}</option>`; }
function quickSelect(id, label, options, selected = "", {required = true} = {}) {
  return `<div class="form-field"><label for="${id}">${escapeHtml(label)}</label><select id="${id}" name="${id}" ${required ? "required" : ""}>${quickOption("", qw("choose"), !selected)}${options.map(([value, text]) => quickOption(value, text, String(value) === String(selected))).join("")}</select></div>`;
}
function quickModelChoices(name, label, selected) {
  return `<fieldset class="quick-model-choices"><legend>${escapeHtml(label)}</legend><div class="quick-model-grid">${EXPERIMENT_MODELS.map((model) => `<label class="quick-check"><input type="checkbox" name="${name}" value="${escapeHtml(model)}" ${model === selected ? "checked" : ""}>${escapeHtml(model)}</label>`).join("")}</div></fieldset>`;
}
function quickSettingInput(path, value, label) {
  const id = `setting-${path.replace(/[^a-z0-9]+/gi, "-")}`;
  if (typeof value === "boolean") return `<label class="quick-check"><input type="checkbox" data-setting="${escapeHtml(path)}" ${value ? "checked" : ""}>${escapeHtml(label)}</label>`;
  const numeric = typeof value === "number";
  const required = numeric || path.endsWith(".model") || path === "output.root_dir_template";
  const fixedMemory = [...FIXED_LOCAL_MEMORY_STRATEGIES].some((strategy) => path === `memory_profiles.${strategy}.model`);
  if (fixedMemory) value = FIXED_LOCAL_MEMORY_MODEL;
  return `<div class="form-field"><label for="${id}">${escapeHtml(label)}</label><input id="${id}" data-setting="${escapeHtml(path)}" type="${numeric ? "number" : "text"}" ${numeric ? `step="${Number.isInteger(value) ? "1" : "any"}"` : ""} value="${escapeHtml(value)}" ${required ? "required" : ""} ${fixedMemory ? "readonly" : ""}>${fixedMemory ? `<small>${escapeHtml(qw("fixedMemoryModel"))}</small>` : ""}</div>`;
}
function quickSetPath(root, path, value) {
  const parts = path.split(".");
  let current = root;
  while (parts.length > 1) current = current[parts.shift()];
  current[parts[0]] = value;
}

async function renderPlatformSettings() {
  const {settings} = await apiGet("/api/platform/settings");
  const backend = Object.entries(settings.backend).map(([key, value]) => quickSettingInput(`backend.${key}`, value, qw(key))).join("");
  const readingLabels = {hide_speaker: "hideSpeaker", speaker_as_json: "speakerAsJson", require_identity: "requireIdentity"};
  const reading = Object.entries(settings.reading).map(([key, value]) => quickSettingInput(`reading.${key}`, value, qw(readingLabels[key] || key))).join("");
  const memory = Object.entries(settings.memory_profiles).filter(([strategy]) => selectableMemoryStrategies().includes(strategy)).map(([strategy, profile]) => `<details class="quick-profile"><summary>${escapeHtml(memoryStrategyLabel(strategy))}</summary><div class="form-grid">${profile.model ? quickSettingInput(`memory_profiles.${strategy}.model`, profile.model, qw("memoryModel")) : ""}${Object.entries(profile.options).map(([key, value]) => quickSettingInput(`memory_profiles.${strategy}.options.${key}`, value, qw(key))).join("") || `<p class="muted">${escapeHtml(memoryStrategyLabel(strategy))}</p>`}</div></details>`).join("");
  const retrieval = Object.entries(settings.validation.retrieval_profiles).filter(([strategy]) => selectableMemoryStrategies().includes(strategy)).map(([strategy, profile]) => `<details class="quick-profile"><summary>${escapeHtml(memoryStrategyLabel(strategy))}</summary><div class="form-grid">${Object.entries(profile).map(([key, value]) => key === "recall_budget" ? `<div class="form-field"><label>${qw(key)}</label><select data-setting="validation.retrieval_profiles.${strategy}.${key}">${["low", "mid", "high"].map((item) => quickOption(item, item, item === value)).join("")}</select></div>` : quickSettingInput(`validation.retrieval_profiles.${strategy}.${key}`, value, qw(key))).join("")}</div></details>`).join("");
  const connection = settings.validation.backend_connection;
  const modelConnections = Object.entries(settings.validation.model_connections).map(([model, selected]) => `<details class="quick-profile"><summary>${escapeHtml(model)} · ${qw("modelConnection")}</summary><div class="form-grid">
    ${quickSelect(`settingConnection-${model}`, qw("connection"), [["inherit", qw("generalConnection")], ["environment", qw("environment")], ["custom", qw("custom")]], selected.mode)}
    ${quickSettingInput(`validation.model_connections.${model}.api_base_env`, selected.api_base_env || "AGENT_COMMUNITY_API_BASE", qw("apiBaseEnv"))}
    ${quickSettingInput(`validation.model_connections.${model}.api_key_env`, selected.api_key_env || "AGENT_COMMUNITY_API_KEY", qw("apiKeyEnv"))}
    ${quickSettingInput(`validation.model_connections.${model}.api_base`, selected.api_base || "", qw("apiBase"))}
    <small class="wide">${qw("modelConnectionHelp")}</small>
  </div></details>`).join("");
  main.innerHTML = `<div class="page quick-page">${pageHeader(qw("settings"), qw("settingsHelp"), `<a class="button secondary" href="/experiment-groups">${qw("group")}</a>`)}
    <form id="platformSettingsForm">
      <section class="section">${sectionHeading(qw("request"))}<div class="form-grid">${backend}</div></section>
      <section class="section">${sectionHeading(qw("reading"))}<div class="form-grid">${reading}</div></section>
      <section class="section">${sectionHeading(qw("memory"))}${memory}</section>
      <section class="section">${sectionHeading(qw("validation"))}<div class="form-grid">
        ${quickSelect("settingConnection", qw("connection"), [["inherit", qw("inherit")], ["environment", qw("environment")], ["custom", qw("custom")]], connection.mode)}
        ${quickSettingInput("validation.backend_connection.api_base_env", connection.api_base_env || "AGENT_COMMUNITY_API_BASE", qw("apiBaseEnv"))}
        ${quickSettingInput("validation.backend_connection.api_key_env", connection.api_key_env || "AGENT_COMMUNITY_API_KEY", qw("apiKeyEnv"))}
        ${quickSettingInput("validation.backend_connection.api_base", connection.api_base || "", qw("apiBase"))}
        ${quickSelect("settingPosition", qw("position"), ["first", "middle", "last", "message", "query_suffix"].map((item) => [item, qw(item)]), settings.validation.api_key_position)}
      </div>${modelConnections}${retrieval}</section>
      <section class="section">${sectionHeading(qw("output"))}<div class="form-grid">${quickSettingInput("output.root_dir_template", settings.output.root_dir_template, qw("root"))}${quickSettingInput("output.save_thread_after_each_round", settings.output.save_thread_after_each_round, qw("saveThread"))}${quickSettingInput("output.save_memory_after_each_round", settings.output.save_memory_after_each_round, qw("saveMemory"))}<small class="wide">${qw("rootHelp")}</small></div></section>
      <div class="page-actions"><button type="submit">${qw("save")}</button></div>
    </form></div>`;
  const form = main.querySelector("#platformSettingsForm");
  const hideSpeaker = form.querySelector('[data-setting="reading.hide_speaker"]');
  const speakerAsJson = form.querySelector('[data-setting="reading.speaker_as_json"]');
  const syncSpeakerFormat = () => {
    speakerAsJson.closest(".quick-check").hidden = hideSpeaker.checked;
    speakerAsJson.disabled = hideSpeaker.checked;
  };
  hideSpeaker.addEventListener("change", syncSpeakerFormat);
  syncSpeakerFormat();
  const syncConnection = (prefix, select) => {
    const mode = select.value;
    ["api_base_env", "api_key_env", "api_base"].forEach((key) => {
      const input = form.querySelector(`[data-setting="${prefix}.${key}"]`);
      input.closest(".form-field").hidden = mode === "inherit" || (key === "api_base" ? mode !== "custom" : key === "api_base_env" ? mode !== "environment" : false);
      input.disabled = input.closest(".form-field").hidden;
      input.required = !input.disabled;
    });
  };
  const connectionControls = [
    ["validation.backend_connection", form.elements.settingConnection],
    ...Object.keys(settings.validation.model_connections).map((model) => [
      `validation.model_connections.${model}`, form.elements[`settingConnection-${model}`],
    ]),
  ];
  connectionControls.forEach(([prefix, select]) => {
    select.addEventListener("change", () => syncConnection(prefix, select));
    syncConnection(prefix, select);
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const next = structuredClone(settings);
      next.memory_profiles = Object.fromEntries(Object.entries(next.memory_profiles).filter(([strategy]) => selectableMemoryStrategies().includes(strategy)));
      next.validation.retrieval_profiles = Object.fromEntries(Object.entries(next.validation.retrieval_profiles).filter(([strategy]) => selectableMemoryStrategies().includes(strategy)));
      form.querySelectorAll("[data-setting]:not(:disabled)").forEach((input) => {
        if (input.dataset.setting.startsWith("validation.backend_connection.")
            || input.dataset.setting.startsWith("validation.model_connections.")) return;
        const value = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
        quickSetPath(next, input.dataset.setting, value);
      });
      if (hideSpeaker.checked) next.reading.speaker_as_json = false;
      const mode = form.elements.settingConnection.value;
      next.validation.backend_connection = mode === "inherit" ? {mode} : mode === "environment"
        ? {mode, api_base_env: form.querySelector('[data-setting="validation.backend_connection.api_base_env"]').value, api_key_env: form.querySelector('[data-setting="validation.backend_connection.api_key_env"]').value}
        : {mode, api_base: form.querySelector('[data-setting="validation.backend_connection.api_base"]').value, api_key_env: form.querySelector('[data-setting="validation.backend_connection.api_key_env"]').value};
      for (const model of Object.keys(settings.validation.model_connections)) {
        const prefix = `validation.model_connections.${model}`;
        const modelMode = form.elements[`settingConnection-${model}`].value;
        const value = (key) => form.querySelector(`[data-setting="${prefix}.${key}"]`).value;
        next.validation.model_connections[model] = modelMode === "inherit" ? {mode: modelMode}
          : modelMode === "environment"
            ? {mode: modelMode, api_base_env: value("api_base_env"), api_key_env: value("api_key_env")}
            : {mode: modelMode, api_base: value("api_base"), api_key_env: value("api_key_env")};
      }
      next.validation.api_key_position = form.elements.settingPosition.value;
      await apiPost("/api/platform/settings", {settings: next});
      platformSettingsCache = next;
      showToast(qw("saved"));
    } catch (error) { showToast(error.message); }
    finally { button.disabled = false; }
  });
}

async function renderQuickExperiment(groupId) {
  const query = new URLSearchParams(location.search);
  const [groupPayload, postsPayload, queryPayload, settingsPayload, experimentsPayload, mappingsPayload, mcpPayload] = await Promise.all([
    apiGet(`/api/platform/experiment-groups/${encodeURIComponent(groupId)}`),
    apiGet("/api/platform/resources?kind=posts&include_content=true"),
    apiGet("/api/platform/resources?kind=queries&include_content=false"),
    apiGet("/api/platform/settings"),
    apiGet("/api/platform/experiments?archived=all"),
    apiGet("/api/platform/target-mappings"),
    apiGet("/api/platform/resources?kind=mcp&include_content=false"),
  ]);
  const settings = settingsPayload.settings;
  const group = groupPayload.group || groupPayload;
  if (group.archived || group.legacy || group.is_legacy || group.can_create === false) throw new Error(qw("noGroup"));
  const experiments = experimentsPayload.experiments || [];
  const mappings = mappingsPayload.mappings || [];
  const stageOneExperiments = () => experiments.filter((item) => item.group_id === groupId && isSupportedReadingExperiment(item) && !item.archived);
  const postRevisions = (postsPayload.resources || []).flatMap((resource) => (resource.revisions || []).filter((revision) => !revision.resource_archived && ["moltnet_forum", "moltnet_thread"].includes(revision.environment_type)).map((revision) => ({...revision, resource_name: resource.name})));
  const queryRevisions = (queryPayload.resources || []).flatMap((resource) => (resource.revisions || []).filter((revision) => !revision.resource_archived).map((revision) => ({...revision, resource_name: resource.name})));
  const mcpRevisions = (mcpPayload.resources || []).flatMap((resource) => (resource.revisions || []).filter((revision) => !revision.resource_archived).map((revision) => ({...revision, resource_name: resource.name, resource_id: resource.resource_id})));
  const stage = query.get("stage") === "tool_selection" ? "tool_selection" : "interaction";
  main.innerHTML = `<div class="page quick-page">${pageHeader(qw("quick"), `${escapeHtml(group.name || groupId)} · ${qw("quickHelp")}`, `<a class="button secondary" href="/experiment-groups/${encodeURIComponent(groupId)}">${t("common.back")}</a><a class="button secondary" href="/settings">${qw("settings")}</a><a class="button secondary" href="/experiments/new?group_id=${encodeURIComponent(groupId)}&stage=${stage}">${t("dashboard.newExperiment")}</a>`)}
    <form id="quickExperimentForm" class="section">
      <div class="form-grid">${quickSelect("quickStage", qw("stage"), [["interaction", qw("stageOne")], ["tool_selection", qw("stageTwo")]], stage)}</div>
      <div id="quickStageFields"></div>
      <div class="form-grid">${textField("quickName", qw("name"), "", qw("autoNameBatch"), "", "wide")}</div>
      <div class="page-actions"><button type="submit" value="create">${qw("createOnly")}</button><button type="submit" value="launch" class="secondary">${qw("createAndLaunch")}</button></div>
      <div id="quickBatchResult" class="quick-batch-result" role="status" hidden></div>
    </form></div>`;
  const form = main.querySelector("#quickExperimentForm");
  let currentSources = [];
  let mcpParameterEditor = null;
  let mcpLoadRequest = 0;
  const mcpResourceCache = new Map();
  const loadSelectedMcp = async () => {
    const request = ++mcpLoadRequest;
    const controller = mcpParameterEditor;
    const revision = mcpRevisions.find((item) => item.revision_id === form.elements.quickMcp?.value);
    controller?.setMcp(null);
    if (!revision || !controller) return;
    try {
      let content = revision.content;
      if (!content && revision.resource_id) {
        if (!mcpResourceCache.has(revision.resource_id)) {
          mcpResourceCache.set(revision.resource_id, apiGet(`/api/platform/resources/${encodeURIComponent(revision.resource_id)}?include_content=true`));
        }
        const resource = await mcpResourceCache.get(revision.resource_id);
        content = (resource.revisions || []).find((item) => item.revision_id === revision.revision_id)?.content;
      }
      if (request !== mcpLoadRequest || controller !== mcpParameterEditor || !form.isConnected) return;
      controller.setMcp(content || null);
    } catch (error) {
      mcpResourceCache.delete(revision.resource_id);
      if (request === mcpLoadRequest && controller === mcpParameterEditor && form.isConnected) showToast(error.message);
    }
  };
  let nameDirty = false;
  form.elements.quickName.addEventListener("input", () => { nameDirty = Boolean(form.elements.quickName.value.trim()); });
  const selectedRevision = () => postRevisions.find((item) => item.revision_id === form.elements.quickPostsResource?.value);
  const selectedPosts = () => [...form.querySelectorAll('[name="quickPostId"]:checked')].map((input) => input.value);
  const selectedModels = (name) => [...form.querySelectorAll(`[name="${name}"]:checked`)].map((input) => input.value);
  const suggestedName = () => {
    if (form.elements.quickStage.value === "interaction") {
      const posts = selectedRevision()?.content?.dataset?.posts || [];
      const titles = selectedPosts().map((id) => posts.find((post) => post.id === id)?.title || id);
      const strategy = form.elements.quickStrategy?.value || "";
      const models = FIXED_LOCAL_MEMORY_STRATEGIES.has(strategy) ? [FIXED_LOCAL_MEMORY_MODEL]
        : MEMORY_MODEL_STRATEGIES.has(strategy) ? selectedModels("quickMemoryModel") : [];
      if (models.length > 1) return "";
      const model = models[0] || "";
      return [titles.join("、"), quickStrategyName(strategy), model].filter(Boolean).join(" + ");
    }
    const source = stageOneExperiments().find((item) => item.experiment_id === form.elements.quickSourceExperiment?.value);
    const queryRevision = queryRevisions.find((item) => item.revision_id === form.elements.quickQuery?.value);
    const models = selectedModels("quickDecisionModel");
    if (models.length > 1) return "";
    return [source?.name || source?.experiment_id, queryRevision?.resource_name || queryRevision?.filename, models[0] || form.elements.quickModel?.value].filter(Boolean).join(" + ");
  };
  const syncName = () => { if (!nameDirty) form.elements.quickName.value = suggestedName(); };
  const renderPosts = () => {
    const revision = selectedRevision();
    const dataset = revision?.content?.dataset;
    const single = revision?.environment_type === "moltnet_thread";
    form.querySelector("#quickPostChoices").innerHTML = (dataset?.posts || []).map((post, index) => `<label class="quick-check"><input name="quickPostId" type="${single ? "radio" : "checkbox"}" value="${escapeHtml(post.id)}" ${!single || index === 0 ? "checked" : ""}>${escapeHtml(post.title || post.id)} <small>${escapeHtml(post.id)}</small></label>`).join("") || qw("noPosts");
    const agents = (dataset?.agents || []).map((agent) => [agent.id, `${agent.name || agent.id} · ${agent.id}`]);
    form.querySelector("#quickReaderField").innerHTML = quickSelect("quickReader", qw("reader"), agents, agents.length === 1 ? agents[0][0] : "");
    syncName();
  };
  const syncMemoryModel = () => {
    const strategy = form.elements.quickStrategy.value;
    const box = form.querySelector("#quickMemoryModelField");
    box.hidden = !MEMORY_MODEL_STRATEGIES.has(strategy);
    box.innerHTML = box.hidden ? "" : FIXED_LOCAL_MEMORY_STRATEGIES.has(strategy)
      ? `${textField("quickMemoryModel", qw("memoryModel"), FIXED_LOCAL_MEMORY_MODEL, qw("fixedMemoryModel"), "readonly")}`
      : quickModelChoices("quickMemoryModel", qw("memoryModels"), settings.memory_profiles[strategy]?.model || EXPERIMENT_MODELS[0]);
    if (strategy === "agent_memory_preview") box.insertAdjacentHTML("beforeend", `<p class="validation-notice">${t("memoryPreview.help")}</p>`);
    box.querySelectorAll("input").forEach((input) => input.addEventListener("change", syncName));
    syncName();
  };
  const selectedSource = () => currentSources.find((item) => item.run_id === form.elements.quickSourceRun?.value)
    || (currentSources.length === 1 ? currentSources[0] : null);
  const syncDecisionModels = () => {
    const source = selectedSource() || currentSources[0];
    const box = form.querySelector("#quickModelField");
    if (!source) { box.innerHTML = ""; syncName(); return; }
    const hosted = ["mem0_platform_v3", "hindsight_cloud", "memos_cloud"].includes(source.memory_strategy);
    const preferred = source.memory_model || source.backend_model;
    box.innerHTML = hosted
      ? quickModelChoices("quickDecisionModel", qw("decisionModels"), EXPERIMENT_MODELS.includes(preferred) ? preferred : EXPERIMENT_MODELS[0])
      : quickSelect("quickModel", qw("secondModel"), EXPERIMENT_MODELS.map((model) => [model, model]), EXPERIMENT_MODELS.includes(preferred) ? preferred : "");
    box.querySelectorAll("input, select").forEach((input) => input.addEventListener("change", syncName));
    syncName();
  };
  const renderSources = async () => {
    const experimentId = form.elements.quickSourceExperiment?.value;
    const status = form.querySelector("#quickSourceStatus");
    currentSources = [];
    form.querySelector("#quickSourceRunField").innerHTML = "";
    form.querySelector("#quickSourceAgentField").innerHTML = "";
    form.querySelector("#quickModelField").innerHTML = "";
    if (!experimentId) { status.textContent = ""; syncName(); return; }
    status.textContent = t("common.loading");
    try {
      let offset = 0;
      while (offset < 1000) {
        const page = await apiGet(`/api/platform/memory-sources?experiment_id=${encodeURIComponent(experimentId)}&group_id=${encodeURIComponent(groupId)}&limit=100&offset=${offset}`);
        const sources = (page.sources || []).filter((item) => item.usable);
        if (form.elements.quickSourceExperiment?.value !== experimentId) return;
        currentSources.push(...sources);
        if (!page.has_more) break;
        offset += 100;
      }
      if (!form.isConnected || form.elements.quickStage.value !== "tool_selection"
          || form.elements.quickSourceExperiment?.value !== experimentId) return;
      status.textContent = currentSources.length ? "" : qw("noSources");
      const runBox = form.querySelector("#quickSourceRunField");
      runBox.innerHTML = currentSources.length ? quickSelect("quickSourceRun", qw("sourceRun"), currentSources.map((source) => [source.run_id, `${source.run_id} · ${source.source_round}`]), currentSources.length === 1 ? currentSources[0].run_id : "") : "";
      runBox.hidden = currentSources.length <= 1;
      renderSourceAgents();
    } catch (error) { status.textContent = error.message; }
  };
  const renderSourceAgents = () => {
    const source = selectedSource();
    const agents = source?.agents || [];
    const box = form.querySelector("#quickSourceAgentField");
    box.innerHTML = agents.length ? quickSelect("quickSourceAgent", qw("sourceAgent"), agents.map((agent) => [agent.agent_id, `${agent.display_name} · ${agent.agent_id}`]), agents.length === 1 ? agents[0].agent_id : "") : "";
    box.hidden = agents.length <= 1;
    syncDecisionModels();
  };
  const renderStage = () => {
    const isOne = form.elements.quickStage.value === "interaction";
    const groupSources = stageOneExperiments();
    form.querySelector("#quickStageFields").innerHTML = isOne ? `<div class="form-grid">
      ${quickSelect("quickPostsResource", qw("postsResource"), postRevisions.map((item) => [item.revision_id, `${item.resource_name} · r${item.revision_number}`]))}
      ${quickSelect("quickStrategy", qw("strategy"), firstStageMemoryChoices().map((key) => [key, memoryStrategyLabel(key)]))}
      <div id="quickReaderField"></div><div id="quickMemoryModelField" class="wide"></div>
      </div><fieldset class="quick-posts"><legend>${qw("posts")}</legend><div id="quickPostChoices"></div></fieldset>`
      : `<div class="form-grid">
        ${quickSelect("quickQuery", qw("query"), queryRevisions.map((item) => [item.revision_id, `${item.resource_name} · r${item.revision_number}`]))}
        ${quickSelect("quickMcp", qw("mcp"), mcpRevisions.map((item) => [item.revision_id, `${item.resource_name} · r${item.revision_number}`]))}
        ${mcpParameterAdditionsPanel()}
        ${quickSelect("quickMapping", qw("mapping"), mappings.map((item) => [item.mapping_id, item.name]), group?.target_mapping_id || "", {required: false})}
        ${quickSelect("quickSourceExperiment", qw("sourceExperiment"), groupSources.filter((item) => !item.archived).map((item) => [item.experiment_id, item.name || item.experiment_id]))}
        <div id="quickModelField" class="wide"></div>
        <div id="quickSourceRunField" hidden></div><div id="quickSourceAgentField" hidden></div>
      </div><p id="quickSourceStatus" class="muted">${mappings.length ? "" : qw("noMapping")}</p>`;
    if (isOne) {
      mcpParameterEditor = null;
      ++mcpLoadRequest;
      form.elements.quickPostsResource.addEventListener("change", renderPosts);
      form.elements.quickStrategy.addEventListener("change", syncMemoryModel);
      form.querySelector("#quickPostChoices").addEventListener("change", syncName);
      form.elements.quickPostsResource.value = postRevisions.length === 1 ? postRevisions[0].revision_id : "";
      form.elements.quickStrategy.value = "langmem_official";
      renderPosts(); syncMemoryModel();
    } else {
      form.elements.quickSourceExperiment.addEventListener("change", () => { renderSources(); syncName(); });
      form.querySelector("#quickSourceRunField").addEventListener("change", renderSourceAgents);
      form.elements.quickQuery.addEventListener("change", syncName);
      mcpParameterEditor = bindMcpParameterAdditionsEditor(form.querySelector("[data-mcp-parameter-editor]"), null);
      form.elements.quickMcp.addEventListener("change", () => { loadSelectedMcp(); syncName(); });
      if (groupSources.length === 1) form.elements.quickSourceExperiment.value = groupSources[0].experiment_id;
      const defaultQueryId = group.resource_refs?.queries?.revision_id;
      if (queryRevisions.some((item) => item.revision_id === defaultQueryId)) form.elements.quickQuery.value = defaultQueryId;
      else if (queryRevisions.length === 1) form.elements.quickQuery.value = queryRevisions[0].revision_id;
      const defaultMcpId = group.resource_refs?.mcp?.revision_id;
      if (mcpRevisions.some((item) => item.revision_id === defaultMcpId)) form.elements.quickMcp.value = defaultMcpId;
      else if (mcpRevisions.length === 1) form.elements.quickMcp.value = mcpRevisions[0].revision_id;
      loadSelectedMcp();
      renderSources(); syncName();
    }
  };
  form.elements.quickStage.addEventListener("change", renderStage);
  renderStage();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const launch = event.submitter?.value === "launch";
    const buttons = [...form.querySelectorAll('button[type="submit"]')];
    try {
      const isOne = form.elements.quickStage.value === "interaction";
      const payload = {stage: form.elements.quickStage.value, name: form.elements.quickName.value.trim(), launch};
      if (isOne) {
        payload.posts_revision_id = form.elements.quickPostsResource.value;
        payload.post_ids = selectedPosts();
        payload.reader_agent_id = form.elements.quickReader.value;
        payload.memory_strategy = form.elements.quickStrategy.value;
        if (MEMORY_MODEL_STRATEGIES.has(payload.memory_strategy)) {
          payload.memory_models = FIXED_LOCAL_MEMORY_STRATEGIES.has(payload.memory_strategy)
            ? [FIXED_LOCAL_MEMORY_MODEL] : selectedModels("quickMemoryModel");
          if (!payload.memory_models.length) throw new Error(qw("selectModel"));
        }
      } else {
        const source = selectedSource();
        if (!source) throw new Error(qw("chooseSourceRun"));
        payload.query_revision_id = form.elements.quickQuery.value;
        payload.mcp_revision_id = form.elements.quickMcp.value;
        payload.mcp_parameter_additions = mcpParameterEditor?.value() || [];
        payload.target_mapping_id = form.elements.quickMapping.value;
        payload.source_experiment_id = form.elements.quickSourceExperiment.value;
        payload.source_run_id = form.elements.quickSourceRun?.value || "";
        payload.source_agent_id = form.elements.quickSourceAgent?.value || "";
        if (["mem0_platform_v3", "hindsight_cloud", "memos_cloud"].includes(source.memory_strategy)) {
          payload.models = selectedModels("quickDecisionModel");
          if (!payload.models.length) throw new Error(qw("selectModel"));
        } else payload.model = form.elements.quickModel?.value || "";
      }
      buttons.forEach((button) => { button.disabled = true; });
      const result = await apiPost(`/api/platform/experiment-groups/${encodeURIComponent(groupId)}/quick-experiments`, payload);
      const output = form.querySelector("#quickBatchResult");
      output.hidden = false;
      output.innerHTML = `<strong>${qw("createdCount").replace("{count}", result.created.length)}</strong>${launch ? ` · ${qw("startedCount").replace("{count}", result.started.length)}` : ""}${result.failures.length ? ` · ${qw("failedCount").replace("{count}", result.failures.length)}` : ""}
        <ul>${result.created.map((item) => `<li><a href="/experiments/${encodeURIComponent(item.experiment_id)}">${escapeHtml(item.name)}</a>${result.started.some((run) => run.experiment_id === item.experiment_id) ? ` · <a href="/runs/${encodeURIComponent(result.started.find((run) => run.experiment_id === item.experiment_id).run_id)}">${t("nav.runs")}</a>` : ""}</li>`).join("")}${result.failures.map((item) => `<li class="error-text">${escapeHtml(item.model || item.experiment_id || "")}: ${escapeHtml(item.error)}</li>`).join("")}</ul>`;
    } catch (error) { showToast(error.message); }
    finally { buttons.forEach((button) => { button.disabled = false; }); }
  });
}
