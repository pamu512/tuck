const PLAYLIST_ERRORS = {
  denied: "Spotify was not authorized. The listening file and the search links are still here.",
  state: "The Spotify return did not match this sitting. Start the playlist again.",
  cookie: "The browser did not keep the sitting through Spotify. Start the playlist again from this page.",
  session: "This sitting is no longer on the server.",
  invite: "This sitting could not be opened again. Start once more.",
  upstream: "Spotify did not finish the playlist. The listening file is still here.",
  empty: "No track cleared what you left out, so no playlist was created. The search links are still here.",
  stale: "That Spotify step expired. Start it again.",
};

const state = {
  email: "",
  mode: "register",
  personName: "",
  sessionId: null,
  token: null,
  probeId: null,
  feedbackId: null,
  busy: false,
};

const askEl = document.getElementById("ask");
const asideEl = document.getElementById("aside");
const statusEl = document.getElementById("status");
const errorEl = document.getElementById("error");
const desk = document.getElementById("desk");
const signOut = document.getElementById("sign-out");
const forms = {
  email: document.getElementById("form-email"),
  choice: document.getElementById("account-choice"),
  password: document.getElementById("form-password"),
  who: document.getElementById("form-who"),
  whoChoice: document.getElementById("who-choice"),
  answer: document.getElementById("form-answer"),
  previewActions: document.getElementById("preview-actions"),
  preview: document.getElementById("preview-sheet"),
  listActions: document.getElementById("list-actions"),
  gift: document.getElementById("gift"),
  giftActions: document.getElementById("gift-actions"),
  feedback: document.getElementById("form-feedback"),
};

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

function setBusy(busy) {
  state.busy = busy;
  desk.setAttribute("aria-busy", busy ? "true" : "false");
  document.querySelectorAll("button, input, textarea").forEach((node) => {
    if (node.id === "sign-out") return;
    if (node.id === "ask-for-list-in-sheet" && node.dataset.blocked === "1") {
      node.disabled = true;
      return;
    }
    node.disabled = busy;
  });
}

function messageFrom(data, status) {
  if (!data) return `The server returned ${status}.`;
  const detail = data.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail.message === "string") return detail.message;
  if (Array.isArray(detail)) {
    return detail.map((item) => (item && item.msg) || "Check that and try again.").join(" ");
  }
  return `The server returned ${status}.`;
}

async function api(path, { method = "GET", body = null, token = null, timeoutMs = 0 } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== null) headers["Content-Type"] = "application/json";
  if (token) headers["X-Invite-Token"] = token;
  const controller = timeoutMs > 0 ? new AbortController() : null;
  let timer = null;
  if (controller) {
    timer = setTimeout(() => controller.abort(), timeoutMs);
  }
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body !== null ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
      signal: controller ? controller.signal : undefined,
    });
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw new Error("That took too long. Nothing was sealed. Try Ask for the list again.");
    }
    throw new Error("The server did not respond. Check the connection and try again.");
  } finally {
    if (timer) clearTimeout(timer);
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (_parse) {
      data = null;
    }
  }
  if (!response.ok) {
    throw new Error(messageFrom(data, response.status));
  }
  return data;
}

function hideAll() {
  Object.values(forms).forEach((node) => {
    node.hidden = true;
  });
}

function focusAsk() {
  const field = desk.querySelector("form:not([hidden]) input, form:not([hidden]) textarea");
  if (field) field.focus();
  else askEl.focus();
}

function showAsk(text, panel) {
  askEl.textContent = text;
  hideAll();
  if (panel) panel.hidden = false;
  setError("");
  focusAsk();
}

function rememberSession(sessionId, token) {
  state.sessionId = sessionId;
  state.token = token;
  sessionStorage.setItem("tuck_session", sessionId);
  sessionStorage.setItem("tuck_token", token);
}

function clearSession() {
  state.sessionId = null;
  state.token = null;
  state.probeId = null;
  state.feedbackId = null;
  sessionStorage.removeItem("tuck_session");
  sessionStorage.removeItem("tuck_token");
}

function add(parent, tag, text, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  parent.appendChild(node);
  return node;
}

function sleeve(parent) {
  const rule = document.createElement("div");
  rule.className = "sleeve";
  rule.setAttribute("aria-hidden", "true");
  parent.appendChild(rule);
}

