const STEPS = ["invite", "memory", "exclusions", "preview", "list", "playlist"];
const MAX_EXCLUSIONS = 12;

const PLAYLIST_ERRORS = {
  denied: "Spotify was not authorized. The M3U and the search links are still here.",
  state: "The Spotify return did not match this session. Start the playlist again.",
  cookie: "The browser did not keep the session cookie through Spotify. Start the playlist again from this page.",
  session: "This session is no longer on the server.",
  invite: "The invite token did not match. Open the session again.",
  upstream: "Spotify did not finish the playlist. The M3U is still available.",
  empty: "No track cleared the exclusions, so no playlist was created. The search links are still here.",
  stale: "That Spotify step expired. Start it again.",
};

const state = {
  sessionId: null,
  token: null,
  label: "",
  step: "invite",
  busy: false,
  invite: null,
  upload: null,
  exclusions: null,
  preview: null,
  candidates: null,
  exportPlan: null,
};

const statusEl = document.getElementById("status");
const errorEl = document.getElementById("error");

function setStatus(text) {
  statusEl.textContent = text || "";
}

function setError(text) {
  if (!text) {
    errorEl.hidden = true;
    errorEl.textContent = "";
    return;
  }
  errorEl.hidden = false;
  errorEl.textContent = text;
}

function messageFrom(data, status) {
  if (!data) return `The server returned ${status}.`;
  const detail = data.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail.message === "string") return detail.message;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => (item && item.msg) || "Check the form and try again.")
      .join(" ");
  }
  return `The server returned ${status}.`;
}

async function api(path, { method = "GET", body = null, token = null, accept = "application/json" } = {}) {
  const headers = { Accept: accept };
  if (body !== null) headers["Content-Type"] = "application/json";
  if (token) headers["X-Invite-Token"] = token;
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body !== null ? JSON.stringify(body) : undefined,
    });
  } catch (_err) {
    throw new Error("The server did not respond. Check the connection and try this step again.");
  }
  if (accept !== "application/json") {
    if (!response.ok) {
      const text = await response.text();
      let data = null;
      try {
        data = JSON.parse(text);
      } catch (_parse) {
        data = null;
      }
      throw new Error(messageFrom(data, response.status));
    }
    return response;
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (_parse) {
      data = { detail: text.slice(0, 300) };
    }
  }
  if (!response.ok) {
    const error = new Error(messageFrom(data, response.status));
    error.payload = data;
    throw error;
  }
  return data;
}

function persist() {
  if (!state.sessionId || !state.token) return;
  const preview = state.preview
    ? {
        signals: state.preview.signals,
        withheld_by_exclusions: state.preview.withheld_by_exclusions,
        unresolved_interests: state.preview.unresolved_interests,
        unresolved_exclusions: state.preview.unresolved_exclusions,
        what_qloo_receives: state.preview.what_qloo_receives,
        explicit_interests: state.preview.explicit_interests,
        photo_count: state.preview.photo_count,
        disclaimer: state.preview.disclaimer,
        claims: state.preview.claims,
        guide: state.preview.guide,
      }
    : null;
  sessionStorage.setItem(
    "gml.v1",
    JSON.stringify({
      sessionId: state.sessionId,
      token: state.token,
      label: state.label,
      preview,
      candidates: state.candidates,
      progress: {
        upload: Boolean(state.upload),
        exclusions: Boolean(state.exclusions),
      },
    }),
  );
}

function restore() {
  const raw = sessionStorage.getItem("gml.v1");
  if (!raw) return false;
  try {
    const saved = JSON.parse(raw);
    if (!saved.sessionId || !saved.token) return false;
    state.sessionId = saved.sessionId;
    state.token = saved.token;
    state.label = saved.label || "";
    state.preview = saved.preview;
    state.candidates = saved.candidates;
    state.invite = { invite_token: saved.token, caregiver_label: saved.label };
    if (saved.progress) {
      state.upload = saved.progress.upload ? { restored: true } : null;
      state.exclusions = saved.progress.exclusions ? { restored: true } : null;
    } else if (saved.preview || saved.candidates) {
      state.upload = { restored: true };
      state.exclusions = { restored: true };
    }
    return true;
  } catch (_err) {
    return false;
  }
}

