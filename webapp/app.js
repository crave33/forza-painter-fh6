const state = {
  settings: [],
  images: [],
  jsonFiles: [],
  selectedImage: "",
  selectedJson: "",
  pollTimer: null,
};

const els = {
  processSelect: document.querySelector("#processSelect"),
  refreshProcessesButton: document.querySelector("#refreshProcessesButton"),
  openOutputButton: document.querySelector("#openOutputButton"),
  statusText: document.querySelector("#statusText"),
  progressText: document.querySelector("#progressText"),
  addImagesButton: document.querySelector("#addImagesButton"),
  removeImagesButton: document.querySelector("#removeImagesButton"),
  imageList: document.querySelector("#imageList"),
  profileSelect: document.querySelector("#profileSelect"),
  profileDescription: document.querySelector("#profileDescription"),
  useCustomInput: document.querySelector("#useCustomInput"),
  stopAtInput: document.querySelector("#stopAtInput"),
  maxResolutionInput: document.querySelector("#maxResolutionInput"),
  randomSamplesInput: document.querySelector("#randomSamplesInput"),
  mutatedSamplesInput: document.querySelector("#mutatedSamplesInput"),
  saveAtInput: document.querySelector("#saveAtInput"),
  generateButton: document.querySelector("#generateButton"),
  stopGenerateButton: document.querySelector("#stopGenerateButton"),
  generateTabButton: document.querySelector("#generateTabButton"),
  importTabButton: document.querySelector("#importTabButton"),
  generatePanel: document.querySelector("#generatePanel"),
  importPanel: document.querySelector("#importPanel"),
  previewImage: document.querySelector("#previewImage"),
  previewEmpty: document.querySelector("#previewEmpty"),
  previewStage: document.querySelector(".previewStage"),
  addJsonButton: document.querySelector("#addJsonButton"),
  useGeneratedButton: document.querySelector("#useGeneratedButton"),
  jsonList: document.querySelector("#jsonList"),
  gameSelect: document.querySelector("#gameSelect"),
  pidInput: document.querySelector("#pidInput"),
  layerCountInput: document.querySelector("#layerCountInput"),
  countAddressInput: document.querySelector("#countAddressInput"),
  tableAddressInput: document.querySelector("#tableAddressInput"),
  importButton: document.querySelector("#importButton"),
  clearLogsButton: document.querySelector("#clearLogsButton"),
  logOutput: document.querySelector("#logOutput"),
};

function api() {
  return window.pywebview.api;
}

async function bootstrap() {
  bindEvents();
  setStatus("Loading...");
  const payload = await api().initial_state();
  state.settings = payload.settings || [];
  state.images = payload.images || [];
  state.jsonFiles = payload.jsonFiles || [];
  renderSettings(payload.selectedProfile);
  renderImages();
  renderJsonFiles();
  renderProcesses(payload.processes || []);
  setStatus(payload.status || "Ready");
  setProgress(payload.progress || "");
  startPolling();
}

function bindEvents() {
  els.refreshProcessesButton.addEventListener("click", refreshProcesses);
  els.openOutputButton.addEventListener("click", () => api().open_output_folder());
  els.addImagesButton.addEventListener("click", chooseImages);
  els.removeImagesButton.addEventListener("click", removeImages);
  els.profileSelect.addEventListener("change", syncProfileFields);
  els.generateButton.addEventListener("click", startGenerate);
  els.stopGenerateButton.addEventListener("click", () => api().stop_generate());
  els.addJsonButton.addEventListener("click", chooseJson);
  els.useGeneratedButton.addEventListener("click", useGeneratedOutputs);
  els.importButton.addEventListener("click", startImport);
  els.clearLogsButton.addEventListener("click", () => {
    els.logOutput.textContent = "";
  });
  els.generateTabButton.addEventListener("click", () => showTab("generate"));
  els.importTabButton.addEventListener("click", () => showTab("import"));
  els.processSelect.addEventListener("change", syncPidFromProcess);
}

function showTab(name) {
  const tabs = {
    generate: [els.generateTabButton, els.generatePanel],
    import: [els.importTabButton, els.importPanel],
  };
  Object.entries(tabs).forEach(([key, pair]) => {
    pair[0].classList.toggle("is-active", key === name);
    pair[1].hidden = key !== name;
  });
}

function renderSettings(selectedLabel = "") {
  els.profileSelect.innerHTML = "";
  state.settings.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.label;
    option.textContent = item.label;
    els.profileSelect.append(option);
  });
  if (selectedLabel) {
    els.profileSelect.value = selectedLabel;
  }
  syncProfileFields();
}

function selectedSetting() {
  return state.settings.find((item) => item.label === els.profileSelect.value) || state.settings[0] || null;
}