async function openSitting(name) {
  const invite = await api("/invites", {
    method: "POST",
    body: { caregiver_label: name },
  });
  rememberSession(invite.session_id, invite.invite_token);
  state.personName = name;
  await showInterview();
}

async function showInterview() {
  const view = await api(`/sessions/${state.sessionId}/interview`, { token: state.token });
  asideEl.hidden = true;
  signOut.hidden = false;
  if (view.phase === "probe" && view.probe) {
    state.probeId = view.probe.id;
    showAsk(view.asked, forms.answer);
    document.getElementById("exit").hidden = !view.can_exit;
    document.getElementById("answer").value = "";
    return;
  }
  state.probeId = null;
  if (view.phase === "preview") {
    showAsk(view.asked, forms.previewActions);
    return;
  }
  if (view.phase === "list") {
    await showPreview({ resume: true, asked: view.asked });
    return;
  }
  await showGift();
}

async function showPreview(opts = {}) {
  const resume = Boolean(opts.resume);
  setStatus(resume ? "Opening the preview again." : "Building the preview.");
  const preview = await api(`/sessions/${state.sessionId}/preview`, {
    method: "POST",
    token: state.token,
  });
  const askText =
    opts.asked ||
    "Here is what I would ask for. Nothing has been requested yet.";
  showAsk(askText, forms.preview);
  // Keep the standalone list-actions hidden; the primary Ask lives in the sheet.
  forms.listActions.hidden = true;
  const sheet = forms.preview;
  sheet.replaceChildren();
  sleeve(sheet);

  const signals = preview.signals || [];
  const canAsk = signals.length > 0;

  if (preview.persona_summary) {
    add(sheet, "h2", "Who this listening is for");
    add(sheet, "p", preview.persona_summary);
  }
  if (preview.qloo_plain) {
    add(sheet, "h2", "What I would ask for");
    add(sheet, "p", preview.qloo_plain);
  }

  const actions = add(sheet, "p", null, "actions");
  const askBtn = document.createElement("button");
  askBtn.type = "button";
  askBtn.className = "primary";
  askBtn.id = "ask-for-list-in-sheet";
  askBtn.textContent = "Ask for the list";
  if (!canAsk) askBtn.dataset.blocked = "1";
  askBtn.disabled = !canAsk || state.busy;
  askBtn.addEventListener("click", () => requestList());
  actions.appendChild(askBtn);
  if (!canAsk) {
    add(
      sheet,
      "p",
      "I could not match enough of what you named to ask for a list yet. Add a singer, a film, or another clear name, then show the preview again."
    );
  }

  const grid = add(sheet, "div", null, "boundary");
  const left = add(grid, "div");
  add(left, "h2", "I would start from");
  const interests = preview.explicit_interests || [];
  if (!interests.length) add(left, "p", "No names yet.");
  else {
    const list = add(left, "ul", null, "names");
    interests.forEach((name) => add(list, "li", name));
  }
  const unresolved = preview.unresolved_interests || [];
  if (unresolved.length) {
    add(left, "p", `I could not match: ${unresolved.join(", ")}. Those words stay out of the request.`);
  }
  if (signals.length) {
    add(left, "p", `Matched for the request: ${signals.map((s) => s.name).filter(Boolean).join(", ")}.`);
  }
  const right = add(grid, "div");
  add(right, "h2", "Left out");
  const exclusions = preview.exclusions || [];
  if (!exclusions.length) add(right, "p", "You said nothing should be left out.");
  else {
    const list = add(right, "ul");
    exclusions.forEach((item) => add(list, "li", item.label));
  }
  const details = document.createElement("details");
  details.className = "tech";
  const summary = document.createElement("summary");
  summary.textContent = "Technical details";
  details.appendChild(summary);
  if (preview.disclaimer) add(details, "p", preview.disclaimer);
  if (preview.what_qloo_receives && preview.what_qloo_receives.note) {
    add(details, "p", preview.what_qloo_receives.note);
  }
  const ids = (preview.what_qloo_receives && preview.what_qloo_receives.signal_ids) || [];
  if (ids.length) add(details, "p", `Signal ids: ${ids.join(", ")}.`);
  sheet.appendChild(details);
  setStatus("");
  askBtn.scrollIntoView({ behavior: "smooth", block: "center" });
  askBtn.focus();
}