function stepReady(name) {
  if (name === "invite") return true;
  if (!state.sessionId) return false;
  if (name === "memory") return true;
  if (name === "exclusions") return Boolean(state.upload);
  if (name === "preview") return Boolean(state.exclusions);
  if (name === "list") return Boolean(state.preview || state.candidates);
  if (name === "playlist") return Boolean(state.candidates || state.exportPlan);
  return false;
}

function show(step, { focus = true } = {}) {
  state.step = step;
  for (const name of STEPS) {
    const panel = document.getElementById(`panel-${name}`);
    const button = document.querySelector(`[data-step="${name}"]`);
    panel.hidden = name !== step;
    button.setAttribute("aria-current", name === step ? "step" : "false");
    button.disabled = state.busy || !stepReady(name);
  }
  statusEl.setAttribute("aria-busy", state.busy ? "true" : "false");
  if (focus) {
    const title = document.getElementById(`${step}-title`);
    if (title) title.focus();
  }
}

function setBusy(busy) {
  state.busy = busy;
  document.querySelectorAll("main button, main input, main textarea, main select").forEach((control) => {
    control.disabled = busy;
  });
  show(state.step, { focus: false });
}

function showGuide(id, guide) {
  const node = document.getElementById(id);
  if (node) node.textContent = (guide && guide.note) || "";
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

async function createInvite(label) {
  const data = await api("/invites", { method: "POST", body: { caregiver_label: label } });
  state.sessionId = data.session_id;
  state.token = data.invite_token;
  state.label = data.caregiver_label;
  state.invite = data;
  state.upload = null;
  state.exclusions = null;
  state.preview = null;
  state.candidates = null;
  state.exportPlan = null;
  document.getElementById("invite-token").textContent = data.invite_token;
  document.getElementById("invite-result").hidden = false;
  showGuide("guide-invite", data.guide);
  persist();
  setStatus(`Session open for ${data.caregiver_label}.`);
}

async function saveMemory() {
  if (state.candidates) {
    throw new Error(
      "This session is sealed. The family note was cleared. Open a new session to add another memory.",
    );
  }
  const interests = document
    .getElementById("interests")
    .value.split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
  const body = {
    photo_path: document.getElementById("photo").value.trim(),
    anecdote: document.getElementById("anecdote").value.trim(),
    interests,
  };
  const data = await api(`/sessions/${state.sessionId}/uploads`, {
    method: "POST",
    body,
    token: state.token,
  });
  state.upload = data;
  const result = document.getElementById("memory-result");
  result.hidden = false;
  result.textContent = data.photo_note;
  showGuide("guide-memory", data.guide);
  persist();
  setStatus("Memory saved. Record exclusions before any list.");
  show("exclusions");
}

function exclusionPayloadFromForm() {
  const rows = document.querySelectorAll(".exclusion-row");
  const exclusions = [];
  for (const row of rows) {
    const label = row.querySelector(".label").value.trim();
    const reason = row.querySelector(".reason").value.trim();
    const kind = row.querySelector(".kind").value;
    if (!label) continue;
    const item = { label, kind };
    if (reason) item.reason = reason;
    exclusions.push(item);
  }
  return exclusions;
}

function renderExclusionAck(data) {
  state.exclusions = data;
  const box = document.getElementById("exclusion-result");
  box.hidden = false;
  box.replaceChildren();
  const count = (data.exclusions || []).length;
  box.appendChild(
    el(
      "p",
      null,
      count
        ? `${count} exclusion${count === 1 ? "" : "s"} recorded.`
        : "Recorded: nothing to exclude.",
    ),
  );
  if (data.unresolved_exclusions && data.unresolved_exclusions.length) {
    box.appendChild(
      el(
        "p",
        null,
        `No Qloo id for: ${data.unresolved_exclusions.join(", ")}. Those still filter by name, and a paraphrase can slip through.`,
      ),
    );
  }
  if (data.resolution_warning) {
    box.appendChild(el("p", null, data.resolution_warning));
  }
  showGuide("guide-exclusions", data.guide);
  persist();
}

async function saveExclusions(exclusions) {
  const data = await api(`/sessions/${state.sessionId}/exclusions`, {
    method: "POST",
    body: { exclusions },
    token: state.token,
  });
  renderExclusionAck(data);
  setStatus("Exclusions recorded. Build the preview to sign what Qloo will receive.");
  show("preview");
}

function addList(parent, title, items) {
  const block = el("div");
  block.appendChild(el("h3", null, title));
  if (!items.length) {
    block.appendChild(el("p", null, "None."));
    parent.appendChild(block);
    return;
  }
  const list = el("ul");
  for (const item of items) list.appendChild(el("li", null, item));
  block.appendChild(list);
  parent.appendChild(block);
}

function renderPreview(preview) {
  state.preview = preview;
  const sheet = document.getElementById("preview-sheet");
  sheet.hidden = false;
  sheet.replaceChildren();
  const sleeve = el("div", "sleeve");
  sleeve.setAttribute("aria-hidden", "true");
  sheet.appendChild(sleeve);
  sheet.appendChild(el("p", null, preview.disclaimer || ""));

  const grid = el("div", "boundary");
  const sent = el("div");
  addList(
    sent,
    "Signal ids Qloo will receive",
    (preview.signals || []).map((signal) => `${signal.name} (${signal.kind})`),
  );
  const ids = (preview.what_qloo_receives && preview.what_qloo_receives.signal_ids) || [];
  sent.appendChild(el("p", "meta", ids.length ? ids.join(", ") : "No signal ids."));
  if (preview.what_qloo_receives && preview.what_qloo_receives.note) {
    sent.appendChild(el("p", "meta", preview.what_qloo_receives.note));
  }
  if (preview.unresolved_interests && preview.unresolved_interests.length) {
    sent.appendChild(
      el(
        "p",
        null,
        `Not sent, because they did not resolve: ${preview.unresolved_interests.join(", ")}. Nothing was guessed.`,
      ),
    );
  }
  grid.appendChild(sent);

  const held = el("div");
  addList(
    held,
    "Withheld by exclusions",
    (preview.withheld_by_exclusions || []).map(
      (item) => `${item.name} (matched ${item.matched_exclusion})`,
    ),
  );
  const claims = preview.claims || {};
  addList(held, "This session will not claim", claims.wont || []);
  grid.appendChild(held);
  sheet.appendChild(grid);

  const stays = el("div");
  stays.appendChild(el("h3", null, "Stays in the family record"));
  const anecdotes = preview.anecdotes || [];
  if (anecdotes.length) {
    const list = el("ul");
    for (const anecdote of anecdotes) list.appendChild(el("li", null, anecdote));
    stays.appendChild(list);
  } else if (state.candidates) {
    stays.appendChild(
      el("p", null, "The family note was cleared when the list was built. It was not sent to Qloo."),
    );
  } else {
    stays.appendChild(
      el(
        "p",
        null,
        "The family note is still on the server. This browser does not keep a second copy, and it is not sent to Qloo.",
      ),
    );
  }
  stays.appendChild(
    el("p", "meta", `Photo notes on file: ${preview.photo_count}. The files are not opened.`),
  );
  sheet.appendChild(stays);

  if (claims.will && claims.will.length) {
    addList(sheet, "What this session is for", claims.will);
  }
  if (claims.background) {
    const note = el("p", "background", claims.background);
    sheet.appendChild(note);
  }
  if (preview.unresolved_exclusions && preview.unresolved_exclusions.length) {
    sheet.appendChild(
      el(
        "p",
        null,
        `Exclusions without a Qloo id: ${preview.unresolved_exclusions.join(", ")}. Name matching still applies.`,
      ),
    );
  }
  showGuide("guide-preview", preview.guide);
  document.getElementById("preview-next").hidden = false;
  persist();
}

async function buildPreview() {
  const data = await api(`/sessions/${state.sessionId}/preview`, {
    method: "POST",
    token: state.token,
  });
  renderPreview(data);
  setStatus("Preview signed. Request the list only if this boundary is right.");
}

function renderCandidates(body) {
  state.candidates = body;
  const root = document.getElementById("list-body");
  root.replaceChildren();
  if (body.boundary) root.appendChild(el("p", null, body.boundary));
  const music = (body.candidates || []).filter((item) => item.type === "music");
  const rest = (body.candidates || []).filter((item) => item.type !== "music");
  if (!music.length && !rest.length) {
    const removed = body.removed_by_exclusions || [];
    root.appendChild(
      el(
        "p",
        null,
        removed.length
          ? "Every name matched an exclusion, so the list is empty. That is the gate working. Start a new session if the family wants a different boundary."
          : "No names came back. Add a clearer interest in a new session. Interests are never guessed from the photo or the anecdote.",
      ),
    );
  }
  if (music.length) {
    root.appendChild(el("h3", null, "Music"));
    for (const item of music) {
      const row = el("div", "track-row");
      row.appendChild(el("p", "music-name", item.name));
      if (item.why) row.appendChild(el("p", "why", item.why));
      root.appendChild(row);
    }
  }
  if (rest.length) {
    root.appendChild(el("h3", null, "Film and place"));
    const list = el("ul", "quiet-list");
    for (const item of rest) {
      const line = el("li");
      line.appendChild(el("span", null, `${item.name} (${item.type})`));
      if (item.why) line.appendChild(el("p", "why", item.why));
      list.appendChild(line);
    }
    root.appendChild(list);
  }
  const removed = body.removed_by_exclusions || [];
  if (removed.length) {
    root.appendChild(el("h3", null, "Kept out"));
    const list = el("ul");
    for (const item of removed) {
      list.appendChild(el("li", null, `${item.name} (matched ${item.matched_exclusion})`));
    }
    root.appendChild(list);
  }
  if (body.types_failed && body.types_failed.length) {
    root.appendChild(
      el(
        "p",
        null,
        `Some categories did not return: ${body.types_failed.map((item) => item.type).join(", ")}. The others are listed.`,
      ),
    );
  }
  showGuide("guide-list", body.guide);
  persist();
}

async function requestList() {
  const data = await api(`/sessions/${state.sessionId}/candidates`, {
    method: "POST",
    token: state.token,
  });
  renderCandidates(data);
  setStatus("List ready. The anecdote and photo note were cleared on the server.");
  show("list");
}

function renderExport(plan) {
  state.exportPlan = plan;
  const root = document.getElementById("playlist-body");
  root.replaceChildren();
  root.appendChild(el("p", "music-name", plan.name));
  root.appendChild(el("p", null, plan.description));
  if (plan.playlist && plan.playlist.url) {
    const link = el("a", "button", "Open the private playlist");
    link.href = plan.playlist.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    const actions = el("div", "actions");
    actions.appendChild(link);
    root.appendChild(actions);
    root.appendChild(el("p", "meta", "The playlist is private."));
  } else if (plan.spotify_configured) {
    const button = el("button", "primary", "Create the private Spotify playlist");
    button.type = "button";
    button.addEventListener("click", startSpotify);
    const actions = el("div", "actions");
    actions.appendChild(button);
    root.appendChild(actions);
  } else {
    root.appendChild(
      el(
        "p",
        null,
        "This server has no Spotify client id. The M3U and the search links are the playlist. YouTube Music and Apple Music are not part of this session.",
      ),
    );
  }
  const download = el("button", "secondary", "Download the M3U");
  download.type = "button";
  download.addEventListener("click", downloadM3u);
  const row = el("div", "actions");
  row.appendChild(download);
  root.appendChild(row);

  const tracks = plan.tracks || [];
  if (!tracks.length) {
    root.appendChild(
      el(
        "p",
        null,
        "No music cleared the exclusions, so there is nothing to put in a playlist. Film and place names stay on the list.",
      ),
    );
  } else {
    const list = el("ul", "quiet-list");
    for (const track of tracks) {
      const item = el("li", "track-row");
      const label = track.title ? `${track.artist} - ${track.title}` : track.artist;
      if (track.search_url) {
        const link = document.createElement("a");
        link.href = track.search_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = label;
        item.appendChild(link);
      } else {
        item.appendChild(el("span", null, label));
      }
      list.appendChild(item);
    }
    root.appendChild(list);
  }
  const withheld = plan.withheld || [];
  if (withheld.length) {
    root.appendChild(el("h3", null, "Kept out of the playlist"));
    const list = el("ul");
    for (const item of withheld) {
      let why = `${item.name} (no track cleared)`;
      if (item.field === "search") {
        why = `${item.name} (Spotify search did not return a track)`;
      } else if (item.matched_exclusion) {
        why = `${item.name} (matched ${item.matched_exclusion})`;
      }
      list.appendChild(el("li", null, why));
    }
    root.appendChild(list);
  }
  showGuide("guide-playlist", plan.guide);
  persist();
}

async function loadExport() {
  const data = await api(`/sessions/${state.sessionId}/export`, { token: state.token });
  if (data.candidates) {
    renderCandidates({
      ...(state.candidates || {}),
      boundary: data.boundary || (state.candidates && state.candidates.boundary) || "",
      removed_by_exclusions: data.removed_by_exclusions || [],
      candidates: data.candidates,
      guide: data.guide,
    });
  }
  renderExport(data);
  return data;
}

async function downloadM3u() {
  const response = await api(`/sessions/${state.sessionId}/playlist.m3u`, {
    token: state.token,
    accept: "audio/x-mpegurl",
  });
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "guided-memory-lane.m3u";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  setStatus("M3U download started.");
}

async function startSpotify() {
  setBusy(true);
  setError("");
  setStatus("Sending you to Spotify to approve a private playlist.");
  try {
    const data = await api(`/sessions/${state.sessionId}/spotify/start`, {
      method: "POST",
      token: state.token,
    });
    if (!data.authorize_url) throw new Error("Spotify did not return an authorization link.");
    window.location.assign(data.authorize_url);
  } catch (err) {
    const fallback = err.payload && err.payload.detail && err.payload.detail.fallback;
    setError(err.message || "Spotify could not start.");
    if (fallback === "m3u") setStatus("Use the M3U or the search links.");
    setBusy(false);
  }
}

function addExclusionRow(values = {}) {
  const rows = document.getElementById("exclusion-rows");
  if (rows.children.length >= MAX_EXCLUSIONS) return;
  const row = el("div", "exclusion-row");
  const label = document.createElement("input");
  label.className = "label";
  label.maxLength = 80;
  label.placeholder = "A name or a theme";
  label.setAttribute("aria-label", "Exclusion label");
  label.value = values.label || "";
  const kind = document.createElement("select");
  kind.className = "kind";
  kind.setAttribute("aria-label", "Exclusion kind");
  for (const [value, text] of [
    ["topic", "A theme"],
    ["entity", "A specific name"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = text;
    if (values.kind === value) option.selected = true;
    kind.appendChild(option);
  }
  const reason = document.createElement("input");
  reason.className = "reason";
  reason.maxLength = 200;
  reason.placeholder = "Why it stays out (optional)";
  reason.setAttribute("aria-label", "Why this stays out");
  reason.value = values.reason || "";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "secondary remove";
  remove.textContent = "Remove this exclusion";
  remove.addEventListener("click", () => {
    const parent = document.getElementById("exclusion-rows");
    if (parent.children.length > 1) {
      row.remove();
      return;
    }
    label.value = "";
    reason.value = "";
    kind.value = "topic";
  });
  if (state.busy) {
    label.disabled = true;
    kind.disabled = true;
    reason.disabled = true;
    remove.disabled = true;
  }
  row.append(label, kind, reason, remove);
  rows.appendChild(row);
}

function fillMemory(sample) {
  document.getElementById("photo").value = sample.photo_path;
  document.getElementById("anecdote").value = sample.anecdote;
  document.getElementById("interests").value = (sample.interests || []).join(", ");
}

function fillExclusions(exclusions) {
  const rows = document.getElementById("exclusion-rows");
  rows.replaceChildren();
  const list = exclusions && exclusions.length ? exclusions : [{}];
  for (const item of list) addExclusionRow(item);
}

async function openSample() {
  const started = performance.now();
  setBusy(true);
  setError("");
  try {
    setStatus("Opening the sample family.");
    const sample = await api("/sample-family");
    document.getElementById("caregiver").value = sample.caregiver_label;
    await createInvite(sample.caregiver_label);
    show("memory", { focus: false });
    fillMemory(sample);
    setStatus("Saving the sample memory.");
    await saveMemory();
    fillExclusions(sample.exclusions);
    setStatus("Recording the sample exclusion.");
    await saveExclusions(exclusionPayloadFromForm());
    setStatus("Building the preview.");
    await buildPreview();
    setStatus("Requesting the list.");
    await requestList();
    setStatus("Preparing the playlist export.");
    show("playlist", { focus: false });
    await loadExport();
    const seconds = Math.max(1, Math.round((performance.now() - started) / 1000));
    setStatus(`Sample family is ready (${seconds}s). Open Preview in the steps to see the boundary.`);
  } catch (err) {
    setError(err.message || "The sample family stopped.");
    setStatus("");
  } finally {
    setBusy(false);
  }
}

document.getElementById("form-invite").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  setBusy(true);
  setError("");
  try {
    await createInvite(document.getElementById("caregiver").value.trim());
  } catch (err) {
    setError(err.message || "The invite was not created.");
  } finally {
    setBusy(false);
  }
});

document.getElementById("sample").addEventListener("click", () => {
  if (!state.busy) openSample();
});

document.getElementById("copy-token").addEventListener("click", async () => {
  if (!state.token) return;
  try {
    await navigator.clipboard.writeText(state.token);
    setStatus("Invite token copied.");
  } catch (_err) {
    setStatus("Select the token and copy it. The browser blocked clipboard access.");
  }
});

document.getElementById("to-memory").addEventListener("click", () => show("memory"));

document.getElementById("form-memory").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy || !state.token) return;
  setBusy(true);
  setError("");
  try {
    await saveMemory();
  } catch (err) {
    setError(err.message || "The memory was not saved.");
  } finally {
    setBusy(false);
  }
});