function syncProfileFields() {
  const item = selectedSetting();
  if (!item) {
    return;
  }
  const values = item.values || {};
  els.profileDescription.textContent = item.description || "";
  if (!els.useCustomInput.checked) {
    els.stopAtInput.value = values.stopAt || "3000";
    els.maxResolutionInput.value = values.maxResolution || "1200";
    els.randomSamplesInput.value = values.randomSamples || "3000";
    els.mutatedSamplesInput.value = values.mutatedSamples || "1000";
    els.saveAtInput.value = values.saveAt || values.stopAt || "3000";
  }
}

async function chooseImages() {
  const payload = await api().choose_images();
  state.images = payload.images || [];
  renderImages();
  setPreview(payload.preview || "");
}

async function removeImages() {
  const payload = await api().remove_images();
  state.images = payload.images || [];
  state.selectedImage = "";
  renderImages();
}

function renderImages() {
  renderFileList(els.imageList, state.images, state.selectedImage, async (path) => {
    state.selectedImage = path;
    renderImages();
    setPreview(await api().preview_image(path));
  });
}

async function chooseJson() {
  const payload = await api().choose_json();
  state.jsonFiles = payload.jsonFiles || [];
  renderJsonFiles();
  setPreview(payload.preview || "");
}

async function useGeneratedOutputs() {
  const payload = await api().use_generated_outputs();
  state.jsonFiles = payload.jsonFiles || [];
  renderJsonFiles();
}

function renderJsonFiles() {
  renderFileList(els.jsonList, state.jsonFiles, state.selectedJson, async (path) => {
    state.selectedJson = path;
    renderJsonFiles();
    setPreview(await api().preview_json(path));
  });
}

function renderFileList(container, paths, selectedPath, onClick) {
  container.innerHTML = "";
  if (!paths.length) {
    const empty = document.createElement("div");
    empty.className = "helpText";
    empty.textContent = "No files selected.";
    container.append(empty);
    return;
  }
  paths.forEach((path) => {
    const button = document.createElement("button");
    button.className = "fileItem";
    button.classList.toggle("is-selected", path === selectedPath);
    button.type = "button";
    button.title = path;
    button.textContent = path;
    button.addEventListener("click", () => onClick(path));
    container.append(button);
  });
}

async function refreshProcesses() {
  renderProcesses(await api().refresh_processes());
}

function renderProcesses(processes) {
  els.processSelect.innerHTML = "";
  if (!processes.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No supported game process detected";
    els.processSelect.append(option);
    return;
  }
  processes.forEach((proc) => {
    const option = document.createElement("option");
    option.value = String(proc.pid || "");
    option.dataset.profile = proc.profile || "fh6";
    option.textContent = proc.label || `${proc.name} pid ${proc.pid}`;
    els.processSelect.append(option);
  });
  syncPidFromProcess();
}

function syncPidFromProcess() {
  const option = els.processSelect.selectedOptions[0];
  els.pidInput.value = option?.value || "";
  if (option?.dataset.profile) {
    els.gameSelect.value = option.dataset.profile;
  }
}

function generationPayload() {
  return {
    selectedProfile: els.profileSelect.value,
    useCustom: els.useCustomInput.checked,
    custom: {
      stopAt: els.stopAtInput.value,
      maxResolution: els.maxResolutionInput.value,
      randomSamples: els.randomSamplesInput.value,
      mutatedSamples: els.mutatedSamplesInput.value,
      saveAt: els.saveAtInput.value,
    },
  };
}

function startGenerate() {
  api().start_generate(generationPayload());
}

function startImport() {
  api().start_import({
    game: els.gameSelect.value,
    pid: els.pidInput.value,
    layerCount: els.layerCountInput.value,
    countAddress: els.countAddressInput.value,
    tableAddress: els.tableAddressInput.value,
  });
}

function setPreview(src) {
  if (!src) {
    els.previewStage.classList.remove("has-image");
    els.previewImage.removeAttribute("src");
    return;
  }
  els.previewImage.src = src;
  els.previewStage.classList.add("has-image");
}

function setStatus(text) {
  els.statusText.textContent = text || "Ready";
}

function setProgress(text) {
  els.progressText.textContent = text || "";
}

function appendLog(line) {
  els.logOutput.textContent += `${line}\n`;
  els.logOutput.scrollTop = els.logOutput.scrollHeight;
}

function startPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
  }
  state.pollTimer = setInterval(pollEvents, 200);
}

async function pollEvents() {
  const events = await api().poll_events();
  events.forEach((event) => {
    if (event.kind === "log") {
      appendLog(event.payload);
    } else if (event.kind === "status") {
      setStatus(event.payload);
    } else if (event.kind === "progress") {
      setProgress(event.payload);
    } else if (event.kind === "preview") {
      setPreview(event.payload);
    } else if (event.kind === "jsonFiles") {
      state.jsonFiles = event.payload || [];
      renderJsonFiles();
    }
  });
}

window.addEventListener("pywebviewready", bootstrap);