async function requestList() {
  if (state.busy) return;
  setBusy(true);
  setError("");
  setStatus("Asking for the list. This can take up to half a minute.");
  const progress = window.setInterval(() => {
    if (!state.busy) return;
    if (statusEl.textContent.indexOf("still working") === -1) {
      setStatus("Still working on the list. Stay on this page.");
    }
  }, 8000);
  try {
    await api(`/sessions/${state.sessionId}/candidates`, {
      method: "POST",
      token: state.token,
      timeoutMs: 25000,
    });
    setStatus("The list is ready. Opening the listening gift.");
    await showGift();
  } catch (err) {
    setError(err.message || "I could not finish the list. The preview is still here.");
    setStatus("");
    const sheetBtn = document.getElementById("ask-for-list-in-sheet");
    if (sheetBtn) {
      sheetBtn.disabled = false;
      sheetBtn.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  } finally {
    window.clearInterval(progress);
    setBusy(false);
  }
}

function trackTitle(track) {
  if (track.title) return `${track.artist}: ${track.title}`;
  return track.artist || "Untitled";
}

async function showGift() {
  setStatus("Opening the listening gift.");
  const plan = await api(`/sessions/${state.sessionId}/export`, { token: state.token });
  const label = plan.caregiver_label || state.personName || "them";
  showAsk(`Listening for ${label}.`, forms.gift);
  const sheet = forms.gift;
  sheet.classList.add("gift");
  sheet.replaceChildren();
  sleeve(sheet);
  add(sheet, "h2", `Listening for ${label}.`);
  add(sheet, "p", "Play these in order, somewhere quiet. Stop when it stops being easy. Most sittings run from about half an hour to two hours.");
  add(
    sheet,
    "p",
    "Open the private playlist on the caregiver's Spotify, or download the listening file and use the search links. The playlist is not public."
  );
  const tracks = plan.tracks || [];
  if (!tracks.length) add(sheet, "p", "No music cleared what you left out.");
  else {
    const list = add(sheet, "ol");
    tracks.forEach((track) => {
      const item = add(list, "li");
      add(item, "p", trackTitle(track), "music-name");
      if (track.why) add(item, "p", track.why, "why");
      if (track.search_url) {
        const link = add(item, "a", "Search this on Spotify");
        link.href = track.search_url;
        link.rel = "noopener noreferrer";
        link.target = "_blank";
      }
    });
  }
  const labels = [];
  (plan.exclusions || []).forEach((label) => {
    if (label && labels.indexOf(label) === -1) labels.push(label);
  });
  if (labels.length) add(sheet, "p", `Left out: ${labels.join(", ")}.`);
  else add(sheet, "p", "You said nothing should be left out.");
  if (plan.boundary) add(sheet, "p", plan.boundary);
  const actions = forms.giftActions;
  actions.replaceChildren();
  actions.hidden = false;
  if (plan.playlist && plan.playlist.url) {
    const open = add(actions, "a", "Open the private playlist", "button");
    open.href = plan.playlist.url;
    open.rel = "noopener noreferrer";
    open.target = "_blank";
  } else {
    const save = add(actions, "button", "Save a private Spotify playlist");
    save.type = "button";
    save.className = "primary";
    save.addEventListener("click", () => connectSpotify());
  }
  const download = add(actions, "button", "Download the listening file");
  download.type = "button";
  download.className = "secondary";
  download.addEventListener("click", () => downloadGift());
  if (statusEl.textContent === "Opening the listening gift.") setStatus("");
  const sitting = await api(`/sessions/${state.sessionId}/interview`, { token: state.token });
  if (sitting.feedback_id && sitting.feedback_ask) {
    state.feedbackId = sitting.feedback_id;
    askEl.textContent = sitting.feedback_ask;
    forms.feedback.hidden = false;
    forms.gift.hidden = false;
    forms.giftActions.hidden = false;
    document.getElementById("feedback").focus();
  }
}

async function connectSpotify() {
  setError("");
  setBusy(true);
  setStatus("Opening Spotify.");
  try {
    const data = await api(`/sessions/${state.sessionId}/spotify/start`, {
      method: "POST",
      token: state.token,
    });
    if (data && data.authorize_url) window.location.assign(data.authorize_url);
  } catch (err) {
    setError(err.message);
    setStatus("");
  } finally {
    setBusy(false);
  }
}

async function downloadGift() {
  setError("");
  setBusy(true);
  try {
    const response = await fetch(`/sessions/${state.sessionId}/playlist.m3u`, {
      headers: { "X-Invite-Token": state.token, Accept: "audio/x-mpegurl" },
    });
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
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "tuck-listening.m3u";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setStatus("The listening file is downloading.");
  } catch (err) {
    setError(err.message);
  } finally {
    setBusy(false);
  }
}

function setupMic() {
  const mic = document.getElementById("mic");
  const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Speech) {
    mic.hidden = true;
    return;
  }
  mic.hidden = false;
  let listening = null;
  mic.addEventListener("click", () => {
    if (listening) return;
    const field = document.getElementById("answer");
    const rec = new Speech();
    listening = rec;
    rec.lang = document.documentElement.lang || "en-US";
    rec.onresult = (event) => {
      const heard = event.results && event.results[0] && event.results[0][0];
      if (!heard || !heard.transcript) return;
      field.value = field.value ? `${field.value.trim()} ${heard.transcript}` : heard.transcript;
      field.focus();
    };
    rec.onerror = () => setError("The microphone did not catch that. You can type it.");
    rec.onend = () => {
      if (listening === rec) listening = null;
    };
    try {
      rec.start();
      setStatus("Listening. You can edit the words before you send.");
    } catch (_err) {
      listening = null;
      setError("The microphone did not start. You can type it.");
    }
  });
}