document.getElementById("add-exclusion").addEventListener("click", () => addExclusionRow());

document.getElementById("form-exclusions").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy || !state.token) return;
  const exclusions = exclusionPayloadFromForm();
  if (!exclusions.length) {
    setError("Add a label, or use the acknowledgment that nothing is excluded.");
    return;
  }
  setBusy(true);
  setError("");
  try {
    await saveExclusions(exclusions);
  } catch (err) {
    setError(err.message || "Exclusions were not recorded.");
  } finally {
    setBusy(false);
  }
});

document.getElementById("exclude-none").addEventListener("click", async () => {
  if (state.busy || !state.token) return;
  setBusy(true);
  setError("");
  try {
    await saveExclusions([]);
  } catch (err) {
    setError(err.message || "The acknowledgment was not saved.");
  } finally {
    setBusy(false);
  }
});

document.getElementById("form-preview").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy || !state.token) return;
  setBusy(true);
  setError("");
  setStatus("Building the preview.");
  try {
    await buildPreview();
  } catch (err) {
    setError(err.message || "The preview was not built.");
    setStatus("");
  } finally {
    setBusy(false);
  }
});

document.getElementById("request-list").addEventListener("click", async () => {
  if (state.busy || !state.token) return;
  setBusy(true);
  setError("");
  setStatus("Requesting the list.");
  try {
    await requestList();
  } catch (err) {
    setError(err.message || "The list was not requested.");
    setStatus("");
  } finally {
    setBusy(false);
  }
});