forms.email.addEventListener("submit", (event) => {
  event.preventDefault();
  state.email = document.getElementById("email").value.trim();
  asideEl.hidden = true;
  showAsk("Do you already have a Tuck account for this email?", forms.choice);
});

document.getElementById("choose-new").addEventListener("click", () => {
  state.mode = "register";
  document.getElementById("password").autocomplete = "new-password";
  showAsk("Choose a password. At least 10 characters.", forms.password);
});

document.getElementById("choose-existing").addEventListener("click", () => {
  state.mode = "login";
  document.getElementById("password").autocomplete = "current-password";
  showAsk("What is the password for that account?", forms.password);
});

forms.password.addEventListener("submit", async (event) => {
  event.preventDefault();
  const password = document.getElementById("password").value;
  setBusy(true);
  setStatus(state.mode === "login" ? "Checking the account." : "Making the account.");
  try {
    const path = state.mode === "login" ? "/login" : "/accounts";
    const account = await api(path, {
      method: "POST",
      body: { email: state.email, password },
    });
    document.getElementById("password").value = "";
    signOut.hidden = false;
    await land(account);
  } catch (err) {
    setError(err.message);
    setStatus("");
  } finally {
    setBusy(false);
  }
});

async function land(account) {
  state.personName = (account && account.person_name) || "";
  if (state.personName) {
    document.getElementById("who-yes").textContent = `Yes, still ${state.personName}`;
    showAsk(`Are you still helping ${state.personName}?`, forms.whoChoice);
    return;
  }
  showAsk("Who are you helping?", forms.who);
}

forms.who.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = document.getElementById("who").value.trim();
  setBusy(true);
  setStatus("Saving their name.");
  try {
    await api("/me/profile", { method: "POST", body: { person_name: name } });
    await openSitting(name);
    setStatus("");
  } catch (err) {
    setError(err.message);
    setStatus("");
  } finally {
    setBusy(false);
  }
});

document.getElementById("who-yes").addEventListener("click", async () => {
  setBusy(true);
  try {
    await openSitting(state.personName);
    setStatus("");
  } catch (err) {
    setError(err.message);
  } finally {
    setBusy(false);
  }
});

document.getElementById("who-else").addEventListener("click", () => {
  document.getElementById("who").value = "";
  showAsk("Who are you helping?", forms.who);
});