document.getElementById("to-playlist").addEventListener("click", () => {
  if (state.busy || !state.token) return;
  openPlaylist();
});

document.getElementById("clear-session").addEventListener("click", async () => {
  if (state.busy || !state.token || !state.sessionId) return;
  setBusy(true);
  setError("");
  try {
    await api(`/sessions/${state.sessionId}`, { method: "DELETE", token: state.token });
    sessionStorage.removeItem("gml.v1");
    state.sessionId = null;
    state.token = null;
    state.upload = null;
    state.exclusions = null;
    state.preview = null;
    state.candidates = null;
    state.exportPlan = null;
    document.getElementById("invite-result").hidden = true;
    setStatus("Session cleared from this server.");
    show("invite");
  } catch (err) {
    setError(err.message || "The session was not cleared.");
  } finally {
    setBusy(false);
  }
});

function revealInvite() {
  if (!state.token) return;
  document.getElementById("invite-token").textContent = state.token;
  document.getElementById("invite-result").hidden = false;
}

function furthestStep() {
  if (state.candidates) return "list";
  if (state.preview) return "preview";
  if (state.exclusions) return "exclusions";
  if (state.upload) return "memory";
  return "invite";
}

async function openPlaylist() {
  show("playlist", { focus: false });
  if (state.exportPlan) {
    renderExport(state.exportPlan);
    setStatus("Playlist export is ready.");
    return;
  }
  if (!state.token || !state.candidates) return;
  setBusy(true);
  setError("");
  try {
    await loadExport();
    setStatus("Playlist export is ready.");
  } catch (err) {
    setError(err.message || "The playlist export is not ready.");
  } finally {
    setBusy(false);
  }
}

document.querySelectorAll(".rail button").forEach((button) => {
  button.addEventListener("click", () => {
    const step = button.getAttribute("data-step");
    if (!step || state.busy || !stepReady(step)) return;
    if (step === "playlist") {
      openPlaylist();
      return;
    }
    show(step);
    if (step === "preview" && state.preview) renderPreview(state.preview);
    if (step === "list" && state.candidates) renderCandidates(state.candidates);
  });
});

addExclusionRow();
show("invite", { focus: false });
showGuide("guide-invite", {
  note: "Qloo is not called on this step. The invite only opens the session.",
});

const params = new URLSearchParams(window.location.search);
const playlistFlag = params.get("playlist");
if (playlistFlag) {
  const code = params.get("code") || "";
  window.history.replaceState({}, "", "/");
  if (restore()) {
    revealInvite();
    if (state.preview) renderPreview(state.preview);
    if (state.candidates) renderCandidates(state.candidates);
    show("playlist", { focus: false });
    setBusy(true);
    loadExport()
      .then(() => {
        if (playlistFlag === "ready") {
          setStatus("Spotify returned. Open the private playlist if the link is here.");
        } else {
          setError(PLAYLIST_ERRORS[code] || "Spotify did not finish. The M3U is still available.");
        }
      })
      .catch((err) => {
        setError(err.message || "The session could not be restored.");
      })
      .finally(() => setBusy(false));
  } else {
    setError("Spotify returned, but this browser has no session to attach it to.");
  }
} else if (restore()) {
  revealInvite();
  if (state.preview) renderPreview(state.preview);
  if (state.candidates) {
    renderCandidates(state.candidates);
    showGuide("guide-memory", {
      note: "This session is sealed. The family note was cleared. Open a new session to change the memory.",
    });
  }
  show(furthestStep(), { focus: false });
  if (state.label) setStatus(`Session restored for ${state.label}.`);
}