forms.answer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = document.getElementById("answer").value.trim();
  if (!text) {
    setError("Say it in a few words, or say none.");
    return;
  }
  setBusy(true);
  setStatus("Saving that.");
  try {
    await api(`/sessions/${state.sessionId}/interview`, {
      method: "POST",
      token: state.token,
      body: { text },
    });
    await showInterview();
    setStatus("");
  } catch (err) {
    setError(err.message);
    setStatus("");
  } finally {
    setBusy(false);
  }
});

document.getElementById("exit").addEventListener("click", async () => {
  setBusy(true);
  setStatus("Stopping here.");
  try {
    await api(`/sessions/${state.sessionId}/interview`, {
      method: "POST",
      token: state.token,
      body: { exit: true },
    });
    await showInterview();
    setStatus("");
  } catch (err) {
    setError(err.message);
    setStatus("");
  } finally {
    setBusy(false);
  }
});

document.getElementById("build-preview").addEventListener("click", async () => {
  setBusy(true);
  try {
    await showPreview();
  } catch (err) {
    setError(err.message);
    setStatus("");
  } finally {
    setBusy(false);
  }
});

document.getElementById("request-list").addEventListener("click", () => {
  requestList();
});

forms.feedback.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = document.getElementById("feedback").value.trim();
  if (!text || !state.feedbackId) {
    setError("Say it in a few words.");
    return;
  }
  setBusy(true);
  try {
    const saved = await api(`/sessions/${state.sessionId}/feedback`, {
      method: "POST",
      token: state.token,
      body: { id: state.feedbackId, text },
    });
    document.getElementById("feedback").value = "";
    if (saved.pending) {
      state.feedbackId = saved.pending;
      showAsk(saved.ask, forms.feedback);
      forms.gift.hidden = false;
      forms.giftActions.hidden = false;
    } else {
      state.feedbackId = null;
      forms.feedback.hidden = true;
      setStatus(saved.ask);
    }
  } catch (err) {
    setError(err.message);
  } finally {
    setBusy(false);
  }
});

signOut.addEventListener("click", async () => {
  setBusy(true);
  try {
    await api("/logout", { method: "POST" });
  } catch (_err) {
    /* The cookie clear is local even if the server is briefly down. */
  }
  clearSession();
  signOut.hidden = true;
  asideEl.hidden = false;
  document.getElementById("email").value = "";
  showAsk("What email should I use for your account?", forms.email);
  setStatus("");
  setBusy(false);
});

document.getElementById("sample").addEventListener("click", async () => {
  setBusy(true);
  setStatus("Opening the sample family.");
  try {
    const sample = await api("/dev/sample", { method: "POST" });
    rememberSession(sample.session_id, sample.invite_token);
    state.personName = sample.caregiver_label;
    asideEl.hidden = true;
    showAsk(sample.note, forms.previewActions);
    setStatus("");
  } catch (err) {
    setError(err.message);
    setStatus("");
  } finally {
    setBusy(false);
  }
});

function playlistNotice() {
  const params = new URLSearchParams(window.location.search);
  const flag = params.get("playlist");
  if (!flag) return;
  if (flag === "ready") setStatus("The private playlist is ready.");
  else setError(PLAYLIST_ERRORS[params.get("code")] || "Spotify did not finish. The listening file is still here.");
  const clean = window.location.pathname;
  window.history.replaceState({}, "", clean);
}

async function resume() {
  const sessionId = sessionStorage.getItem("tuck_session");
  const token = sessionStorage.getItem("tuck_token");
  if (!sessionId || !token) return false;
  state.sessionId = sessionId;
  state.token = token;
  try {
    await showInterview();
    return true;
  } catch (_err) {
    clearSession();
    return false;
  }
}

async function boot() {
  setBusy(true);
  setStatus("One moment.");
  try {
    const account = await api("/me");
    signOut.hidden = false;
    const resumed = await resume();
    if (!resumed) await land(account);
  } catch (err) {
    const message = err.message || "";
    if (message.indexOf("Sign in") !== -1 || message.indexOf("not configured") !== -1) {
      clearSession();
    }
    if (message.indexOf("not configured") !== -1) setError(message);
    showAsk("What email should I use for your account?", forms.email);
  } finally {
    playlistNotice();
    if (statusEl.textContent === "One moment.") setStatus("");
    setBusy(false);
  }
}

setupMic();
boot();

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.activeElement && document.activeElement !== document.body) {
    setError("");
  }
});
