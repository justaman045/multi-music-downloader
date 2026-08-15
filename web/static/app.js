/* ==========================================================================
   LocalSpot — premium web UI for YT Music Downloader
   Vanilla JS, no dependencies, no build step.
   ========================================================================== */

"use strict";

/* -------------------------------------------------------------------------
 * State
 * ---------------------------------------------------------------------- */

const state = {
  library: null,            // { accounts: [], tracks: [] }
  scope: "all",             // "all" or account slug
  route: "home",
  downloads: {},            // slug -> session snapshot
  refreshing: {},           // slug -> true while refreshing
  player: {
    queue: [],
    index: -1,
    playing: false,
    shuffle: false,
    repeat: "off",          // off | all | one
    volume: Math.max(0, Math.min(1, Number(localStorage.getItem("ls-volume") ?? 0.8))),
    muted: localStorage.getItem("ls-muted") === "1",
    audio: new Audio(),
    lastVol: 0.8,
  },
  token: localStorage.getItem("ls-token") || "",
  _tokenPrompting: false,
  _renderSeq: 0,
  settings: null,            // cached /api/settings
  sleepTimer: { endAt: null, interval: null, endOfSong: false },
  _playReported: false,      // one play report per track playback
};

const QUALITIES = [
  ["best", "Best available source audio"],
  ["mp3_320", "MP3 — 320 kbps"],
  ["mp3_v0", "MP3 — Best VBR (V0)"],
  ["mp3_256", "MP3 — 256 kbps"],
  ["mp3_192", "MP3 — 192 kbps"],
  ["mp3_128", "MP3 — 128 kbps"],
  ["flac", "FLAC — lossless"],
  ["m4a_256", "M4A/AAC — 256 kbps"],
];

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* -------------------------------------------------------------------------
 * Utilities
 * ---------------------------------------------------------------------- */

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtDuration(sec) {
  if (!sec && sec !== 0) return "—";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function fmtTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function fmtSize(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let value = bytes;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[i]}`;
}

const PALETTES = [
  "linear-gradient(135deg,#3a1c71,#d76d77)",
  "linear-gradient(135deg,#0f3443,#34e89e)",
  "linear-gradient(135deg,#c31432,#240b36)",
  "linear-gradient(135deg,#2c3e50,#fd746c)",
  "linear-gradient(135deg,#1a2a6c,#b21f1f)",
  "linear-gradient(135deg,#0f2027,#2c5364)",
  "linear-gradient(135deg,#41295a,#2f0743)",
  "linear-gradient(135deg,#5f2c82,#49a09d)",
];

function paletteFor(text) {
  let hash = 0;
  for (const ch of String(text)) hash = (hash * 31 + ch.codePointAt(0)) | 0;
  return PALETTES[Math.abs(hash) % PALETTES.length];
}

function initialsOf(name) {
  const parts = String(name || "?").trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0] || "").join("").toUpperCase() || "?";
}

function artPlaceholder(name, big = false) {
  return `<div class="art-placeholder" style="background:${paletteFor(name)};font-size:${big ? "72px" : "34px"}">${esc(initialsOf(name))}</div>`;
}

function artImg(url, name, cls = "") {
  return `<img src="${esc(url)}" alt="${esc(name)}" loading="lazy" onerror="this.outerHTML=artPlaceholderFor(this)" class="${cls}"/>`;
}

function artPlaceholderFor(img) {
  const name = img.getAttribute("data-name") || "?";
  const big = img.hasAttribute("data-big");
  return artPlaceholder(name, big);
}

function withToken(url) {
  if (!state.token || !url || !url.startsWith("/api/")) return url;
  const u = new URL(url, location.origin);
  u.searchParams.set("token", state.token);
  return u.href;
}

async function promptToken() {
  const value = await modal(
    "API token required",
    `
    <p style="color:var(--text-dim)">This server requires a token. Enter it to continue.</p>
    <input name="token" class="input" type="password" placeholder="Token" style="width:100%;margin:12px 0" value="${esc(state.token)}"/>
    `,
    [
      { label: "Cancel", value: () => false },
      { label: "Save", variant: "btn-primary", value: () => $("#modal-root input[name='token']").value, autoFocus: { name: "token" } },
    ],
  );
  if (value) {
    state.token = value.trim();
    localStorage.setItem("ls-token", state.token);
    return true;
  }
  return false;
}

async function api(path, opts = {}) {
  const options = { headers: {}, ...opts };
  if (state.token) options.headers["Authorization"] = `Bearer ${state.token}`;
  if (options.body !== undefined && typeof options.body !== "string") {
    options.body = JSON.stringify(options.body);
    options.headers["Content-Type"] = "application/json";
  }
  let res = await fetch(path, options);
  if (res.status === 401 && state.token === "" && !state._tokenPrompting) {
    state._tokenPrompting = true;
    try {
      if (await promptToken()) {
        options.headers["Authorization"] = `Bearer ${state.token}`;
        res = await fetch(path, options);
      }
    } finally {
      state._tokenPrompting = false;
    }
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
  if (!res.ok) {
    throw new Error((data && data.error) || `HTTP ${res.status}`);
  }
  return data;
}

function toast(message, type = "") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  $("#toasts").appendChild(node);
  setTimeout(() => {
    node.style.opacity = "0";
    node.style.transform = "translateX(20px)";
    setTimeout(() => node.remove(), 250);
  }, 3800);
}

function notify(title, body) {
  if (!state.settings?.notifications_enabled) return;
  if (!("Notification" in window)) return;
  if (Notification.permission === "granted") {
    try { new Notification(title, { body, silent: true }); } catch (e) {}
  } else if (Notification.permission !== "denied") {
    Notification.requestPermission().then((p) => {
      if (p === "granted") {
        try { new Notification(title, { body, silent: true }); } catch (e) {}
      }
    });
  }
}

function modal(title, bodyHTML, actions = []) {
  return new Promise((resolve) => {
    const root = $("#modal-root");
    root.innerHTML = `
      <div class="modal-bg"></div>
      <div class="modal">
        <h3>${esc(title)}</h3>
        ${bodyHTML}
        <div class="modal-actions">
          ${actions.map((a, i) =>
            `<button class="btn ${a.variant || ""}" data-a="${i}">${esc(a.label)}</button>`
          ).join("")}
        </div>
      </div>`;
    root.classList.remove("hidden");
    const close = (value) => {
      root.classList.add("hidden");
      root.innerHTML = "";
      root.dataset.open = "0";
      document.removeEventListener("keydown", onKey, true);
      resolve(value);
    };
    root.dataset.open = "1";
    const onKey = (e) => {
      if (e.key === "Escape" && root.dataset.open === "1") {
        e.preventDefault();
        close(null);
      }
    };
    document.addEventListener("keydown", onKey, true);
    $(".modal-bg", root).onclick = () => close(null);
    $$("button[data-a]", root).forEach((btn) => {
      btn.onclick = () => {
        const action = actions[Number(btn.dataset.a)];
        const value = action.value ? action.value() : true;
        close(value === false ? null : value);
      };
    });
    const auto = actions.find((a) => a.autoFocus);
    if (auto) {
      const node = $$("input, select, textarea", root).find((n) => n.name === auto.name);
      if (node) node.focus();
    }
  });
}

function confirmModal(title, body) {
  return modal(
    title,
    `<p>${body}</p>`,
    [
      { label: "Cancel", value: () => false },
      { label: "Remove", variant: "btn-danger", value: () => true, autoFocus: { name: "" } },
    ],
  );
}

/* -------------------------------------------------------------------------
 * Library + scope
 * ---------------------------------------------------------------------- */

async function loadLibrary() {
  state.library = await api("/api/library");
  state.refreshing = state.refreshing || {};
  if (state.token) {
    for (const t of state.library.tracks) {
      if (t.art) t.art = withToken(t.art);
      if (t.stream) t.stream = withToken(t.stream);
    }
  }
  renderAccountsNav();
  return state.library;
}

function selectedAccountName() {
  if (state.scope === "all" || !state.library) return "All accounts";
  const acc = state.library.accounts.find((a) => a.slug === state.scope);
  return acc ? acc.name : "All accounts";
}

function visibleTracks() {
  if (!state.library) return [];
  if (state.scope === "all") return state.library.tracks;
  return state.library.tracks.filter((t) => t.account === state.scope);
}

function normalizeArtist(name) {
  return String(name || "Unknown Artist").split(", ").map((s) => s.trim()).filter(Boolean);
}

function aggregates() {
  const tracks = visibleTracks();
  const artists = new Map();   // name -> {name, count, art}
  const albums = new Map();    // album|artist -> {title, artist, year, count, ids, art}
  const years = new Map();     // year -> count
  const genres = new Map();    // genre -> count

  for (const t of tracks) {
    for (const artist of normalizeArtist(t.artist)) {
      const a = artists.get(artist) || { name: artist, count: 0, art: null };
      a.count += 1;
      if (!a.art && t.art) a.art = t.art;
      artists.set(artist, a);
    }

    if (t.album) {
      const key = `${t.album}###${t.artist}`;
      const al = albums.get(key) || {
        title: t.album, artist: t.artist, year: t.year, count: 0,
        ids: [], art: null, key,
      };
      al.count += 1;
      al.ids.push(t.id);
      if (!al.art && t.art) al.art = t.art;
      albums.set(key, al);
    }

    if (t.year) years.set(t.year, (years.get(t.year) || 0) + 1);
    if (t.genre) genres.set(t.genre, (genres.get(t.genre) || 0) + 1);
  }

  return {
    artists: [...artists.values()].sort((a, b) => a.name.localeCompare(b.name)),
    albums: [...albums.values()].sort((a, b) => a.title.localeCompare(b.title)),
    years: [...years.entries()].map(([year, count]) => ({ year, count })).sort((a, b) => b.year - a.year),
    genres: [...genres.entries()].map(([genre, count]) => ({ genre, count })).sort((a, b) => b.count - a.count || a.genre.localeCompare(b.genre)),
  };
}

function tracksForAlbum(key) {
  const [album, artist] = key.split("###");
  return visibleTracks().filter((t) => t.album === album && t.artist === artist);
}

function tracksForArtist(name) {
  return visibleTracks().filter((t) => normalizeArtist(t.artist).includes(name));
}

function tracksForYear(year) {
  return visibleTracks().filter((t) => t.year === year);
}

function tracksForGenre(genre) {
  return visibleTracks().filter((t) => t.genre === genre);
}

/* -------------------------------------------------------------------------
 * Player
 * ---------------------------------------------------------------------- */

const audio = state.player.audio;

function streamUrl(track) {
  if (!track || !track.stream) return null;
  const flac = /\.flac$/i.test(track.ext || "");
  if (flac && !canPlayFlac()) {
    const u = new URL(track.stream, location.origin);
    u.searchParams.set("format", "opus");
    return u.href;
  }
  return track.stream;
}

function canPlayFlac() {
  return audio.canPlayType("audio/flac").length > 0;
}

function queueFor(tracks) {
  const seen = new Set();
  const queue = [];
  for (const t of tracks) {
    if (!t.stream) continue;
    const key = t.id + "###" + t.account;
    if (seen.has(key)) continue;
    seen.add(key);
    queue.push(t);
  }
  return queue;
}

function persistPlayer() {
  localStorage.setItem("ls-player", JSON.stringify({
    queue: state.player.queue.map((t) => ({ id: t.id, account: t.account })),
    index: state.player.index,
    shuffle: state.player.shuffle,
    repeat: state.player.repeat,
    ts: Date.now(),
  }));
}

function restorePlayer() {
  const raw = localStorage.getItem("ls-player");
  if (!raw) return;
  let snap;
  try {
    snap = JSON.parse(raw);
  } catch (e) {
    localStorage.removeItem("ls-player");
    return;
  }
  if (!snap || !Array.isArray(snap.queue) || !snap.queue.length) {
    localStorage.removeItem("ls-player");
    return;
  }
  if (!state.library || !Array.isArray(state.library.tracks)) return;
  const byKey = new Map(state.library.tracks.map((t) => [t.id + "###" + t.account, t]));
  const queue = [];
  for (const ref of snap.queue) {
    if (!ref || typeof ref.id !== "string") continue;
    const t = byKey.get(ref.id + "###" + ref.account);
    if (t && t.stream) queue.push(t);
  }
  if (!queue.length) {
    localStorage.removeItem("ls-player");
    return;
  }
  let index = Number.isInteger(snap.index) ? snap.index : 0;
  if (index < 0) index = 0;
  if (index >= queue.length) index = queue.length - 1;
  state.player.queue = queue;
  state.player.index = index;
  state.player.shuffle = !!snap.shuffle;
  if (snap.repeat === "off" || snap.repeat === "all" || snap.repeat === "one") {
    state.player.repeat = snap.repeat;
  }
  state.player._playReported = false;
  const track = queue[index];
  audio.src = streamUrl(track);
  audio.volume = state.player.muted ? 0 : state.player.volume;
  updatePlayerUI();
}

function setQueue(tracks, startIndex = 0) {
  state.player.queue = queueFor(tracks);
  state.player.index = -1;
  if (!state.player.queue.length) {
    stopPlayback("Nothing to play in this list");
    return;
  }
  const clicked = tracks[startIndex] || tracks[0];
  const idx = clicked
    ? state.player.queue.findIndex((t) => t.id === clicked.id && t.account === clicked.account)
    : 0;
  playIndex(idx >= 0 ? idx : 0);
}

function playIndex(index) {
  if (index < 0 || index >= state.player.queue.length) return;
  const prev = state.player.queue[state.player.index];
  const track = state.player.queue[index];
  const isSame = !!(prev && track && prev.id === track.id && prev.account === track.account);
  state.player.index = index;
  state._playReported = false;
  if (isSame) {
    audio.currentTime = 0;
    audio.play().then(() => { state.player.playing = true; }).catch(() => {});
    updatePlayerUI();
    persistPlayer();
    return;
  }
  audio.src = streamUrl(track);
  audio.volume = state.player.muted ? 0 : state.player.volume;
  audio.play()
    .then(() => { state.player.playing = true; })
    .catch(() => { state.player.playing = false; });
  updatePlayerUI();
  persistPlayer();
}

function stopPlayback(message) {
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  state.player.queue = [];
  state.player.index = -1;
  state.player.playing = false;
  localStorage.removeItem("ls-player");
  if (message) toast(message, "info");
  updatePlayerUI();
}

function togglePlay() {
  if (state.player.index < 0) {
    const tracks = visibleTracks();
    const queue = queueFor(tracks);
    if (!queue.length) { stopPlayback("Nothing to play"); return; }
    const first = queue[0];
    const idx = tracks.findIndex((t) => t.id === first.id && t.account === first.account);
    setQueue(tracks, idx >= 0 ? idx : 0);
    return;
  }
  if (audio.paused) {
    audio.play().then(() => { state.player.playing = true; }).catch(() => {});
  } else {
    audio.pause();
  }
  state.player.playing = !audio.paused;
  updatePlayerUI();
}

function nextIndex() {
  const q = state.player.queue;
  if (!q.length) return -1;
  if (state.player.index < 0) return 0;
  if (state.player.shuffle && q.length > 1) {
    let i;
    do { i = Math.floor(Math.random() * q.length); } while (i === state.player.index);
    return i;
  }
  return (state.player.index + 1) % q.length;
}

function prevIndex() {
  const q = state.player.queue;
  if (!q.length || state.player.index < 0) return -1;
  if (audio.currentTime > 3) { audio.currentTime = 0; return state.player.index; }
  return (state.player.index - 1 + q.length) % q.length;
}

function next() {
  const i = nextIndex();
  if (i >= 0) playIndex(i);
}

function prev() {
  const i = prevIndex();
  if (i >= 0) playIndex(i);
}

audio.addEventListener("ended", () => {
  if (state.sleepTimer.endOfSong) {
    state.sleepTimer.endOfSong = false;
    state.player.playing = false;
    updatePlayerUI();
    toast("Sleep timer ended — playback stopped", "info");
    notify("Sleep timer", "Playback stopped.");
    return;
  }
  if (state.player.repeat === "one") {
    state._playReported = false;
    audio.currentTime = 0;
    audio.play();
    return;
  }
  const q = state.player.queue;
  const atEnd = state.player.index >= q.length - 1;
  if (atEnd && state.player.repeat !== "all" && !state.player.shuffle) {
    state.player.playing = false;
    updatePlayerUI();
    return;
  }
  next();
});

audio.addEventListener("timeupdate", () => {
  if (!audio.duration || isNaN(audio.duration)) return;
  const frac = (audio.currentTime / audio.duration) * 1000;
  if ($("#seek").dataset.seeking !== "1") $("#seek").value = String(Math.round(frac));
  $("#cur-time").textContent = fmtTime(audio.currentTime);
  $("#dur-time").textContent = fmtTime(audio.duration);
  if (audio.currentTime / audio.duration >= 0.6) reportPlay();
});

async function reportPlay() {
  if (state._playReported) return;
  const track = currentTrack();
  if (!track || !track.id || !track.account) return;
  state._playReported = true;
  try {
    const res = await api("/api/plays", {
      method: "POST",
      body: { slug: track.account, video_id: track.id, progress: 60 },
    });
    if (res && typeof res.total === "number" && state.library) {
      const hit = state.library.tracks.find((t) => t.id === track.id && t.account === track.account);
      if (hit) hit.plays = res.total;
      if (["home", "stats"].includes(state.route)) navigate();
    }
  } catch (e) { /* non-fatal */ }
}

audio.addEventListener("play", () => { state.player.playing = true; updatePlayerUI(); });
audio.addEventListener("pause", () => { state.player.playing = false; updatePlayerUI(); });
audio.addEventListener("error", () => {
  if (state.player.queue.length) toast("Could not play this file", "error");
});

function updatePlayerUI() {
  const track = state.player.queue[state.player.index];
  const artBox = $("#player-art");
  const title = $("#player-title");
  const artist = $("#player-artist");
  const playBtn = $("#btn-play");

  if (track) {
    title.textContent = track.title;
    artist.textContent = `${track.artist} — ${track.album || "Single"}`;
    artBox.innerHTML = track.art
      ? `<img src="${esc(track.art)}" data-name="${esc(track.artist)}" onerror="this.outerHTML=artPlaceholderFor(this)"/>`
      : artPlaceholder(track.artist);
  } else {
    title.textContent = "Nothing playing";
    artist.textContent = "—";
    artBox.innerHTML = artPlaceholder("?");
  }
  playBtn.innerHTML = state.player.playing ? "&#10074;&#10074;" : "&#9654;";
  playBtn.title = state.player.playing ? "Pause (Space)" : "Play (Space)";
  $("#btn-shuffle").classList.toggle("active", state.player.shuffle);
  $("#btn-repeat").classList.toggle("active", state.player.repeat !== "off");
  $("#btn-mute").classList.toggle("active", state.player.muted);
  $("#volume").value = String(Math.round(state.player.volume * 100));
  markPlayingRows(track);
  if (!$("#queue-drawer").classList.contains("hidden")) renderQueueDrawer();
}

function markPlayingRows(track) {
  const inScope = !track || state.scope === "all" || track.account === state.scope;
  $$("table.tracks tbody tr").forEach((row) => {
    row.classList.toggle("playing", inScope && !!track && row.dataset.id === track.id && row.dataset.account === track.account);
  });
}

/* -------------------------------------------------------------------------
 * Sleep timer
 * ---------------------------------------------------------------------- */

function clearSleepTimer() {
  if (state.sleepTimer.interval) clearInterval(state.sleepTimer.interval);
  state.sleepTimer.interval = null;
  state.sleepTimer.endAt = null;
  state.sleepTimer.endOfSong = false;
  const btn = $("#btn-sleep");
  if (btn) { btn.classList.remove("active"); btn.title = "Sleep timer"; }
}

function setSleepTimer(minutes) {
  clearSleepTimer();
  if (!minutes) return;
  state.sleepTimer.endAt = Date.now() + minutes * 60000;
  state.sleepTimer.interval = setInterval(() => {
    const remaining = Math.max(0, state.sleepTimer.endAt - Date.now());
    const btn = $("#btn-sleep");
    if (btn) {
      btn.classList.add("active");
      btn.title = `Sleep in ${Math.ceil(remaining / 60000)}m — click to adjust`;
    }
    if (remaining <= 0) {
      clearSleepTimer();
      audio.pause();
      state.player.playing = false;
      updatePlayerUI();
      toast("Sleep timer ended — playback stopped", "info");
      notify("Sleep timer", "Playback stopped.");
    }
  }, 10000);
  toast(`Sleep timer set for ${minutes} minute${minutes === 1 ? "" : "s"}`, "info");
}

function sleepTimerDialog() {
  const minutes = Math.round((state.sleepTimer.endAt ? state.sleepTimer.endAt - Date.now() : 0) / 60000);
  modal(
    "Sleep timer",
    `<p style="color:var(--text-dim)">Pause playback automatically after the chosen time. Music stops at the current track boundary (best effort).</p>
     <div class="filter-bar" style="margin-top:14px">${[15, 30, 45, 60, 90].map((m) => `
       <button class="btn ${minutes === m ? "btn-primary" : ""}" data-min="${m}" style="flex:1">${m}m</button>`).join("")}
     </div>`,
    [
      { label: "Off", value: () => false },
      { label: "1 hour", value: () => ({ minutes: 60 }) },
      { label: "End of song", value: () => ({ minutes: null, endOfSong: true }) },
    ],
  ).then((value) => {
    if (!value) { clearSleepTimer(); return; }
    if (value.endOfSong) {
      state.sleepTimer.endOfSong = true;
      toast("End of song — playback stops when the current track finishes", "info");
      return;
    }
    setSleepTimer(value.minutes);
  });

  $$("#modal-root [data-min]").forEach((btn) => {
    btn.onclick = () => {
      setSleepTimer(Number(btn.dataset.min));
      const root = $("#modal-root");
      root.classList.add("hidden");
      root.innerHTML = "";
    };
  });
}

/* -------------------------------------------------------------------------
 * Queue drawer
 * ---------------------------------------------------------------------- */

function toggleQueueDrawer() {
  const drawer = $("#queue-drawer");
  drawer.classList.toggle("hidden");
  if (!drawer.classList.contains("hidden")) renderQueueDrawer();
}

function renderQueueDrawer() {
  const list = $("#queue-list");
  if (!list) return;
  const q = state.player.queue;
  const cur = state.player.index;
  const items = q.map((t, i) => `
    <div class="queue-item ${i === cur ? "current" : ""}" data-i="${i}">
      <span class="queue-pos">${i + 1}</span>
      <div class="mini-art" data-name="${esc(t.artist)}">
        ${t.art ? `<img src="${esc(t.art)}" loading="lazy" onerror="this.outerHTML=artPlaceholderFor(this)"/>` : artPlaceholder(t.artist)}
      </div>
      <div class="queue-meta">
        <div class="t-title">${esc(t.title)}</div>
        <div class="t-sub">${esc(t.artist)}</div>
      </div>
    </div>`).join("") || `<div class="empty-state" style="padding:24px">Queue is empty.</div>`;
  list.innerHTML = items;
  $$(".queue-item", list).forEach((row) => {
    row.onclick = () => { playIndex(Number(row.dataset.i)); renderQueueDrawer(); };
  });
  $("#queue-list").scrollTop = 0;
}

/* -------------------------------------------------------------------------
 * Downloads + refresh
 * ---------------------------------------------------------------------- */

async function startDownload(config) {
  const body = {
    account: config.account,
    quality: config.quality,
    existing_policy: config.existing_policy,
    session: config.session,
  };
  if (config.output) body.output = config.output;
  if (config.count) body.count = config.count;
  if (config.video_ids) body.video_ids = config.video_ids;
  if (config.playlist_id) body.playlist_id = config.playlist_id;
  if (config.tracks) body.tracks = config.tracks;
  const snapshot = await api("/api/downloads", { method: "POST", body });
  state.downloads[config.account] = snapshot;
  renderDownloadsBadge();
  return snapshot;
}

function needsDownload(t) {
  return t.status !== "downloaded" && t.status !== "premium";
}

function groupByAccount(tracks) {
  const groups = new Map();
  for (const t of tracks) {
    if (!groups.has(t.account)) groups.set(t.account, []);
    groups.get(t.account).push(t);
  }
  return groups;
}

function accountHasActiveDownload(slug) {
  const s = state.downloads[slug];
  return !!(s && !s.finished);
}

async function startAutoDownload(tracks, { title = "Start download" } = {}) {
  tracks = tracks.filter(needsDownload);
  if (!tracks.length) return false;
  const config = await downloadDialog({ account: "", title });
  if (!config) return false;
  let started = 0;
  let skipped = 0;
  const accountNames = state.library ? state.library.accounts : [];
  const nameFor = (slug) => (accountNames.find((a) => a.slug === slug) || {}).name || slug;
  for (const [slug, group] of groupByAccount(tracks)) {
    if (accountHasActiveDownload(slug)) {
      skipped += group.length;
      toast(`Already downloading for ${nameFor(slug)} — skipped`, "info");
      continue;
    }
    try {
      await startDownload({
        account: slug,
        quality: config.quality,
        existing_policy: config.existing_policy,
        session: config.session,
        video_ids: group.map((t) => t.id),
      });
      started += group.length;
    } catch (e) {
      toast(`Failed to start ${nameFor(slug)}: ${e.message}`, "error");
    }
  }
  if (started) toast(`Downloading ${started} song${started === 1 ? "" : "s"}…`, "info");
  return started > 0;
}

async function downloadDialog({ account, title = "Start download", bodyHTML = "", defaults = null } = {}) {
  if (!defaults) {
    try { defaults = await api("/api/settings"); } catch (e) { defaults = {}; }
  }
  const dq = QUALITIES.some(([v]) => v === defaults.default_quality) ? defaults.default_quality : "best";
  const dp = defaults.default_existing_policy === "replace" ? "replace" : "skip";
  const ds = ["once", "loop", "skip"].includes(defaults.default_session) ? defaults.default_session : "loop";
  const value = await modal(title, `
    <div class="label">Audio quality</div>
    <select name="quality" class="input">
      ${QUALITIES.map(([v, l]) => `<option value="${v}" ${v === dq ? "selected" : ""}>${esc(l)}</option>`).join("")}
    </select>
    <div class="label">If audio already exists</div>
    <select name="policy" class="input">
      <option value="skip" ${dp === "skip" ? "selected" : ""}>Skip existing file</option>
      <option value="replace" ${dp === "replace" ? "selected" : ""}>Replace existing file</option>
    </select>
    <div class="label">Session</div>
    <select name="session" class="input">
      <option value="once" ${ds === "once" ? "selected" : ""}>Run the whole list once</option>
      <option value="loop" ${ds === "loop" ? "selected" : ""}>Loop until stopped</option>
      <option value="skip" ${ds === "skip" ? "selected" : ""}>Download missing across all qualities</option>
    </select>
    ${bodyHTML}
    <p style="margin-top:14px;color:var(--text-faint);font-size:12px">
      These settings are locked for the whole session once it starts.
    </p>`,
    [
      { label: "Cancel", value: () => false },
      {
        label: "Start Download",
        variant: "btn-primary",
        value: () => ({
          account,
          quality: $("#modal-root select[name=quality]").value,
          existing_policy: $("#modal-root select[name=policy]").value,
          session: $("#modal-root select[name=session]").value,
        }),
      },
    ],
  );
  return value;
}

async function refreshLibrary(slug) {
  if (slug !== "all" && state.refreshing[slug]) return;
  if (slug === "all") {
    const accounts = (state.library ? state.library.accounts : (await api("/api/library")).accounts) || [];
    for (const acc of accounts) {
      try { await api(`/api/accounts/${acc.slug}/refresh`, { method: "POST", body: {} }); state.refreshing[acc.slug] = true; } catch (e) { toast(e.message, "error"); }
    }
  } else {
    await api(`/api/accounts/${slug}/refresh`, { method: "POST", body: {} });
    state.refreshing[slug] = true;
  }
  renderAccountsNav();
  renderRefreshUI();
  toast("Library refresh started", "info");
}

/* -------------------------------------------------------------------------
 * Views
 * ---------------------------------------------------------------------- */

const view = $("#view");
const topbarTitle = $("#topbar-title");

function spinner() { return `<div class="spinner"></div>`; }
function empty(text, icon = "&#9835;") {
  return `<div class="empty-state"><div class="big">${icon}</div>${esc(text)}</div>`;
}

function trackRowsHTML(tracks) {
  const showAccount = state.scope === "all";
  return tracks.map((t, i) => `
    <tr data-id="${esc(t.id)}" data-account="${esc(t.account)}" data-index="${i}">
      <td style="color:var(--text-faint);width:36px">${i + 1}</td>
      <td>
        <div class="track-cell">
          <div class="mini-art" data-name="${esc(t.artist)}">
            ${t.art ? `<img src="${esc(t.art)}" loading="lazy" onerror="this.outerHTML=artPlaceholderFor(this)"/>` : artPlaceholder(t.artist)}
          </div>
          <div class="t-main">
            <div class="t-title">${esc(t.title)}</div>
            <div class="t-sub">${esc(t.artist)}</div>
          </div>
        </div>
      </td>
      <td style="color:var(--text-dim)">${esc(t.album || "—")}</td>
      ${showAccount ? `<td><span class="q-badge">${esc(t.account_name)}</span></td>` : ""}
      <td style="color:var(--text-dim)">${esc(t.year || "—")}</td>
      <td style="color:var(--text-dim)">${fmtDuration(t.duration)}</td>
      <td style="color:var(--text-dim)">${t.plays ? t.plays : "—"}</td>
      <td>
        <span class="q-badge"><span class="state-dot ${esc(t.status)}"></span>${esc(t.status === "premium" ? "Premium" : (t.quality || "Not downloaded"))}</span>
      </td>
      <td>${needsDownload(t) ? `<button class="btn btn-ghost row-dl" data-id="${esc(t.id)}" data-account="${esc(t.account)}" title="Download to ${esc(t.account_name)}">&#8681;</button>` : ""}</td>
      <td><button class="btn btn-ghost row-pl" data-id="${esc(t.id)}" data-account="${esc(t.account)}" data-title="${esc(t.title)}" data-artist="${esc(t.artist)}" title="Add to playlist">&#43;</button></td>
    </tr>`).join("");
}

function trackTable(tracks, opts = {}) {
  if (!tracks.length) return empty(opts.emptyText || "No songs in this view yet.");
  const showAccount = state.scope === "all";
  const online = tracks.filter(needsDownload);
  const head = `
    <th data-col="index">#</th>
    <th data-col="title">Title</th>
    <th data-col="album">Album</th>
    ${showAccount ? '<th data-col="account">Account</th>' : ""}
    <th data-col="year">Year</th>
    <th data-col="duration">Duration</th>
    <th data-col="plays">Plays</th>
    <th data-col="quality">Quality</th>
    <th></th>
    <th></th>`;

  return `
    <div class="filter-bar">
      <input id="track-filter" class="input" type="text" placeholder="Filter by title, artist, album…" />
      ${online.length ? `<button id="download-view" class="btn btn-primary">&#8681; Download ${online.length}</button>` : ""}
    </div>
    <div style="overflow-x:auto">
      <table class="tracks">
        <thead><tr>${head}</tr></thead>
        <tbody>${trackRowsHTML(tracks)}</tbody>
      </table>
    </div>`;
}

function currentTrack() {
  return state.player.queue[state.player.index];
}

function bindTrackTable(container, allTracks) {
  const table = $("table.tracks", container);
  if (table) {
    $$("tbody tr", table).forEach((row) => {
      row.onclick = (e) => {
        if (e.target.closest("a")) return;
        if (e.target.closest("button")) return;
        const index = Number(row.dataset.index);
        setQueue(allTracks, index);
      };
    });
    let sortCol = "title";
    let sortDir = 1;
    $$("th[data-col]", table).forEach((th) => {
      th.onclick = () => {
        const col = th.dataset.col;
        if (col === "index") return;
        if (col === sortCol) sortDir *= -1; else { sortCol = col; sortDir = 1; }
        const sorted = [...allTracks].sort((a, b) => {
          const av = a[col]; const bv = b[col];
          if (av == null && bv == null) return 0;
          if (av == null) return 1;
          if (bv == null) return -1;
          if (typeof av === "number" && typeof bv === "number") return (av - bv) * sortDir;
          return String(av).localeCompare(String(bv)) * sortDir;
        });
        const tb = $("tbody", table);
        tb.innerHTML = trackRowsHTML(sorted);
        bindTrackTable(container, sorted);
      };
    });
  }

  const filter = $("#track-filter", container);
  if (filter) {
    filter.oninput = () => {
      const q = filter.value.toLowerCase();
      const filtered = allTracks.filter((t) =>
        (t.title + " " + t.artist + " " + (t.album || "") + " " + t.account_name).toLowerCase().includes(q));
      const tb = $("tbody", table);
      tb.innerHTML = trackRowsHTML(filtered);
      bindTrackTable(container, filtered);
    };
  }

  markPlayingRows(currentTrack());

  const downloadBtn = $("#download-view", container);
  if (downloadBtn) {
    downloadBtn.onclick = async () => {
      await startAutoDownload(allTracks, { title: "Download missing tracks" });
    };
  }

  if (table) $$(".row-dl", table).forEach((btn) => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const t = allTracks.find((x) => x.id === btn.dataset.id && x.account === btn.dataset.account);
      if (!t) return;
      if (accountHasActiveDownload(t.account)) {
        toast(`Already downloading for ${t.account_name}`, "info");
        return;
      }
      const config = await downloadDialog({ account: t.account, title: `Download “${t.title}”` });
      if (!config) return;
      try {
        await startDownload({
          account: t.account,
          quality: config.quality,
          existing_policy: config.existing_policy,
          session: config.session,
          video_ids: [t.id],
        });
        toast(`Downloading “${t.title}”…`, "info");
      } catch (err) { toast(err.message, "error"); }
    };
  });

  if (table) $$(".row-pl", table).forEach((btn) => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const t = allTracks.find((x) => x.id === btn.dataset.id && x.account === btn.dataset.account);
      if (!t) return;
      addToPlaylistModal(t);
    };
  });
}

async function addToPlaylistModal(track) {
  let playlistsData;
  try { playlistsData = await api("/api/playlists"); }
  catch (e) { toast(e.message, "error"); return; }
  const custom = playlistsData.custom || [];
  const tname = `${track.title} — ${track.artist}`;
  const created = await modal(`Add “${tname}” to playlist`, `
    <div class="label">New playlist name (optional)</div>
    <input id="pl-new" class="input" type="text" placeholder="Leave empty to pick an existing playlist" />
    ${custom.length ? `<div class="label" style="margin-top:12px">Existing playlists</div>
      <select id="pl-pick" class="input">
        <option value="">— choose —</option>
        ${custom.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")}
      </select>` : ""}`,
    [
      { label: "Cancel", value: () => false },
      {
        label: "Add",
        variant: "btn-primary",
        value: () => {
          const name = $("#pl-new").value.trim();
          const id = $("#pl-pick") ? $("#pl-pick").value : "";
          return { name, id };
        },
      },
    ]);
  if (!created) return;
  let playlistId = created.id;
  try {
    if (created.name) {
      const pl = await api("/api/playlists", { method: "POST", body: { name: created.name, kind: "custom", account: track.account } });
      playlistId = pl.id;
    }
    if (!playlistId) { toast("No playlist chosen", "info"); return; }
    const r = await api(`/api/playlists/${playlistId}/add`, {
      method: "POST",
      body: { tracks: [{ video_id: track.id, account: track.account }] },
    });
    toast(`Added to playlist (${r.added ?? 0} added)`, r.added ? "success" : "info");
  } catch (e) { toast(e.message, "error"); }
}

async function renderHome() {
  const tracks = visibleTracks();
  const agg = aggregates();
  const accounts = state.library ? state.library.accounts : [];

  const liked = tracks.filter((t) => t.liked).length;
  const downloaded = tracks.filter((t) => t.status === "downloaded").length;
  const missing = tracks.filter((t) => t.status === "missing").length;
  const totalPlays = tracks.reduce((s, t) => s + (t.plays || 0), 0);
  const totalSize = accounts.reduce((s, a) => s + (a.size_bytes || 0), 0);

  const recent = [...tracks].filter((t) => t.added).sort((a, b) => b.added - a.added).slice(0, 12);
  const topPlayed = [...tracks].filter((t) => t.plays).sort((a, b) => b.plays - a.plays).slice(0, 8);
  const online = tracks.filter(needsDownload);

  view.innerHTML = `
    <div class="hero">
      <div class="p-type">${esc(selectedAccountName())}</div>
      <h1>Welcome to your library</h1>
      <p>Your downloaded music, organized like a streaming service — search, browse by artist, album, year or genre, and play anything instantly. Everything stays on this machine.</p>
      <div class="hero-actions">
        <button id="hero-refresh" class="btn btn-ghost">&#8635; Refresh library</button>
        ${online.length ? `<button id="hero-download" class="btn btn-primary">&#8681; Download ${online.length}</button>` : ""}
        <a href="#/most-played" class="btn">&#128172; Most played</a>
      </div>
    </div>

    <div class="stat-grid">
      <div class="stat-card"><div class="stat-value">${accounts.length}</div><div class="stat-label">Accounts</div></div>
      <div class="stat-card"><div class="stat-value">${liked}</div><div class="stat-label">Liked tracks</div></div>
      <div class="stat-card"><div class="stat-value">${downloaded}</div><div class="stat-label">Downloaded</div></div>
      <div class="stat-card"><div class="stat-value">${missing}</div><div class="stat-label">Missing files</div></div>
      <div class="stat-card"><div class="stat-value">${totalPlays.toLocaleString()}</div><div class="stat-label">Total plays</div></div>
      <div class="stat-card"><div class="stat-value">${fmtSize(totalSize)}</div><div class="stat-label">Library size</div></div>
    </div>

    ${recent.length ? `
      <div class="section-head"><h2>Recently added</h2></div>
      <div class="card-grid" id="recent-grid"></div>` : ""}
    ${topPlayed.length ? `
      <div class="section-head"><h2>Most played</h2></div>
      <div class="card-grid" id="top-grid"></div>` : ""}
  `;

  const heroRefresh = $("#hero-refresh");
  if (heroRefresh) heroRefresh.onclick = () => { refreshLibrary(state.scope); };
  const heroDownload = $("#hero-download");
  if (heroDownload) heroDownload.onclick = async () => {
    await startAutoDownload(online, { title: "Download missing tracks" });
  };

  const grid = (el, list) => {
    list.forEach((t) => {
      const node = document.createElement("div");
      node.className = "card";
      node.innerHTML = `
        <div class="card-art" data-name="${esc(t.artist)}">
          ${t.art ? `<img src="${esc(t.art)}" loading="lazy" onerror="this.outerHTML=artPlaceholderFor(this)"/>` : artPlaceholder(t.artist)}
        </div>
        <div class="card-title">${esc(t.title)}</div>
        <div class="card-sub">${esc(t.artist)}</div>`;
      node.onclick = () => {
        const all = visibleTracks().filter((x) => x.stream);
        const idx = all.findIndex((x) => x.id === t.id && x.account === t.account);
        setQueue(all, idx >= 0 ? idx : 0);
      };
      $(el).appendChild(node);
    });
  };
  grid("#recent-grid", recent);
  grid("#top-grid", topPlayed);
}

async function renderSongs() {
  const tracks = visibleTracks();
  view.innerHTML = `
    <div class="section-head"><h2>All songs <span style="color:var(--text-faint);font-weight:600;font-size:14px">${tracks.length}</span></h2></div>
    ${tracks.length ? trackTable(tracks) : empty("No songs yet. Add an account and refresh your library.")}`;
  bindTrackTable(view, tracks);
}

function cardGridHTML(cards, hrefFn) {
  return `<div class="card-grid">${cards.map((c, i) => `
    <a href="${hrefFn(c)}" style="text-decoration:none;color:inherit">
      <div class="card" style="animation-delay:${i * 0.02}s">
        <div class="card-art" data-name="${esc(c.title || c.name || c.year)}">
          ${c.art ? `<img src="${esc(c.art)}" loading="lazy" onerror="this.outerHTML=artPlaceholderFor(this)"/>` : artPlaceholder(c.title || c.name || String(c.year), true)}
        </div>
        <div class="card-title">${esc(c.title || c.name || c.year)}</div>
        <div class="card-sub">${esc(c.sub || `${c.count} song${c.count === 1 ? "" : "s"}`)}</div>
      </div>
    </a>`).join("")}</div>`;
}

async function renderArtists() {
  const { artists } = aggregates();
  view.innerHTML = `
    <div class="section-head"><h2>Artists <span style="color:var(--text-faint);font-weight:600;font-size:14px">${artists.length}</span></h2></div>
    ${artists.length ? cardGridHTML(artists.map((a) => ({
      name: a.name, art: a.art,
      sub: `${a.count} song${a.count === 1 ? "" : "s"}`,
    })), (a) => `#/artist/${encodeURIComponent(a.name)}`) : empty("No artists yet.")}`;
}

async function renderAlbums() {
  const { albums } = aggregates();
  view.innerHTML = `
    <div class="section-head"><h2>Albums <span style="color:var(--text-faint);font-weight:600;font-size:14px">${albums.length}</span></h2></div>
    ${albums.length ? cardGridHTML(albums.map((a) => ({
      title: a.title, artist: a.artist, art: a.art,
      sub: `${a.artist || "—"} · ${a.year || "—"} · ${a.count} song${a.count === 1 ? "" : "s"}`,
    })), (c) => `#/album/${encodeURIComponent(c.title + "###" + (c.artist || ""))}`) : empty("No albums yet.")}`;
}

async function renderYears() {
  const { years } = aggregates();
  view.innerHTML = `
    <div class="section-head"><h2>Years &amp; decades</h2></div>
    ${years.length ? cardGridHTML(years.map((y) => ({
      name: String(y.year),
      art: null,
      sub: `${y.count} song${y.count === 1 ? "" : "s"}`,
    })), (c) => `#/year/${c.name}`) : empty("No year data yet — downloaded files carry year tags.")}`;
}

async function renderGenres() {
  const { genres } = aggregates();
  view.innerHTML = `
    <div class="section-head"><h2>Genres</h2></div>
    ${genres.length ? cardGridHTML(genres.map((g) => ({
      name: g.genre,
      art: null,
      sub: `${g.count} song${g.count === 1 ? "" : "s"}`,
    })), (c) => `#/genre/${encodeURIComponent(c.name)}`) : empty("No genre data yet — downloaded files carry genre tags.")}`;
}

async function renderArtist(name) {
  const tracks = tracksForArtist(name);
  const plays = tracks.reduce((s, t) => s + (t.plays || 0), 0);
  const art = tracks.find((t) => t.art);
  const albums = [...new Set(tracks.map((t) => t.album).filter(Boolean))];
  const { artists } = aggregates();
  const exact = artists.find((a) => a.name === name);

  const online = tracks.filter(needsDownload);
  view.innerHTML = `
    <div class="page-hero">
      <div class="big-art" data-name="${esc(name)}">
        ${art ? `<img src="${esc(art.art)}" data-big data-name="${esc(name)}" onerror="this.outerHTML=artPlaceholderFor(this)"/>` : artPlaceholder(name, true)}
      </div>
      <div>
        <div class="p-type">Artist</div>
        <h1>${esc(name)}</h1>
        <div class="p-sub">${tracks.length} song${tracks.length === 1 ? "" : "s"} · ${albums.length} album${albums.length === 1 ? "" : "s"} · ${plays} play${plays === 1 ? "" : "s"}</div>
        <div class="hero-actions">
          <button id="artist-play" class="btn btn-primary">&#9654; Play</button>
          ${online.length ? `<button id="artist-download" class="btn">&#8681; Download ${online.length}</button>` : ""}
        </div>
      </div>
    </div>
    <div class="section-head"><h2>Songs</h2></div>
    ${trackTable(tracks, { emptyText: "No downloaded songs for this artist yet." })}`;

  $("#artist-play").onclick = () => setQueue(tracks.filter((t) => t.stream), 0);
  $("#artist-download").onclick = async () => {
    await startAutoDownload(online, { title: `Download missing ${name} tracks` });
  };
  bindTrackTable(view, tracks);
}

async function renderAlbum(key) {
  const [albumTitle, artist] = key.split("###");
  const tracks = tracksForAlbum(key);
  const art = tracks.find((t) => t.art);
  const online = tracks.filter(needsDownload);

  view.innerHTML = `
    <div class="page-hero">
      <div class="big-art" data-name="${esc(albumTitle)}">
        ${art ? `<img src="${esc(art.art)}" data-big data-name="${esc(albumTitle)}" onerror="this.outerHTML=artPlaceholderFor(this)"/>` : artPlaceholder(albumTitle, true)}
      </div>
      <div>
        <div class="p-type">Album</div>
        <h1>${esc(albumTitle)}</h1>
        <div class="p-sub">
          ${esc(artist)} · ${esc(tracks[0]?.year || "—")} · ${tracks.length} song${tracks.length === 1 ? "" : "s"}
        </div>
        <div class="hero-actions">
          <button id="album-play" class="btn btn-primary">&#9654; Play</button>
          ${online.length ? `<button id="album-download" class="btn">&#8681; Download ${online.length}</button>` : ""}
        </div>
      </div>
    </div>
    ${trackTable(tracks, { emptyText: "No downloaded songs in this album yet." })}`;

  $("#album-play").onclick = () => setQueue(tracks.filter((t) => t.stream), 0);
  $("#album-download").onclick = async () => {
    await startAutoDownload(online, { title: `Download missing ${albumTitle} tracks` });
  };
  bindTrackTable(view, tracks);
}

async function renderYear(year) {
  const tracks = tracksForYear(year);
  view.innerHTML = `
    <div class="page-hero">
      <div class="big-art"><div class="art-placeholder" style="font-size:64px">${year}</div></div>
      <div>
        <div class="p-type">Released</div>
        <h1>${year}</h1>
        <div class="p-sub">${tracks.length} song${tracks.length === 1 ? "" : "s"}</div>
        <div class="hero-actions"><button id="year-play" class="btn btn-primary">&#9654; Play all</button></div>
      </div>
    </div>
    ${trackTable(tracks, { emptyText: "No songs released in this year." })}`;
  $("#year-play").onclick = () => setQueue(tracks.filter((t) => t.stream), 0);
  bindTrackTable(view, tracks);
}

async function renderGenre(genre) {
  const tracks = tracksForGenre(genre);
  view.innerHTML = `
    <div class="page-hero">
      <div class="big-art"><div class="art-placeholder" style="font-size:52px">${esc(genre)}</div></div>
      <div>
        <div class="p-type">Genre</div>
        <h1>${esc(genre)}</h1>
        <div class="p-sub">${tracks.length} song${tracks.length === 1 ? "" : "s"}</div>
        <div class="hero-actions"><button id="genre-play" class="btn btn-primary">&#9654; Play all</button></div>
      </div>
    </div>
    ${trackTable(tracks, { emptyText: "No songs in this genre." })}`;
  $("#genre-play").onclick = () => setQueue(tracks.filter((t) => t.stream), 0);
  bindTrackTable(view, tracks);
}

async function renderMostPlayed() {
  if (state.scope === "all") {
    view.innerHTML = `<div class="alert alert-info">Select an account in the sidebar to see its most-played chart.</div>`;
    return;
  }
  const seq = state._renderSeq;
  const data = await api(`/api/most-played/${state.scope}`);
  if (seq !== state._renderSeq) return;
  let defaultQuality = "mp3_320";
  try { defaultQuality = (await api("/api/settings")).default_quality || defaultQuality; } catch (e) { /* keep fallback */ }
  const tracks = data.report.tracks || [];
  const rows = tracks.map((t, i) => `
    <tr data-id="${esc(t.video_id)}">
      <td style="color:var(--text-faint)">#${i + 1}</td>
      <td>
        <div class="track-cell">
          <div class="t-main"><div class="t-title">${esc(t.title)}</div><div class="t-sub">${esc(t.artist)}</div></div>
        </div>
      </td>
      <td style="color:var(--text-dim)">${t.plays} play${t.plays === 1 ? "" : "s"}</td>
    </tr>`).join("");

  view.innerHTML = `
    <div class="section-head"><h2>Most played — ${esc(selectedAccountName())}</h2></div>
    <div class="panel">
      <div class="meta-row"><span class="k">Top tracks in your watch history</span><span class="v">${tracks.length}</span></div>
      <div class="filter-bar" style="margin-top:16px">
        <div class="label" style="margin:0">Download top</div>
        <input id="mp-count" class="input" type="number" min="1" max="100" value="25" style="width:90px" />
        <select id="mp-quality" class="input" style="width:auto">
          ${QUALITIES.map(([v, l]) => `<option value="${v}" ${v === defaultQuality ? "selected" : ""}>${esc(l)}</option>`).join("")}
        </select>
        <button id="mp-download" class="btn btn-primary">&#8681; Download</button>
        <button id="mp-refresh" class="btn btn-ghost">&#8635; Re-fetch history</button>
      </div>
    </div>
    ${rows ? `<table class="tracks"><thead><tr><th>#</th><th>Title</th><th>Plays</th></tr></thead><tbody>${rows}</tbody></table>` : empty("No history found.")}`;

  $("#mp-download").onclick = async () => {
    const count = Math.max(1, Math.min(100, Number($("#mp-count").value) || 25));
    const config = {
      account: state.scope,
      quality: $("#mp-quality").value,
      existing_policy: "skip",
      session: "once",
      output: "most_played",
      count,
    };
    try { await startDownload(config); toast(`Downloading top ${count}…`, "info"); } catch (e) { toast(e.message, "error"); }
  };
  $("#mp-refresh").onclick = async () => {
    try {
      toast("Fetching play history…", "info");
      await api(`/api/most-played/${state.scope}?fresh=1`);
      renderMostPlayed();
    } catch (e) { toast(e.message, "error"); }
  };

  const libTracks = state.library ? state.library.tracks.filter((t) => t.account === state.scope) : [];
  $$("#view table.tracks tbody tr", view).forEach((row) => {
    row.onclick = () => {
      const track = libTracks.find((t) => t.id === row.dataset.id);
      if (!track) return;
      if (!track.stream) { toast(`"${track.title}" isn't downloaded yet`, "info"); return; }
      const tracks = libTracks.filter((t) => t.stream);
      const idx = tracks.findIndex((t) => t.id === track.id);
      if (idx >= 0) setQueue(tracks, idx);
    };
  });
}

async function renderDuplicates() {
  view.innerHTML = spinner();
  try {
    const seq = state._renderSeq;
    const data = await api("/api/duplicates");
    if (seq !== state._renderSeq) return;
    const report = data.report || {};
    const candidates = data.candidates || [];
    const exact = report.exact || [];
    const byTitle = report.by_title || [];

    let html = `<div class="section-head"><h2>Duplicates</h2></div>`;

    if (!exact.length && !byTitle.length) {
      html += `<div class="empty-state"><div class="big">&#10004;</div>No duplicate songs found across your accounts.</div>`;
      view.innerHTML = html;
      return;
    }

    html += `
      <div class="alert alert-info">
        ${report.duplicate_count ?? exact.length} duplicate song${(report.duplicate_count ?? 1) === 1 ? "" : "s"} across ${report.account_count ?? 1} account${report.account_count === 1 ? "" : "s"}.
        ${candidates.length} removable item${candidates.length === 1 ? "" : "s"}.
      </div>
      <div class="filter-bar">
        <button id="dup-remove" class="btn btn-danger" disabled>&#128465; Remove selected</button>
        <button id="dup-export" class="btn btn-ghost">&#8681; Export report</button>
        <button id="dup-rescan" class="btn btn-ghost">&#8635; Rescan</button>
      </div>`;

    const section = (title, items, itemHTML) => {
      if (!items.length) return "";
      return `<div class="dup-section">
        <div class="dup-title">${title} (${items.length})</div>
        ${items.map(itemHTML).join("")}
      </div>`;
    };

    html += section("Same upload liked in multiple accounts", exact, (entry) => `
      ${entry.accounts.map((a) => `
        <div class="check-row">
          <label>
            <input type="checkbox" class="dup-cb" data-slug="${esc(a.slug)}" data-vid="${esc(entry.video_id)}" data-label="${esc(`${a.name}: ${entry.artist} — ${entry.title}`)}" />
            <span>${esc(a.name)}: <strong>${esc(entry.artist)} — ${esc(entry.title)}</strong>
              <span style="color:var(--text-faint)">[same upload, ${a.downloaded ? "downloaded" : "no local files"}]</span></span>
          </label>
        </div>`).join("")}`);

    html += section("Same song, different uploads", byTitle, (entry) => `
      ${entry.accounts.map((a) => `
        <div class="check-row">
          <label>
            <input type="checkbox" class="dup-cb" data-slug="${esc(a.slug)}" data-vid="${esc(a.video_ids?.[0] || entry.title)}" data-label="${esc(`${a.name}: ${entry.title} — ${entry.artist}`)}" />
            <span>${esc(a.name)}: <strong>${esc(entry.title)} — ${esc(entry.artist)}</strong>
              <span style="color:var(--text-faint)">[${a.upload_count} upload${a.upload_count === 1 ? "" : "s"}]</span></span>
          </label>
        </div>`).join("")}`);

    html += `<div class="empty-state" id="dup-none" style="display:none"><div class="big">&#10004;</div>No duplicates selected.</div>`;

    html += `<div class="section-head" style="margin-top:26px"><h3>Duplicates within a single account</h3></div>
      <div class="alert alert-info">Re-releases, covers and remasters that share a normalized title + artist inside the same account. Pick which copy to keep — the rest are unliked and their files deleted.</div>
      <div id="within-root">Loading…</div>`;

    view.innerHTML = html;

    const update = () => {
      const checked = $$(".dup-cb:checked");
      $("#dup-remove").disabled = checked.length === 0;
    };
    $$(".dup-cb").forEach((cb) => { cb.onchange = update; });

    populateWithinDuplicates(seq);

    $("#dup-remove").onclick = async () => {
      const checked = $$(".dup-cb:checked");
      const candidatesList = checked.map((cb) => ({
        slug: cb.dataset.slug,
        video_id: cb.dataset.vid,
        label: cb.dataset.label,
      }));
      const proceed = await modal(
        `Remove ${candidatesList.length} item${candidatesList.length === 1 ? "" : "s"}?`,
        `<p>Each selection will be <strong>unliked</strong> on its account and its downloaded files deleted from disk. This mirrors the TUI's verified removal flow.</p>
         <p style="margin-top:8px">${candidatesList.map((c) => `<div style="font-size:12.5px;color:var(--text-dim)">• ${esc(c.label)}</div>`).join("")}</p>`,
        [
          { label: "Cancel", value: () => false },
          { label: "Remove", variant: "btn-danger", value: () => true },
        ],
      );
      if (!proceed) return;
      try {
        const result = await api("/api/duplicates/remove", { method: "POST", body: { candidates: candidatesList } });
        let removed = 0;
        for (const r of result.results || []) removed += (r.unliked || []).length;
        toast(`Removed ${removed} item${removed === 1 ? "" : "s"}`, "success");
        loadLibrary();
        renderDuplicates();
      } catch (e) { toast(e.message, "error"); }
    };

    $("#dup-rescan").onclick = () => { renderDuplicates(); };

    $("#dup-export").onclick = async () => {
      try {
        const r = await api("/api/duplicates/export", { method: "POST", body: {} });
        toast(`Duplicate report saved to ${r.file}`, "success");
      } catch (e) { toast(e.message, "error"); }
    };
  } catch (e) {
    view.innerHTML = `<div class="alert alert-error">${esc(e.message)}</div>`;
  }
}

async function populateWithinDuplicates(seq) {
  const root = $("#within-root");
  if (!root) return;
  let data;
  try { data = await api("/api/duplicates/within"); } catch (e) { root.innerHTML = `<div class="alert alert-error">${esc(e.message)}</div>`; return; }
  if (seq !== state._renderSeq) return;

  let html = "";
  for (const acc of data) {
    if (acc.error) { html += `<div class="alert alert-error">${esc(acc.name || acc.slug)}: ${esc(acc.error)}</div>`; continue; }
    const groups = acc.groups || [];
    html += `<div class="dup-section">
      <div class="dup-title">${esc(acc.name)} (${groups.length} group${groups.length === 1 ? "" : "s"})</div>
      ${groups.length ? groups.map((g) => `
        <div class="dup-title" style="padding:10px 0 4px;color:var(--text-dim)">${esc(g.title)} — ${esc(g.artist)}</div>
        ${g.tracks.map((t) => `
          <div class="check-row">
            <label>
              <input type="checkbox" class="within-cb" data-slug="${esc(acc.slug)}" data-vid="${esc(t.video_id)}" data-label="${esc(`${t.title} — ${t.artist}${t.album ? " [" + t.album + "]" : ""} [${(t.qualities || []).join(", ")}]`)}" />
              <span>${esc(t.title)} <span style="color:var(--text-faint)">${t.album ? esc("[" + t.album + "]") : ""} · ${(t.qualities || []).join(", ")}</span></span>
            </label>
          </div>`).join("")}
        ${g.tracks.length > 1 ? `<button class="btn btn-ghost" style="margin:6px 0 10px;padding:5px 12px;font-size:12px" data-within-keep="${esc(acc.slug)}" data-keep-vid="${esc(g.tracks[0].video_id)}" data-keep-label="${esc(g.tracks[0].title)}">Keep first copy</button>` : ""}
      `).join("") : `<div class="empty-state" style="padding:12px">No within-account duplicates.</div>`}
    </div>`;
  }

  root.innerHTML = html;

  $$("[data-within-keep]", root).forEach((btn) => {
    btn.onclick = async () => {
      const group = data.find((a) => a.slug === btn.dataset.withinKeep);
      const keep = btn.dataset.keepVid;
      const entry = group?.groups?.flatMap((g) => g.tracks).find((t) => t.video_id === keep);
      const label = `${btn.dataset.keepLabel}${entry?.album ? " [" + entry.album + "]" : ""}`.trim();
      const others = group?.groups?.flatMap((g) => g.tracks).filter((t) => t.video_id !== keep) || [];
      const ok = await confirmModal("Remove duplicate copies", `Keep “${label}” and remove ${others.length} other copy/copies (unlike + delete files)?`);
      if (!ok) return;
      try {
        const r = await api("/api/duplicates/within/remove", {
          method: "POST",
          body: { slug: btn.dataset.withinKeep, video_ids: others.map((t) => t.video_id) },
        });
        toast(`Removed ${r.removed.length} duplicate${r.removed.length === 1 ? "" : "s"}`, "success");
        loadLibrary();
        renderDuplicates();
      } catch (e) { toast(e.message, "error"); }
    };
  });
}

async function renderStatus() {
  const seq = state._renderSeq;
  view.innerHTML = spinner();
  const targets = state.scope === "all" && state.library
    ? state.library.accounts.map((a) => a.slug)
    : [state.scope];

  let html = `<div class="section-head"><h2>Library status — ${esc(selectedAccountName())}</h2></div>`;

  for (const slug of targets) {
    let status = {};
    try { status = await api(`/api/status/${slug}`); } catch (e) {}
    if (seq !== state._renderSeq) return;
    const counts = status.counts || {};

    html += `
      <div class="panel">
        <h3>${esc(slug)} <span style="color:var(--text-faint);font-weight:600;font-size:12px">${status.quality || ""} · ${status.timestamp ? new Date(status.timestamp * 1000).toLocaleString() : "no snapshot yet"}</span></h3>
        <div class="stat-grid" style="margin:6px 0 0">
          ${[["liked", "Liked"], ["downloaded", "Downloaded"], ["not_downloaded", "Not downloaded"], ["missing", "Missing files"], ["no_longer_liked", "No longer liked"], ["failed", "Failed"], ["skipped", "Skipped"], ["premium", "Premium-locked"]].map(([k, label]) => `
            <div class="stat-card" style="padding:14px 16px">
              <div class="stat-value" style="font-size:20px">${counts[k] ?? 0}</div>
              <div class="stat-label">${label}</div>
            </div>`).join("")}
        </div>
        <div class="filter-bar" style="margin-top:12px">
          <button class="btn btn-ghost" data-act="repair" data-slug="${esc(slug)}">&#10003; Repair downloads</button>
          <button class="btn btn-ghost" data-act="upgrade" data-slug="${esc(slug)}">&#9650; Upgrade quality</button>
          <button class="btn btn-ghost" data-act="premium" data-slug="${esc(slug)}">&#128176; Retry premium</button>
          <button class="btn btn-ghost" data-act="purge" data-slug="${esc(slug)}">&#128465; Purge no-longer-liked</button>
          <button class="btn btn-ghost" data-act="verify" data-slug="${esc(slug)}">&#128221; Verify files</button>
        </div>
      </div>`;

    for (const [key, label, icon] of [
      ["missing", "MISSING FILES", "&#9888;"],
      ["not_downloaded", "NOT DOWNLOADED", "&#128718;"],
      ["premium", "PREMIUM-LOCKED", "&#128176;"],
      ["no_longer_liked", "NO LONGER LIKED", "&#9824;"],
      ["failed", "FAILED", "&#10007;"],
      ["skipped", "SKIPPED", "&#8618;"],
    ]) {
      const items = status[key] || [];
      if (!items.length) continue;
      html += `
        <div class="dup-section">
          <div class="dup-title">${icon} ${label} (${items.length})</div>
          ${items.slice(0, 40).map((it) => `
            <div class="check-row"><span style="flex:1;min-width:0">
              <strong>${esc(it.title)}</strong> — <span style="color:var(--text-dim)">${esc(it.artist)}</span>
              <span style="color:var(--text-faint);display:block;font-size:12px">${esc(it.reason || "")}</span>
            </span></div>`).join("")}
        </div>`;
    }
  }

  view.innerHTML = html;

  $$("[data-act]", view).forEach((btn) => {
    btn.onclick = () => {
      if (btn.dataset.act === "repair") repairDialog(btn.dataset.slug);
      else if (btn.dataset.act === "upgrade") upgradeDialog(btn.dataset.slug);
      else if (btn.dataset.act === "premium") premiumRetryDialog(btn.dataset.slug);
      else if (btn.dataset.act === "verify") verifyDialog(btn.dataset.slug);
      else purgeDialog(btn.dataset.slug);
    };
  });
}

async function verifyDialog(slug) {
  const ok = await confirmModal("Verify files", `Hash every downloaded file in <strong>${esc(slug)}</strong> and flag any that are missing or corrupted?`);
  if (!ok) return;
  try {
    const report = await api("/api/verify", { method: "POST", body: { slug } });
    const s = report.summary || {};
    const bad = (s.missing || 0) + (s.corrupt || 0) + (s.size_zero || 0);
    const acc = (state.library?.accounts || []).find((a) => a.slug === slug);
    const heading = `Verification — ${esc(acc?.name || slug)}`;
    if (!bad) {
      toast(`All ${report.total} files OK`, "success");
      return;
    }
    const problems = Object.values(report.results || {}).filter((r) => r.status !== "ok");
    const rows = problems.map((r) => `
      <tr>
        <td style="color:var(--text-faint)">${esc(r.status)}</td>
        <td><div class="track-cell"><div class="t-main"><div class="t-title">${esc(r.title)}</div><div class="t-sub">${esc(r.artist)}</div></div></div></td>
        <td style="color:var(--text-dim)">${esc(r.quality)}</td>
        <td style="color:var(--text-faint);font-size:12px">${esc(r.reason || "")}</td>
      </tr>`).join("");
    const action = await modal(heading, `
      <p><strong>${bad}</strong> problem file${bad === 1 ? "" : "s"} found (${s.missing || 0} missing, ${s.corrupt || 0} corrupt, ${s.size_zero || 0} empty).</p>
      <table class="tracks"><thead><tr><th>Status</th><th>Title</th><th>Quality</th><th>Reason</th></tr></thead><tbody>${rows}</tbody></table>`,
      [
        { label: "Close", value: () => true },
        { label: "Repair", variant: "btn-primary", value: () => "repair" },
      ]);
    if (action === "repair") repairDialog(slug);
  } catch (e) { toast(e.message, "error"); }
}

async function upgradeDialog(slug) {
  const config = await modal("Upgrade quality", `
    <p>Re-download every track in <strong>${esc(slug)}</strong> that currently has a file at a lower quality. Existing higher-quality files are left alone.</p>
    <div class="label">Target quality</div>
    <select name="quality" class="input">
      ${QUALITIES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("")}
    </select>`,
    [
      { label: "Cancel", value: () => false },
      {
        label: "Preview",
        variant: "btn-primary",
        value: () => ({ quality: $("#modal-root select[name=quality]").value }),
      },
    ]);
  if (!config) return;

  const acc = (state.library?.accounts || []).find((a) => a.slug === slug);
  let preview;
  try { preview = await api(`/api/upgrade-preview/${slug}?quality=${encodeURIComponent(config.quality)}`); }
  catch (e) { toast(e.message, "error"); return; }
  const n = preview.count ?? 0;
  if (!n) { toast(`Nothing to upgrade for ${acc?.name || slug}`, "info"); return; }

  const ok = await modal("Upgrade preview", `
    <p><strong>${n}</strong> track${n === 1 ? "" : "s"} in <strong>${esc(acc?.name || slug)}</strong> can be upgraded to ${esc(config.quality)}. Their current files will be replaced.</p>`,
    [
      { label: "Cancel", value: () => false },
      { label: "Upgrade all", variant: "btn-primary", value: () => true },
    ]);
  if (!ok) return;
  try {
    await startDownload({ account: slug, quality: config.quality, existing_policy: "replace", session: "once", output: "upgrade" });
    toast(`Upgrading ${n} track${n === 1 ? "" : "s"}…`, "info");
  } catch (e) { toast(e.message, "error"); }
}

async function premiumRetryDialog(slug) {
  const ok = await confirmModal("Retry premium-locked tracks", `Retry premium-locked tracks for ${slug} now?`);
  if (!ok) return;
  try {
    const r = await api("/api/scheduler/premium_retry", { method: "POST" });
    const result = (r.results || {})[slug];
    const started = result?.started;
    if (started) toast(`Premium retry started (${result.count} tracks)`, "info");
    else if (result?.error) toast(`Premium retry: ${result.error}`, "error");
    else toast("No premium-locked tracks to retry", "info");
  } catch (e) { toast(e.message, "error"); }
}

async function repairDialog(slug) {
  const config = await modal("Repair downloads", `
    <p>Re-download every currently-liked track in <strong>${esc(slug)}</strong> that has no playable file at the chosen quality. Files that already exist are left alone.</p>
    <div class="label">Quality to repair</div>
    <select name="quality" class="input">
      ${QUALITIES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("")}
    </select>`,
    [
      { label: "Cancel", value: () => false },
      {
        label: "Repair",
        variant: "btn-primary",
        value: () => ({ quality: $("#modal-root select[name=quality]").value }),
      },
    ]);
  if (!config) return;
  try {
    await startDownload({
      account: slug,
      quality: config.quality,
      existing_policy: "skip",
      session: "once",
      output: "repair",
    });
    toast("Repair download started", "info");
  } catch (e) { toast(e.message, "error"); }
}

async function purgeDialog(slug) {
  let items = [];
  try { items = await api(`/api/accounts/${slug}/unliked`); } catch (e) { toast(e.message, "error"); return; }
  if (!items.length) { toast("No no-longer-liked files to purge", "info"); return; }
  const totalBytes = items.reduce((s, i) => s + (i.size_bytes || 0), 0);
  const ok = await modal("Purge no-longer-liked files", `
    <p><strong>${items.length}</strong> track${items.length === 1 ? "" : "s"} were downloaded but are no longer in Liked Music. Their local files (${fmtSize(totalBytes)}) will be deleted.</p>
    <div style="max-height:280px;overflow:auto;border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:6px 12px">
      ${items.slice(0, 40).map((i) => `
        <div class="check-row"><span style="flex:1;min-width:0">
          <strong>${esc(i.title)}</strong> — <span style="color:var(--text-dim)">${esc(i.artist)}</span>
          <span style="color:var(--text-faint);font-size:12px">(${fmtSize(i.size_bytes || 0)})</span>
        </span></div>`).join("")}
    </div>
    ${items.length > 40 ? `<p style="color:var(--text-faint);font-size:12px">+${items.length - 40} more</p>` : ""}`,
    [
      { label: "Cancel", value: () => false },
      { label: "Delete files", variant: "btn-danger", value: () => true },
    ]);
  if (!ok) return;
  try {
    const result = await api(`/api/accounts/${slug}/unliked`, {
      method: "POST",
      body: { video_ids: items.map((i) => i.video_id) },
    });
    const freed = result.freed_bytes || 0;
    toast(`Purged ${result.video_ids.length} track${result.video_ids.length === 1 ? "" : "s"}, freed ${fmtSize(freed)}`, "success");
    renderStatus();
    loadLibrary();
  } catch (e) { toast(e.message, "error"); }
}

async function renderDownloads() {
  const sessions = Object.values(state.downloads);
  view.innerHTML = `
    <div class="section-head"><h2>Downloads</h2>
      ${sessions.length ? `<button id="dl-cancel-all" class="btn btn-danger" style="float:right">&#10005; Cancel all</button>` : ""}
    </div>
    ${sessions.length ? "" : `<div class="empty-state"><div class="big">&#8681;</div>No active download sessions. Start one from any song list or the download buttons.</div>`}`;

  sessions.forEach((s) => {
    const counts = s.counts || {};
    const fin = s.finished_info || {};
    const failedRes = (fin.results || []).filter((r) => r.status === "failed");
    const failedIds = [...new Set(failedRes.map((r) => r.video_id))].filter(Boolean);
    const node = document.createElement("div");
    node.className = `session-card${s.finished ? " finished" : ""}`;
    node.id = `session-${s.slug}`;
    node.innerHTML = `
      <div class="session-head">
        <div>
          <div class="session-title">${esc(s.name)} <span class="slug">${s.quality || ""}</span></div>
          <div class="session-meta">
            ${s.finished
              ? `${s.finished_info?.cancelled ? "Cancelled" : "Finished"} — ${fin.downloaded || 0} downloaded, ${fin.skipped || 0} skipped, ${fin.failed || 0} failed`
              : (s.track ? `${s.track.number}/${s.track.total} — ${esc(s.track.title)}` : "Starting…")}
          </div>
        </div>
        <div class="session-title">${s.finished ? (s.finished_info?.cancelled ? "Stopped" : "Done") : Math.round(s.progress) + "%"}</div>
      </div>
      <div class="progress-track"><div class="progress-fill" style="width:${Math.max(0, Math.min(100, s.progress))}%"></div></div>
      <div class="session-footer">
        <span>D:${counts.downloaded || 0} S:${counts.skipped || 0} F:${counts.failed || 0} U:${counts.unavailable || 0}</span>
        <span>
          ${failedIds.length ? `<button class="btn btn-ghost" style="padding:6px 14px;font-size:12px" data-act="retry" data-slug="${esc(s.slug)}" data-quality="${esc(s.quality || "best")}">&#8635; Retry ${failedIds.length} failed</button>` : ""}
          ${s.finished ? "" : `<button class="btn btn-ghost" style="padding:6px 14px;font-size:12px" data-act="stop" data-slug="${esc(s.slug)}">Stop after stage</button>`}
          ${s.finished ? "" : `<button class="btn btn-danger" style="padding:6px 14px;font-size:12px" data-act="cancel" data-slug="${esc(s.slug)}">Cancel</button>`}
        </span>
      </div>`;
    if (failedRes.length) {
      const show = failedRes.slice(0, 8);
      const more = failedRes.length - show.length;
      const list = document.createElement("div");
      list.className = "session-failures";
      list.innerHTML = show.map((r) => `<div class="check-row"><span style="flex:1;min-width:0"><strong>${esc(r.title)}</strong> — <span style="color:var(--text-dim)">${esc(r.artist)}</span><span style="color:var(--text-faint);display:block;font-size:12px">${esc(r.reason || "")}</span></span></div>`).join("")
        + (more > 0 ? `<div class="dup-title" style="padding:6px 0">…and ${more} more</div>` : "");
      node.appendChild(list);
    }
    view.appendChild(node);
  });

  $("#dl-cancel-all")?.addEventListener("click", async () => {
    try {
      await api("/api/downloads/cancel-all", { method: "POST", body: {} });
      toast("Cancelling all sessions…", "info");
    } catch (e) { toast(e.message, "error"); }
  });

  $$("[data-act]", view).forEach((btn) => {
    btn.onclick = async () => {
      const slug = btn.dataset.slug;
      const act = btn.dataset.act;
      try {
        if (act === "retry") {
          const fin2 = state.downloads[slug]?.finished_info || {};
          const ids = [...new Set((fin2.results || []).filter((r) => r.status === "failed").map((r) => r.video_id))].filter(Boolean);
          if (!ids.length) return;
          await startDownload({ account: slug, quality: btn.dataset.quality, existing_policy: "skip", session: "once", video_ids: ids });
          toast(`Retrying ${ids.length} failed track${ids.length === 1 ? "" : "s"}…`, "info");
          return;
        }
        await api(`/api/downloads/${slug}/${act}`, { method: "POST", body: {} });
        toast(act === "stop" ? "Stopping after current stage…" : "Cancelling…", "info");
      } catch (e) { toast(e.message, "error"); }
    };
  });
}

async function renderAccounts() {
  const accounts = state.library ? state.library.accounts : [];
  view.innerHTML = `
    <div class="section-head"><h2>Accounts</h2></div>
    <div class="panel">
      <h3>&#65291; Add account</h3>
      <div class="label">Account name</div>
      <input id="acc-name" class="input" type="text" placeholder="e.g. Shay Mercer" />
      <div class="label">Request headers (from browser Developer Tools → Network → /browse request)</div>
      <textarea id="acc-headers" class="input" placeholder="Paste the complete 'Copy request headers' block here…"></textarea>
      <div class="modal-actions" style="margin-top:14px">
        <button id="acc-test" class="btn btn-ghost">Test &amp; Add</button>
      </div>
    </div>
    <div class="panel">
      <h3>Connected accounts</h3>
      ${accounts.length ? accounts.map((a) => `
        <div class="check-row">
          <span class="account-avatar" style="background:${paletteFor(a.name)};color:#000">${esc(initialsOf(a.name))}</span>
          <span style="flex:1;min-width:0">
            <strong>${esc(a.name)}</strong>
            <span style="color:var(--text-dim);display:block;font-size:12px">
              ${a.liked_count} liked · ${a.downloaded_count} downloaded · ${fmtSize(a.size_bytes)}
            </span>
          </span>
          <button class="btn btn-ghost" data-act="refresh" data-slug="${esc(a.slug)}">Refresh</button>
          <button class="btn btn-danger" data-act="delete" data-slug="${esc(a.slug)}">Remove</button>
        </div>`).join("") : `<div class="empty-state" style="padding:20px">No accounts connected.</div>`}
    </div>`;

  $("#acc-test").onclick = async () => {
    const name = $("#acc-name").value.trim();
    const headers = $("#acc-headers").value;
    if (!name || !headers) { toast("Name and headers are required", "error"); return; }
    const btn = $("#acc-test");
    btn.disabled = true;
    try {
      await api("/api/accounts", { method: "POST", body: { name, headers } });
      toast("Account added", "success");
      $("#acc-name").value = "";
      $("#acc-headers").value = "";
      await loadLibrary();
      renderAccounts();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      btn.disabled = false;
    }
  };

  $$("[data-act]", view).forEach((btn) => {
    const slug = btn.dataset.slug;
    const act = btn.dataset.act;
    btn.onclick = async () => {
      if (act === "refresh") {
        try { await refreshLibrary(slug); } catch (e) { toast(e.message, "error"); }
        return;
      }
      const ok = await confirmModal("Remove account", `${slug} will be removed. Authentication and metadata are deleted; downloaded music files are NOT deleted.`);
      if (!ok) return;
      try {
        await api(`/api/accounts/${slug}`, { method: "DELETE" });
        toast("Account removed", "info");
        if (state.scope === slug) state.scope = "all";
        await loadLibrary();
        navigate();
      } catch (e) { toast(e.message, "error"); }
    };
  });
}

async function renderSettings() {
  let settings = {};
  try { settings = await api("/api/settings"); } catch (e) {}

  const accent = localStorage.getItem("ls-accent") || "#1db954";

  view.innerHTML = `
    <div class="section-head"><h2>Settings</h2></div>
    <div class="panel">
      <h3>Appearance</h3>
      <div class="label">Accent color</div>
      <div class="filter-bar">
        <input id="set-accent" class="input" type="color" value="${esc(accent)}" style="width:70px;padding:4px;height:40px" />
        <div class="filter-bar" id="accent-presets" style="margin:0"></div>
      </div>
      <div class="label">Download defaults</div>
      <div class="label">Default audio quality</div>
      <select id="set-quality" class="input">
        ${QUALITIES.map(([v, l]) => `<option value="${v}" ${settings.default_quality === v ? "selected" : ""}>${esc(l)}</option>`).join("")}
      </select>
      <div class="label">If audio already exists</div>
      <select id="set-policy" class="input">
        <option value="skip" ${settings.default_existing_policy === "skip" ? "selected" : ""}>Skip existing file</option>
        <option value="replace" ${settings.default_existing_policy === "replace" ? "selected" : ""}>Replace existing file</option>
      </select>
      <div class="label">Default session mode</div>
      <select id="set-session" class="input">
        <option value="once" ${settings.default_session === "once" ? "selected" : ""}>Run once</option>
        <option value="loop" ${settings.default_session === "loop" ? "selected" : ""}>Loop until stopped</option>
        <option value="skip" ${settings.default_session === "skip" ? "selected" : ""}>Download missing across all qualities</option>
      </select>
      <div class="filter-bar">
        <div style="flex:1"><div class="label">Most Played count</div><input id="set-limit" class="input" type="number" min="1" max="100" value="${settings.default_limit ?? 25}" /></div>
        <div style="flex:1"><div class="label">Retry attempts</div><input id="set-attempts" class="input" type="number" min="1" max="20" value="${settings.retry_attempts ?? 3}" /></div>
        <div style="flex:1"><div class="label">Retry delay (s)</div><input id="set-delay" class="input" type="number" min="0" max="300" value="${settings.retry_delay ?? 3}" /></div>
      </div>
      <div class="label" style="margin-top:14px">Automation</div>
      <div class="filter-bar">
        <div style="flex:1"><div class="label">Auto-refresh library (hours, 0 = off)</div><input id="set-auto-refresh" class="input" type="number" min="0" max="168" value="${settings.auto_refresh_interval_hours ?? 0}" /></div>
        <div style="flex:1"><div class="label">Retry premium-locked (days, 0 = off)</div><input id="set-premium-interval" class="input" type="number" min="0" max="90" value="${settings.premium_retry_interval_days ?? 7}" /></div>
        <div style="flex:1"><div class="label">Auto-backup (hours, 0 = off)</div><input id="set-backup-interval" class="input" type="number" min="0" max="336" value="${settings.backup_interval_hours ?? 0}" /></div>
      </div>
      <div class="filter-bar">
        <div style="flex:1"><div class="label">Parallel downloads (1-8)</div><input id="set-parallel" class="input" type="number" min="1" max="8" value="${settings.max_parallel_downloads ?? 1}" /></div>
        <div style="flex:1"><div class="label">Download rate limit (e.g. 500K, 1M, empty = unlimited)</div><input id="set-rate-limit" class="input" type="text" value="${esc(settings.download_rate_limit || "")}" placeholder="Unlimited" autocomplete="off" /></div>
        <div style="flex:1;display:flex;align-items:flex-end;padding-bottom:6px">
          <label class="check-label"><input id="set-auto-dl" type="checkbox" ${settings.auto_download_new_likes ? "checked" : ""} /> Auto-download new likes after refresh</label>
        </div>
        <div style="flex:1;display:flex;align-items:flex-end;padding-bottom:6px">
          <label class="check-label"><input id="set-notif" type="checkbox" ${settings.notifications_enabled ? "checked" : ""} /> Desktop notifications</label>
        </div>
      </div>
      <div class="label" style="margin-top:14px">last.fm scrobbling</div>
      <div class="filter-bar">
        <div style="flex:1"><div class="label">API key</div><input id="set-lfm-key" class="input" type="text" value="${esc(settings.lastfm_api_key || "")}" autocomplete="off" /></div>
        <div style="flex:1"><div class="label">API secret</div><input id="set-lfm-secret" class="input" type="password" value="${esc(settings.lastfm_api_secret || "")}" autocomplete="off" /></div>
      </div>
      <div class="filter-bar">
        <div style="flex:1"><div class="label">Username</div><input id="set-lfm-user" class="input" type="text" value="${esc(settings.lastfm_username || "")}" autocomplete="off" /></div>
        <div style="flex:1"><div class="label">Password (MD5)</div><input id="set-lfm-pass" class="input" type="password" value="${esc(settings.lastfm_password_md5 || "")}" autocomplete="off" /></div>
        <div style="flex:1;display:flex;align-items:flex-end;padding-bottom:6px">
          <label class="check-label"><input id="set-lfm-enabled" type="checkbox" ${settings.lastfm_enabled ? "checked" : ""} /> Enable scrobbling</label>
        </div>
      </div>
      <div class="label">API token</div>
      <div class="label">Token (only if the server was started with --token)</div>
      <div class="filter-bar">
        <input id="set-token" class="input" type="password" placeholder="Bearer token" value="${esc(state.token)}" autocomplete="off" />
        <button id="set-token-save" class="btn">Save token</button>
        <button id="set-token-clear" class="btn btn-ghost">Clear</button>
      </div>
      <div class="label">Maintenance</div>
      <div class="filter-bar">
        <button id="set-ytdlp-check" class="btn btn-ghost">&#8635; Check yt-dlp update</button>
        <button id="set-scrobble" class="btn btn-ghost">&#128228; Flush last.fm scrobbles</button>
        <button id="set-premium-now" class="btn btn-ghost">&#128176; Retry premium now</button>
        <button id="set-backup-now" class="btn btn-ghost">&#128190; Backup now</button>
      </div>
      <div class="label">Recent backups</div>
      <div id="backup-list" class="check-row" style="justify-content:space-between"><span style="color:var(--text-faint)">Loading…</span></div>
      <div class="filter-bar" style="margin-top:6px">
        <div style="flex:1"><div class="label">Keep newest N backups</div><input id="set-keep-backups" class="input" type="number" min="1" max="200" value="20" /></div>
        <div style="flex:1;display:flex;align-items:flex-end;padding-bottom:6px"><button id="set-prune-backups" class="btn btn-ghost">&#128465; Prune old backups</button></div>
      </div>
      <div class="modal-actions" style="margin-top:18px">
        <button id="set-save" class="btn btn-primary">Save settings</button>
        <button id="set-backup" class="btn btn-ghost">Export backup</button>
      </div>
    </div>`;

  const presets = ["#1db954", "#1d8ab9", "#b91d4a", "#8a2be2", "#e8a018", "#18c2a0"];
  const presetBar = $("#accent-presets");
  presets.forEach((p) => {
    const b = document.createElement("button");
    b.style.cssText = `width:34px;height:34px;border-radius:50%;background:${p};border:2px solid ${p === accent ? "#fff" : "transparent"}`;
    b.onclick = () => {
      $("#set-accent").value = p;
      $$("#accent-presets button").forEach((x) => (x.style.borderColor = "transparent"));
      b.style.borderColor = "#fff";
    };
    presetBar.appendChild(b);
  });

  $("#set-accent").oninput = (e) => applyAccent(e.target.value);

  $("#set-token-save").onclick = async () => {
    const token = $("#set-token").value.trim();
    state.token = token;
    if (token) {
      localStorage.setItem("ls-token", token);
      await loadLibrary();
      toast("Token saved", "success");
    } else {
      localStorage.removeItem("ls-token");
      toast("Token cleared", "info");
    }
  };
  $("#set-token-clear").onclick = () => {
    state.token = "";
    localStorage.removeItem("ls-token");
    $("#set-token").value = "";
    toast("Token cleared", "info");
  };

  $("#set-save").onclick = async () => {
    const body = {
      default_quality: $("#set-quality").value,
      default_existing_policy: $("#set-policy").value,
      default_session: $("#set-session").value,
      default_limit: Number($("#set-limit").value),
      retry_attempts: Number($("#set-attempts").value),
      retry_delay: Number($("#set-delay").value),
      auto_refresh_interval_hours: Number($("#set-auto-refresh").value),
      premium_retry_interval_days: Number($("#set-premium-interval").value),
      backup_interval_hours: Number($("#set-backup-interval").value),
      max_parallel_downloads: Number($("#set-parallel").value),
      download_rate_limit: $("#set-rate-limit").value.trim(),
      auto_download_new_likes: $("#set-auto-dl").checked,
      notifications_enabled: $("#set-notif").checked,
      lastfm_enabled: $("#set-lfm-enabled").checked,
      lastfm_api_key: $("#set-lfm-key").value.trim(),
      lastfm_api_secret: $("#set-lfm-secret").value.trim(),
      lastfm_username: $("#set-lfm-user").value.trim(),
      lastfm_password_md5: $("#set-lfm-pass").value.trim(),
    };
    try {
      state.settings = await api("/api/settings", { method: "POST", body });
      toast("Settings saved", "success");
    } catch (e) { toast(e.message, "error"); }
  };

  $("#set-notif").onchange = () => {
    if ($("#set-notif").checked && "Notification" in window && Notification.permission === "default") {
      Notification.requestPermission();
    }
  };

  $("#set-ytdlp-check").onclick = async () => {
    try {
      const r = await api("/api/yt-dlp-update");
      if (r.error) { toast(`yt-dlp check failed: ${r.error}`, "error"); return; }
      if (r.update_available) toast(`yt-dlp ${r.current} → ${r.latest} available`, "info");
      else toast(`yt-dlp is up to date (${r.current || "?"})`, "success");
    } catch (e) { toast(e.message, "error"); }
  };

  $("#set-scrobble").onclick = async () => {
    try {
      const r = await api("/api/lastfm/flush", { method: "POST", body: {} });
      if (r.error) toast(`last.fm flush failed: ${r.error}`, "error");
      else toast(`Scrobbled ${r.sent} track${r.sent === 1 ? "" : "s"}, ${r.failed} failed`, r.sent ? "success" : "info");
    } catch (e) { toast(e.message, "error"); }
  };

  $("#set-premium-now").onclick = async () => {
    try {
      const r = await api("/api/scheduler/premium_retry", { method: "POST" });
      const started = Object.values(r.results || {}).filter((v) => v?.started).length;
      toast(started ? `Premium retry started for ${started} account${started === 1 ? "" : "s"}` : "No premium-locked tracks to retry", started ? "info" : "success");
    } catch (e) { toast(e.message, "error"); }
  };

  $("#set-backup-now").onclick = async () => {
    try {
      const r = await api("/api/backup", { method: "POST", body: {} });
      toast(`Backup written to ${r.file}`, "success");
      renderBackups();
    } catch (e) { toast(e.message, "error"); }
  };

  $("#set-prune-backups").onclick = async () => {
    const keep = Number($("#set-keep-backups").value) || 20;
    try {
      const r = await api("/api/backups/prune", { method: "POST", body: { keep } });
      toast(`Pruned ${r.pruned} old backup${r.pruned === 1 ? "" : "s"}`, "info");
      renderBackups();
    } catch (e) { toast(e.message, "error"); }
  };

  renderBackups();

  $("#set-backup").onclick = async () => {
    try {
      const r = await api("/api/backup", { method: "POST", body: {} });
      toast(`Backup written to ${r.file}`, "info");
      renderBackups();
    } catch (e) { toast(e.message, "error"); }
  };
}

async function renderBackups() {
  const box = $("#backup-list");
  if (!box) return;
  try {
    const files = await api("/api/backups");
    box.innerHTML = files.length
      ? files.slice(0, 8).map((f) => `<span>${esc(f.name)} <span style="color:var(--text-faint)">· ${fmtSize(f.size_bytes)} · ${new Date(f.created_at * 1000).toLocaleString()}</span></span>`).join("")
      : `<span style="color:var(--text-faint)">No backups yet.</span>`;
  } catch (e) {
    box.innerHTML = `<span style="color:var(--text-faint)">Could not load backups.</span>`;
  }
}

function applyAccent(color) {
  localStorage.setItem("ls-accent", color);
  document.documentElement.style.setProperty("--accent", color);
  document.documentElement.style.setProperty("--accent-hover", color);
}

/* -------------------------------------------------------------------------
 * Playlists
 * ---------------------------------------------------------------------- */

async function renderPlaylists() {
  const seq = state._renderSeq;
  view.innerHTML = spinner();
  let data;
  try { data = await api("/api/playlists"); } catch (e) { view.innerHTML = `<div class="alert alert-error">${esc(e.message)}</div>`; return; }
  if (seq !== state._renderSeq) return;

  const custom = data.custom || [];
  const ytm = data.ytm || {};

  let html = `
    <div class="section-head"><h2>Playlists</h2></div>
    <div class="filter-bar">
      <button id="pl-new" class="btn btn-primary">&#43; New playlist</button>
      <button id="pl-import" class="btn btn-ghost">&#8681; Import (M3U / JSON / Spotify)</button>
      ${Object.keys(ytm).length ? `<button id="pl-sync" class="btn btn-ghost">&#8635; Sync YT Music playlists</button>` : ""}
    </div>`;

  const customPl = custom.filter((p) => p.kind === "custom");
  const smartPl = custom.filter((p) => p.kind === "smart");

  if (customPl.length) {
    html += `<div class="section-head" style="margin-top:18px"><h3>Custom playlists</h3></div>
      <div class="card-grid">
        ${customPl.map((p) => `
          <a href="#/playlist/${encodeURIComponent(p.id)}" style="text-decoration:none;color:inherit">
            <div class="card">
              <div class="card-art" style="background:linear-gradient(135deg,#1f1c2c,#928dab);font-size:34px;display:flex;align-items:center;justify-content:center;color:#fff">&#9835;</div>
              <div class="card-title">${esc(p.name)}</div>
              <div class="card-sub">${p.track_count} track${p.track_count === 1 ? "" : "s"}</div>
            </div>
          </a>`).join("")}
      </div>`;
  }

  if (smartPl.length) {
    html += `<div class="section-head" style="margin-top:18px"><h3>Smart playlists</h3></div>
      <div class="card-grid">
        ${smartPl.map((p) => `
          <a href="#/playlist/${encodeURIComponent(p.id)}" style="text-decoration:none;color:inherit">
            <div class="card">
              <div class="card-art" style="background:linear-gradient(135deg,#134e5e,#71b280);font-size:34px;display:flex;align-items:center;justify-content:center;color:#fff">&#9881;</div>
              <div class="card-title">${esc(p.name)}</div>
              <div class="card-sub">${p.track_count} track${p.track_count === 1 ? "" : "s"} · auto</div>
            </div>
          </a>`).join("")}
      </div>`;
  }

  if (!customPl.length && !smartPl.length) {
    html += `<div class="empty-state" style="padding:24px"><div class="big">&#9835;</div>No custom playlists yet. Create one to group tracks.</div>`;
  }

  for (const [slug, playlists] of Object.entries(ytm)) {
    const acc = (state.library?.accounts || []).find((a) => a.slug === slug);
    const name = acc?.name || slug;
    html += `<div class="section-head" style="margin-top:18px"><h3>YouTube Music — ${esc(name)}</h3></div>
      ${playlists.length ? `<div class="card-grid">
        ${playlists.map((p) => `
          <a href="#/playlist/ytm/${encodeURIComponent(slug)}/${encodeURIComponent(p.id)}" style="text-decoration:none;color:inherit">
            <div class="card">
              <div class="card-art" style="background:linear-gradient(135deg,#283c86,#45a247);font-size:34px;display:flex;align-items:center;justify-content:center;color:#fff">&#9654;</div>
              <div class="card-title">${esc(p.title || p.id)}</div>
              <div class="card-sub">${p.count ?? ""} tracks</div>
            </div>
          </a>`).join("")}
      </div>` : `<div class="empty-state" style="padding:20px">No synced playlists for this account.</div>`}`;
  }

  view.innerHTML = html;

  $("#pl-new").onclick = () => {
    const accs = state.library?.accounts || [];
    const promise = modal("New playlist", `
      <div class="label">Name</div>
      <input name="name" class="input" type="text" placeholder="e.g. Road trip 2026" />
      <div class="label">Type</div>
      <select name="kind" class="input">
        <option value="custom">Custom — hand-picked tracks</option>
        <option value="smart">Smart — auto-matching rules</option>
      </select>
      <div class="label">Account (optional — restricts which account's tracks it matches)</div>
      <select name="account" class="input">
        <option value="">All accounts</option>
        ${accs.map((a) => `<option value="${esc(a.slug)}">${esc(a.name)}</option>`).join("")}
      </select>
      <div id="smart-rules" style="display:none;margin-top:12px">
        <div class="label">Artist contains</div><input name="artist" class="input" type="text" />
        <div class="label">Genre contains</div><input name="genre" class="input" type="text" />
        <div class="label">Title contains</div><input name="title" class="input" type="text" />
        <div class="label">Album contains</div><input name="album" class="input" type="text" />
        <div class="filter-bar">
          <div style="flex:1"><div class="label">Min plays</div><input name="min_plays" class="input" type="number" min="0" /></div>
          <div style="flex:1"><div class="label">Max plays</div><input name="max_plays" class="input" type="number" min="0" /></div>
        </div>
        <div class="filter-bar">
          <div style="flex:1"><div class="label">Year</div><input name="year" class="input" type="number" min="1900" max="2100" /></div>
          <div style="flex:1"><div class="label">Max tracks</div><input name="max_count" class="input" type="number" min="1" /></div>
        </div>
        <div class="label">Status</div>
        <select name="status" class="input"><option value="">Any</option><option value="downloaded">Downloaded</option><option value="missing">Missing files</option><option value="premium">Premium-locked</option></select>
      </div>`,
      [
        { label: "Cancel", value: () => false },
        { label: "Create", variant: "btn-primary", value: () => {
          const kind = $("#modal-root select[name=kind]").value;
          const rules = {};
          if (kind === "smart") {
            const get = (n) => $("#modal-root input[name=" + n + "]")?.value?.trim();
            const num = (n) => { const v = get(n); return v === "" ? undefined : Number(v); };
            rules.account = $("#modal-root select[name=account]").value || undefined;
            rules.status = $("#modal-root select[name=status]").value || undefined;
            rules.artist = get("artist"); rules.genre = get("genre"); rules.title = get("title"); rules.album = get("album");
            rules.min_plays = num("min_plays"); rules.max_plays = num("max_plays");
            rules.year = num("year"); rules.max_count = num("max_count");
          }
          return { name: $("#modal-root input[name=name]").value.trim(), kind, account: rules.account, rules };
        } },
      ]);
    $("#modal-root select[name=kind]").onchange = () => {
      $("#smart-rules").style.display = $("#modal-root select[name=kind]").value === "smart" ? "block" : "none";
    };
    promise.then((value) => { if (value) createPlaylist(value); });
  };

  $("#pl-import").onclick = () => {
    const accs = state.library?.accounts || [];
    const promise = modal("Import playlist", `
      <div class="label">Source</div>
      <select name="type" class="input">
        <option value="m3u">M3U file contents</option>
        <option value="json">LocalSpot JSON export</option>
        <option value="spotify">Spotify playlist URL</option>
      </select>
      <div class="label">Name (m3u only)</div>
      <input name="name" class="input" type="text" placeholder="Imported playlist" />
      <div id="import-acc-wrap" style="display:none"><div class="label">Account (spotify import matching)</div>
        <select name="account" class="input"><option value="">All accounts</option>${accs.map((a) => `<option value="${esc(a.slug)}">${esc(a.name)}</option>`).join("")}</select>
      </div>
      <div class="label" id="content-label">M3U contents (paths or URLs)</div>
      <textarea name="content" class="input" style="min-height:140px" placeholder="Paste file contents here…"></textarea>`,
      [
        { label: "Cancel", value: () => false },
        { label: "Import", variant: "btn-primary", value: () => ({
          type: $("#modal-root select[name=type]").value,
          name: $("#modal-root input[name=name]").value.trim(),
          account: $("#modal-root select[name=account]")?.value,
          content: $("#modal-root textarea[name=content]").value,
        }) },
      ]);
    const setUI = () => {
      const t = $("#modal-root select[name=type]").value;
      $("#import-acc-wrap").style.display = t === "spotify" ? "block" : "none";
      $("#content-label").textContent = t === "spotify" ? "Spotify playlist URL" : (t === "json" ? "JSON export contents" : "M3U contents (paths or URLs)");
    };
    $("#modal-root select[name=type]").onchange = setUI;
    setUI();
    promise.then((value) => { if (value) importPlaylist(value); });
  };

  $("#pl-sync")?.addEventListener("click", async () => {
    try {
      await api("/api/playlists/sync", { method: "POST", body: {} });
      toast("Playlist sync started", "info");
      setTimeout(renderPlaylists, 2500);
    } catch (e) { toast(e.message, "error"); }
  });
}

async function createPlaylist(config) {
  if (!config?.name) { toast("Playlist name is required", "error"); return; }
  try {
    await api("/api/playlists", {
      method: "POST",
      body: { name: config.name, kind: config.kind, account: config.account, rules: config.kind === "smart" ? config.rules : undefined },
    });
    toast(`Playlist “${config.name}” created`, "success");
    renderPlaylists();
  } catch (e) { toast(e.message, "error"); }
}

async function importPlaylist(config) {
  try {
    const body = { type: config.type, name: config.name || undefined };
    if (config.type === "spotify") { body.url = config.content.trim(); body.account = config.account; }
    else body.content = config.content;
    const r = await api("/api/playlists/import", { method: "POST", body });
    const matched = r.matched?.length ?? null;
    toast(matched === null ? `Imported “${r.name}”` : `Imported “${r.name}” — ${matched} matched, ${r.unmatched ?? 0} unmatched`, "success");
    location.hash = `#/playlist/${r.id}`;
  } catch (e) { toast(e.message, "error"); }
}

async function renderPlaylistDetail(id, slug, ytmId) {
  const seq = state._renderSeq;
  view.innerHTML = spinner();
  let playlist;
  let isYtm = false;
  try {
    if (slug) {
      playlist = await api(`/api/playlists/ytm/${slug}/${ytmId}`);
      isYtm = true;
    } else {
      playlist = await api(`/api/playlists/${id}`);
    }
  } catch (e) { view.innerHTML = `<div class="alert alert-error">${esc(e.message)}</div>`; return; }
  if (seq !== state._renderSeq) return;

  const tracks = playlist.tracks || [];
  const kind = isYtm ? "ytm" : playlist.kind;
  const title = esc(playlist.name);

  view.innerHTML = `
    <div class="section-head">
      <h2>${title} <span style="color:var(--text-faint);font-weight:600;font-size:14px">${tracks.length} track${tracks.length === 1 ? "" : "s"}</span></h2>
      <div class="filter-bar" style="margin-top:10px">
        <button id="pl-play" class="btn btn-primary">&#9654; Play all</button>
        ${isYtm
          ? `<button id="pl-dl" class="btn btn-ghost">&#8681; Download playlist</button>`
          : (kind === "custom"
            ? `<button id="pl-export" class="btn btn-ghost">&#8681; Export</button>
               <button id="pl-rename" class="btn btn-ghost">&#9998; Rename</button>
               <button id="pl-delete" class="btn btn-danger">&#128465; Delete</button>`
            : `<button id="pl-export" class="btn btn-ghost">&#8681; Export</button>
               <button id="pl-delete" class="btn btn-danger">&#128465; Delete</button>`)}
      </div>
    </div>
    ${kind === "smart" ? `<div class="alert alert-info">Smart playlist — tracks match its rules automatically. Rules: ${esc(JSON.stringify(playlist.rules || {}))}</div>` : ""}
    ${tracks.length
      ? trackTable(tracks, { emptyText: "No tracks in this playlist." })
      : empty("This playlist has no tracks yet.")}`;

  $("#pl-play").onclick = () => {
    const queueable = tracks.filter((t) => t.stream);
    if (!queueable.length) { toast("Nothing playable in this playlist", "info"); return; }
    setQueue(queueable, 0);
  };

  const dlBtn = $("#pl-dl");
  if (dlBtn) {
    dlBtn.onclick = async () => {
      const config = await downloadDialog({ account: slug, title: `Download “${playlist.name}”` });
      if (!config) return;
      try {
        await startDownload({ ...config, session: "once", output: "playlist", playlist_id: ytmId });
        toast(`Downloading playlist…`, "info");
      } catch (e) { toast(e.message, "error"); }
    };
  }

  const exportBtn = $("#pl-export");
  if (exportBtn) {
    exportBtn.onclick = async () => {
      const fmt = await modal("Export playlist", `
        <div class="label">Format</div>
        <select name="fmt" class="input"><option value="m3u">M3U (paths to local files)</option><option value="json">JSON</option><option value="csv">CSV</option></select>`,
        [
          { label: "Cancel", value: () => false },
          { label: "Export", variant: "btn-primary", value: () => $("#modal-root select[name=fmt]").value },
        ]);
      if (!fmt) return;
      try {
        const r = await api(`/api/playlists/${id}/export`, { method: "POST", body: { format: fmt } });
        toast(`Exported to ${r.file}`, "success");
      } catch (e) { toast(e.message, "error"); }
    };
  }

  const renameBtn = $("#pl-rename");
  if (renameBtn) {
    renameBtn.onclick = async () => {
      const name = await modal("Rename playlist", `<div class="label">Name</div><input name="name" class="input" type="text" value="${esc(playlist.name)}" />`,
        [
          { label: "Cancel", value: () => false },
          { label: "Save", variant: "btn-primary", value: () => $("#modal-root input[name=name]").value.trim() },
        ]);
      if (!name) return;
      try {
        await api(`/api/playlists/${id}/update`, { method: "POST", body: { name } });
        toast("Renamed", "success");
        navigate();
      } catch (e) { toast(e.message, "error"); }
    };
  }

  const deleteBtn = $("#pl-delete");
  if (deleteBtn) {
    deleteBtn.onclick = async () => {
      const ok = await confirmModal("Delete playlist", `${playlist.name} will be deleted. Tracks are not removed from your library.`);
      if (!ok) return;
      try {
        await api(`/api/playlists/${id}`, { method: "DELETE" });
        toast("Playlist deleted", "info");
        location.hash = "#/playlists";
      } catch (e) { toast(e.message, "error"); }
    };
  }

  bindTrackTable(view, tracks);
}

/* -------------------------------------------------------------------------
 * Stats
 * ---------------------------------------------------------------------- */

async function renderStats() {
  const seq = state._renderSeq;
  view.innerHTML = spinner();
  let data;
  try { data = await api("/api/stats"); } catch (e) { view.innerHTML = `<div class="alert alert-error">${esc(e.message)}</div>`; return; }
  if (seq !== state._renderSeq) return;

  const perAccount = data.per_account || {};
  const recent = data.recent || [];

  let html = `
    <div class="section-head"><h2>Listening stats</h2></div>
    <div class="stat-grid">
      <div class="stat-card"><div class="stat-value">${data.total_plays?.toLocaleString() || 0}</div><div class="stat-label">Total plays</div></div>
      <div class="stat-card"><div class="stat-value">${data.unique_tracks || 0}</div><div class="stat-label">Unique tracks</div></div>
    </div>
    <div class="section-head" style="margin-top:18px"><h3>History</h3>
      <div class="filter-bar" style="margin:0">
        ${[7, 30, 90].map((d) => `<button class="btn btn-ghost hist-range" data-days="${d}">${d}d</button>`).join("")}
      </div>
    </div>
    <div id="history-view"><div class="empty-state" style="padding:20px">Loading history…</div></div>`;

  if (Object.keys(perAccount).length) {
    html += `<div class="section-head" style="margin-top:18px"><h3>Plays by account</h3></div>
      <div class="panel">
        ${Object.entries(perAccount).map(([slug, count]) => {
          const acc = (state.library?.accounts || []).find((a) => a.slug === slug);
          return `<div class="check-row"><strong>${esc(acc?.name || slug)}</strong><span style="margin-left:auto;color:var(--text-dim)">${count.toLocaleString()} play${count === 1 ? "" : "s"}</span></div>`;
        }).join("")}
      </div>`;
  }

  if (recent.length) {
    html += `<div class="section-head" style="margin-top:18px"><h3>Recent listens</h3></div>
      <div class="panel">
        ${recent.slice(0, 20).map((r) => {
          const t = (state.library?.tracks || []).find((x) => x.id === r.video_id && x.account === r.account);
          return `<div class="check-row"><span style="color:var(--text-faint);width:60px;font-size:12px">${new Date((r.ts || 0) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span><strong>${esc(t?.title || r.video_id)}</strong><span style="color:var(--text-dim);margin-left:auto">${esc(r.account)}</span></div>`;
        }).join("")}
      </div>`;
  }

  if (!(data.total_plays || 0) && !recent.length) {
    html += `<div class="empty-state" style="padding:24px"><div class="big">&#9654;</div>No listening data yet. Play some tracks — plays are counted once you reach 60% of a song.</div>`;
  }

  view.innerHTML = html;

  bindTrackTable(view, (state.library?.tracks || []).filter((t) => t.stream));

  const loadHistory = async (days) => {
    const box = $("#history-view");
    if (!box) return;
    const hseq = ++state._historySeq;
    box.innerHTML = `<div class="empty-state" style="padding:20px">Loading…</div>`;
    let h;
    try { h = await api(`/api/history?days=${days}`); } catch (e) {
      if (hseq === state._historySeq) box.innerHTML = `<div class="alert alert-error">${esc(e.message)}</div>`;
      return;
    }
    if (hseq !== state._historySeq) return;

    const perDay = h.per_day || {};
    const daysList = Object.entries(perDay);
    const maxDay = Math.max(1, ...daysList.map(([, v]) => v));
    const topTracks = h.top_tracks || [];
    const topArtists = h.top_artists || [];
    const topAlbums = h.top_albums || [];

    let hh = "";
    if (daysList.length) {
      hh += `<div class="panel"><div class="bar-chart">
        ${daysList.map(([day, count]) => `
          <div class="bar-col" title="${esc(day)}: ${count} play${count === 1 ? "" : "s"}">
            <div class="bar-fill" style="height:${Math.max(4, Math.round((count / maxDay) * 120))}px"></div>
            <div class="bar-label">${day.slice(5)}</div>
          </div>`).join("")}
      </div></div>`;
    }

    if (topTracks.length) {
      hh += `<div class="section-head" style="margin-top:18px"><h3>Most played tracks — last ${days} days</h3></div>
        <div class="panel"><table class="tracks"><thead><tr><th>#</th><th>Title</th><th>Plays</th></tr></thead><tbody>
          ${topTracks.map((t, i) => `
            <tr data-id="${esc(t.id)}" data-account="${esc(t.account)}">
              <td style="color:var(--text-faint)">${i + 1}</td>
              <td><div class="track-cell"><div class="mini-art" data-name="${esc(t.artist)}">
                ${t.art ? `<img src="${esc(t.art)}" loading="lazy" onerror="this.outerHTML=artPlaceholderFor(this)"/>` : artPlaceholder(t.artist)}
              </div><div class="t-main"><div class="t-title">${esc(t.title)}</div><div class="t-sub">${esc(t.artist)}</div></div></div></td>
              <td style="color:var(--text-dim)">${t.plays} play${t.plays === 1 ? "" : "s"}</td>
            </tr>`).join("")}
        </tbody></table></div>`;
    }

    if (topArtists.length) {
      hh += `<div class="section-head" style="margin-top:18px"><h3>Most played artists — last ${days} days</h3></div>
        <div class="panel">
          ${topArtists.map((a, i) => `<div class="check-row"><span style="color:var(--text-faint);width:26px">${i + 1}</span><strong>${esc(a.artist)}</strong><span style="margin-left:auto;color:var(--text-dim)">${a.plays} play${a.plays === 1 ? "" : "s"}</span></div>`).join("")}
        </div>`;
    }

    if (topAlbums.length) {
      hh += `<div class="section-head" style="margin-top:18px"><h3>Most played albums — last ${days} days</h3></div>
        <div class="panel">
          ${topAlbums.map((a, i) => `<div class="check-row"><span style="color:var(--text-faint);width:26px">${i + 1}</span><strong>${esc(a.album)}</strong><span style="margin-left:auto;color:var(--text-dim)">${a.plays} play${a.plays === 1 ? "" : "s"}</span></div>`).join("")}
        </div>`;
    }

    if (!daysList.length && !topTracks.length) {
      hh = `<div class="empty-state" style="padding:20px">No plays in the last ${days} days.</div>`;
    }

    box.innerHTML = hh;
    if (topTracks.length) bindTrackTable(box, topTracks.map((t) => ({ ...t, id: t.id, stream: true })));
  };

  state._historySeq = 0;
  $$(".hist-range", view).forEach((btn) => {
    btn.onclick = () => {
      $$(".hist-range", view).forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      loadHistory(Number(btn.dataset.days));
    };
  });
  loadHistory(30);
}

/* -------------------------------------------------------------------------
 * Search
 * ---------------------------------------------------------------------- */

function openSearch() {
  $("#search-overlay").classList.remove("hidden");
  $("#search-input").value = "";
  renderSearchResults("");
  $("#search-input").focus();
}

function closeSearch() {
  $("#search-overlay").classList.add("hidden");
}

function renderSearchResults(query) {
  const box = $("#search-results");
  const q = query.toLowerCase().trim();
  const tracks = state.library ? state.library.tracks : [];
  const agg = aggregates();

  if (!q) {
    box.innerHTML = `<div class="empty-state" style="padding:26px">Start typing to search songs, artists and albums.</div>`;
    return;
  }

  const songMatches = tracks.filter((t) =>
    (t.title + " " + t.artist + " " + (t.album || "")).toLowerCase().includes(q)).slice(0, 12);
  const artistMatches = agg.artists.filter((a) => a.name.toLowerCase().includes(q)).slice(0, 6);
  const albumMatches = agg.albums.filter((a) => a.title.toLowerCase().includes(q)).slice(0, 6);

  let html = "";
  if (songMatches.length) {
    html += `<div class="dup-title" style="padding:10px 18px 4px">Songs</div>` + songMatches.map((t) => `
      <div class="search-row" data-type="song" data-id="${esc(t.id)}" data-account="${esc(t.account)}">
        <div class="mini-art" data-name="${esc(t.artist)}">
          ${t.art ? `<img src="${esc(t.art)}" loading="lazy" onerror="this.outerHTML=artPlaceholderFor(this)"/>` : artPlaceholder(t.artist)}
        </div>
        <div class="s-type">Song</div>
        <div style="flex:1;min-width:0"><div class="s-title">${esc(t.title)}</div><div class="s-sub">${esc(t.artist)}</div></div>
      </div>`).join("");
  }
  if (artistMatches.length) {
    html += `<div class="dup-title" style="padding:10px 18px 4px">Artists</div>` + artistMatches.map((a) => `
      <a href="#/artist/${encodeURIComponent(a.name)}" class="search-row" style="text-decoration:none;color:inherit">
        <div class="mini-art">${a.art ? `<img src="${esc(a.art)}" loading="lazy"/>` : artPlaceholder(a.name)}</div>
        <div class="s-type">Artist</div>
        <div style="flex:1;min-width:0"><div class="s-title">${esc(a.name)}</div><div class="s-sub">${a.count} songs</div></div>
      </a>`).join("");
  }
  if (albumMatches.length) {
    html += `<div class="dup-title" style="padding:10px 18px 4px">Albums</div>` + albumMatches.map((a) => `
      <a href="#/album/${encodeURIComponent(a.title + "###" + a.artist)}" class="search-row" style="text-decoration:none;color:inherit">
        <div class="mini-art">${a.art ? `<img src="${esc(a.art)}" loading="lazy"/>` : artPlaceholder(a.title)}</div>
        <div class="s-type">Album</div>
        <div style="flex:1;min-width:0"><div class="s-title">${esc(a.title)}</div><div class="s-sub">${esc(a.artist)}</div></div>
      </a>`).join("");
  }

  const accounts = state.library ? state.library.accounts : [];
  html += `
    <div class="dup-title" style="padding:10px 18px 4px;display:flex;align-items:center;gap:8px">
      <span>YouTube Music</span>
      <select id="ytm-account" class="input" style="width:auto;margin-left:auto;font-size:12px;padding:4px 8px;height:auto">
        ${state.scope !== "all"
          ? `<option value="${esc(state.scope)}">${esc(selectedAccountName())}</option>`
          : `<option value="">All accounts</option>${accounts.map((a) => `<option value="${esc(a.slug)}">${esc(a.name)}</option>`).join("")}`}
      </select>
    </div>
    <div id="ytm-results"><div class="empty-state" style="padding:16px;font-size:13px">Searching YouTube Music…</div></div>`;

  box.innerHTML = html || `<div class="empty-state" style="padding:26px">No matches for “${esc(q)}”.</div>`;

  $$(".search-row[data-type=song]", box).forEach((row) => {
    row.onclick = () => {
      closeSearch();
      const all = tracks.filter((t) => t.stream);
      const idx = all.findIndex((t) => t.id === row.dataset.id && t.account === row.dataset.account);
      if (idx >= 0) setQueue(all, idx);
    };
  });

  box.onclick = (e) => {
    if (e.target.closest("a")) closeSearch();
  };

  populateYTMSearch(q);
}

let _ytmSearchSeq = 0;
async function populateYTMSearch(query) {
  const box = $("#ytm-results");
  if (!box) return;
  const seq = ++_ytmSearchSeq;
  const results = [];
  const accountNames = {};
  (state.library?.accounts || []).forEach((a) => { accountNames[a.slug] = a.name; });
  try {
    const sel = $("#ytm-account");
    const allSlugs = (state.library?.accounts || []).map((a) => a.slug);
    let accountsParam = "";
    if (sel) {
      const v = sel.value;
      if (v) accountsParam = encodeURIComponent(v);
      else if (state.scope !== "all" && state.scope) accountsParam = encodeURIComponent(state.scope);
      else if (allSlugs.length) accountsParam = encodeURIComponent(allSlugs.join(","));
    }
    const data = await api(`/api/search?q=${encodeURIComponent(query)}&limit=8${accountsParam ? `&accounts=${accountsParam}` : ""}`);
    if (Array.isArray(data)) results.push(...data);
    else if (Array.isArray(data.results)) results.push(...data.results);
    else if (Array.isArray(data.tracks)) results.push(...data.tracks);
  } catch (e) {
    if (seq === _ytmSearchSeq) box.innerHTML = `<div class="empty-state" style="padding:16px;font-size:13px">Search failed: ${esc(e.message)}</div>`;
    return;
  }
  if (seq !== _ytmSearchSeq) return;

  if (!results.length) {
    box.innerHTML = `<div class="empty-state" style="padding:16px;font-size:13px">No YouTube Music results.</div>`;
    return;
  }

  const rows = results.map((r) => {
    const title = r.title || "Untitled";
    const artist = (r.artists || []).map((a) => a.name || a).join(", ") || "Unknown Artist";
    const album = r.album ? (r.album.name || r.album) : null;
    const dur = r.duration ? fmtDuration(r.duration) : "";
    const accs = Array.isArray(r.accounts) ? r.accounts : [];
    const owned = Array.isArray(r.owned_by) ? r.owned_by : [];
    const ownedNote = owned.length
      ? ` <span style="color:var(--accent)">&#10003; ${owned.map((s) => accountNames[s] || s).join(", ")}</span>`
      : "";
    return `
      <div class="search-row ytm-row" data-vid="${esc(r.videoId)}">
        <div class="mini-art">${artPlaceholder(title)}</div>
        <div class="s-type">YTM</div>
        <div style="flex:1;min-width:0"><div class="s-title">${esc(title)}</div><div class="s-sub">${esc(artist)}${album ? ` — ${esc(album)}` : ""}${dur ? ` · ${dur}` : ""}${ownedNote}</div></div>
        <button class="btn btn-ghost ytm-dl" style="padding:6px 12px;font-size:12px" data-vid="${esc(r.videoId)}" data-title="${esc(title)}" data-artist="${esc(artist)}" data-album="${esc(album || "")}">&#8681;</button>
      </div>`;
  }).join("");

  box.innerHTML = `<div class="ytm-bar"><button id="ytm-dl-all" class="btn btn-primary" style="padding:6px 14px;font-size:12.5px">&#8681; Download ${results.length} result${results.length === 1 ? "" : "s"}</button></div>` + rows;

  const accountFor = () => $("#ytm-account").value || null;

  $("#ytm-dl-all").onclick = () => {
    const tracks = results.map((r) => ({
      videoId: r.videoId,
      title: r.title || "",
      artists: (r.artists || []).map((a) => ({ name: a.name || a })),
      album: r.album ? (r.album.name || r.album) : null,
    }));
    downloadSearchTracks(tracks, accountFor());
  };

  $$(".ytm-dl", box).forEach((btn) => {
    btn.onclick = () => {
      const t = results.find((r) => r.videoId === btn.dataset.vid);
      if (!t) return;
      const targets = Array.isArray(t.accounts) && t.accounts.length ? t.accounts : null;
      downloadSearchTracks([{
        videoId: t.videoId,
        title: t.title || "",
        artists: (t.artists || []).map((a) => ({ name: a.name || a })),
        album: t.album ? (t.album.name || t.album) : null,
      }], accountFor(), targets);
    };
  });
}

async function downloadSearchTracks(tracks, accountSlug, preferredAccounts = null) {
  if (!tracks.length) return;
  const accounts = state.library ? state.library.accounts : [];
  let target = accountSlug || null;

  if (!target && preferredAccounts && preferredAccounts.length) {
    const usable = preferredAccounts.filter((s) => accounts.some((a) => a.slug === s));
    if (usable.length === 1) target = usable[0];
  }

  if (!target) {
    if (!accounts.length) { toast("Add an account first", "error"); return; }
    const picked = await modal("Download to account", `
      <div class="label">Account</div>
      <select name="account" class="input">${accounts.map((a) => `<option value="${esc(a.slug)}">${esc(a.name)}</option>`).join("")}</select>`,
      [
        { label: "Cancel", value: () => false },
        { label: "Continue", variant: "btn-primary", value: () => $("#modal-root select[name=account]").value },
      ]);
    if (!picked) return;
    target = picked;
  }
  const config = await downloadDialog({ account: target, title: `Download ${tracks.length} search result${tracks.length === 1 ? "" : "s"}` });
  if (!config) return;
  try {
    await startDownload({
      account: target,
      quality: config.quality,
      existing_policy: config.existing_policy,
      session: config.session,
      output: "search",
      tracks,
    });
    toast(`Downloading ${tracks.length} search result${tracks.length === 1 ? "" : "s"}…`, "info");
  } catch (e) { toast(e.message, "error"); }
}

/* -------------------------------------------------------------------------
 * Router
 * ---------------------------------------------------------------------- */

const routes = {
  home: renderHome,
  songs: renderSongs,
  artists: renderArtists,
  albums: renderAlbums,
  years: renderYears,
  genres: renderGenres,
  "most-played": renderMostPlayed,
  duplicates: renderDuplicates,
  status: renderStatus,
  downloads: renderDownloads,
  accounts: renderAccounts,
  settings: renderSettings,
  playlists: renderPlaylists,
  stats: renderStats,
};

async function navigate() {
  ++state._renderSeq;
  const hash = location.hash.replace(/^#\/?/, "");
  const [route, ...rest] = hash.split("/");
  const key = route || "home";
  state.route = key;

  const navItem = $(`#main-nav .nav-item[data-route="${key}"], #browse-nav .nav-item[data-route="${key}"], #tools-nav .nav-item[data-route="${key}"], #system-nav .nav-item[data-route="${key}"]`);
  $$(".nav-item").forEach((n) => n.classList.remove("active"));
  if (navItem) navItem.classList.add("active");

  const titles = {
    home: "Home", songs: "All Songs", artists: "Artists", albums: "Albums",
    years: "Years", genres: "Genres", "most-played": "Most Played",
    duplicates: "Duplicates", status: "Library Status", downloads: "Downloads",
    accounts: "Accounts", settings: "Settings", playlists: "Playlists", stats: "Stats",
  };
  topbarTitle.textContent = titles[key] || "Home";

  view.innerHTML = spinner();

  try {
    if (key === "artist" && rest[0]) return renderArtist(decodeURIComponent(rest[0]));
    if (key === "album" && rest[0]) return renderAlbum(decodeURIComponent(rest[0]));
    if (key === "year" && rest[0]) return renderYear(Number(rest[0]));
    if (key === "genre" && rest[0]) return renderGenre(decodeURIComponent(rest[0]));
    if (key === "playlist" && rest.length) {
      if (rest[0] === "ytm" && rest.length >= 3) return renderPlaylistDetail(null, decodeURIComponent(rest[1]), decodeURIComponent(rest[2]));
      return renderPlaylistDetail(decodeURIComponent(rest[0]));
    }
    const fn = routes[key];
    if (fn) {
      await fn();
      return;
    }
    return renderHome();
  } catch (e) {
    view.innerHTML = `<div class="alert alert-error">${esc(e.message)}</div>`;
  }
}

/* -------------------------------------------------------------------------
 * Sidebar / accounts nav
 * ---------------------------------------------------------------------- */

function renderAccountsNav() {
  const box = $("#accounts-nav");
  const accounts = state.library ? state.library.accounts : [];
  box.innerHTML = "";

  const chip = (slug, name) => {
    const node = document.createElement("div");
    node.className = `account-chip${state.scope === slug ? " active" : ""}`;
    node.innerHTML = `
      <span class="account-avatar" style="background:${paletteFor(name)};color:#000">${esc(initialsOf(name))}</span>
      <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(name)}</span>
      ${state.refreshing[slug] ? '<span style="font-size:11px;color:var(--accent)">…</span>' : ""}`;
    node.onclick = () => { state.scope = slug; renderAccountsNav(); navigate(); };
    box.appendChild(node);
  };

  const all = document.createElement("div");
  all.className = `account-chip${state.scope === "all" ? " active" : ""}`;
  all.innerHTML = `<span class="account-avatar" style="background:linear-gradient(135deg,#6a5acd,#00bfff);color:#000">ALL</span><span>All accounts</span>`;
  all.onclick = () => { state.scope = "all"; renderAccountsNav(); navigate(); };
  box.appendChild(all);

  accounts.forEach((a) => chip(a.slug, a.name));
}

function renderDownloadsBadge() {
  const count = Object.keys(state.downloads).length;
  const badge = $("#active-downloads-badge");
  if (count) { badge.textContent = count; badge.classList.remove("hidden"); }
  else badge.classList.add("hidden");
}

function renderRefreshUI() {
  renderAccountsNav();
}

/* -------------------------------------------------------------------------
 * SSE + polling
 * ---------------------------------------------------------------------- */

function connectEvents() {
  const url = state.token ? `/api/events?token=${encodeURIComponent(state.token)}` : "/api/events";
  const source = new EventSource(url);
  let pollTimer = null;

  const stopPolling = () => {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  };

  const startPolling = () => {
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      try {
        const snapshot = await api("/api/downloads");
        state.downloads = {};
        (snapshot || []).forEach((s) => { state.downloads[s.slug] = s; });
        renderDownloadsBadge();
        if (state.route === "downloads") renderDownloads();
      } catch (e) { /* keep polling */ }
    }, 5000);
  };

  source.onmessage = (event) => {
    stopPolling();
    let data;
    try { data = JSON.parse(event.data); } catch (e) { return; }

    if (data.type === "hello") {
      const dl = data.downloads || [];
      state.downloads = {};
      dl.forEach((s) => { state.downloads[s.slug] = s; });
      (data.refreshing || []).forEach((slug) => { state.refreshing[slug] = true; });
      renderDownloadsBadge();
      renderAccountsNav();
      return;
    }

    if (data.type === "play_recorded") {
      if (state.library) {
        const hit = state.library.tracks.find((t) => t.id === data.video_id && t.account === data.slug);
        if (hit && typeof data.total === "number") hit.plays = data.total;
      }
      if (state.route === "stats" || state.route === "home") navigate();
      return;
    }

    if (data.type === "session_update") {
      const s = data.session;
      state.downloads[s.slug] = s;
      renderDownloadsBadge();
      if (state.route === "downloads") renderDownloads();
      return;
    }

    if (data.type === "session_finished") {
      const slug = data.slug;
      const sid = data.session_id;
      setTimeout(() => {
        const cur = state.downloads[slug];
        if (cur && cur.session_id === sid) delete state.downloads[slug];
        renderDownloadsBadge();
        if (state.route === "downloads") renderDownloads();
      }, 8000);
      const detail = `${data.downloaded} downloaded, ${data.failed} failed, ${data.skipped} skipped`;
      toast(`Download finished — ${detail}`, data.failed ? "info" : "success");
      notify(data.name ? `Download finished: ${data.name}` : "Download finished", detail);
      loadLibrary().then(() => { if (["songs", "home", "playlists", "stats", "duplicates", "status"].includes(state.route)) navigate(); });
      return;
    }

    if (data.type === "refresh_finished") {
      delete state.refreshing[data.slug];
      renderAccountsNav();
      toast(`${data.slug} refreshed (${data.liked_count ?? "?"} liked tracks)`, "success");
      notify(`${data.slug} refreshed`, `${data.liked_count ?? "?"} liked tracks`);
      loadLibrary().then(() => { if (["home", "songs", "playlists", "stats", "status"].includes(state.route)) navigate(); });
      return;
    }

    if (data.type === "refresh_failed") {
      delete state.refreshing[data.slug];
      renderAccountsNav();
      toast(`${data.slug}: refresh failed — ${data.error}`, "error");
      notify(`${data.slug}: refresh failed`, data.error);
      return;
    }

    if (data.type === "auto_download_started") {
      toast(`Auto-download started for ${data.slug} (${data.count} tracks)`, "info");
      return;
    }
    if (data.type === "premium_retry_started") {
      toast(`Premium retry started for ${data.slug} (${data.count} tracks)`, "info");
      return;
    }
    if (data.type === "backup_written") {
      toast(`Backup written to ${data.file}`, "info");
      return;
    }
    if (data.type === "ytdlp_update_check") {
      if (data.update_available) toast(`yt-dlp update available: ${data.current} → ${data.latest}`, "info");
      return;
    }
    if (data.type === "scrobble_flush") {
      if (data.sent) toast(`Scrobbled ${data.sent} track${data.sent === 1 ? "" : "s"} to last.fm`, "success");
      if (data.error) toast(`last.fm scrobble error: ${data.error}`, "error");
    }
  };
  source.onerror = () => {
    if (source.readyState !== EventSource.CLOSED) return;
    stopPolling();
    startPolling();
  };
}

/* -------------------------------------------------------------------------
 * Player bar wiring
 * ---------------------------------------------------------------------- */

function wirePlayer() {
  $("#btn-play").onclick = togglePlay;
  $("#btn-next").onclick = next;
  $("#btn-prev").onclick = prev;
  $("#btn-shuffle").onclick = () => { state.player.shuffle = !state.player.shuffle; updatePlayerUI(); persistPlayer(); };
  $("#btn-repeat").onclick = () => {
    state.player.repeat = state.player.repeat === "off" ? "all" : state.player.repeat === "all" ? "one" : "off";
    updatePlayerUI();
    persistPlayer();
  };
  $("#btn-sleep").onclick = sleepTimerDialog;
  $("#btn-queue").onclick = toggleQueueDrawer;
  $("#btn-queue-close").onclick = toggleQueueDrawer;

  const seek = $("#seek");
  seek.addEventListener("pointerdown", () => { seek.dataset.seeking = "1"; });
  seek.addEventListener("pointerup", () => { seek.dataset.seeking = "0"; });
  seek.addEventListener("pointerleave", () => { seek.dataset.seeking = "0"; });
  seek.addEventListener("input", () => {
    if (!audio.duration) return;
    audio.currentTime = (Number(seek.value) / 1000) * audio.duration;
    $("#cur-time").textContent = fmtTime(audio.currentTime);
  });

  const vol = $("#volume");
  vol.addEventListener("input", () => {
    const v = Number(vol.value) / 100;
    state.player.volume = v;
    state.player.muted = v === 0;
    localStorage.setItem("ls-volume", String(v));
    localStorage.setItem("ls-muted", state.player.muted ? "1" : "0");
    audio.volume = state.player.muted ? 0 : v;
  });

  $("#btn-mute").onclick = () => {
    state.player.muted = !state.player.muted;
    if (state.player.muted) {
      audio.volume = 0;
      if (state.player.volume > 0) state.player.lastVol = state.player.volume;
    } else {
      state.player.volume = state.player.volume > 0 ? state.player.volume : (state.player.lastVol || 0.8);
      audio.volume = state.player.volume;
      vol.value = String(Math.round(state.player.volume * 100));
    }
    localStorage.setItem("ls-muted", state.player.muted ? "1" : "0");
    updatePlayerUI();
  };
}

/* -------------------------------------------------------------------------
 * Global keyboard shortcuts (mirroring the TUI bindings)
 * ---------------------------------------------------------------------- */

function isTyping() {
  const el = document.activeElement;
  return el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);
}

document.addEventListener("keydown", (e) => {
  const searchOpen = !$("#search-overlay").classList.contains("hidden");

  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (searchOpen) closeSearch(); else openSearch();
    return;
  }
  if (e.key === "Escape") {
    if (searchOpen) { closeSearch(); return; }
    const root = $("#modal-root");
    if (!root.classList.contains("hidden")) { root.classList.add("hidden"); root.innerHTML = ""; return; }
  }
  if (searchOpen) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); return; }
    return;
  }
  if (isTyping()) return;

  if (e.key === " " && e.target.tagName !== "BUTTON") {
    e.preventDefault();
    togglePlay();
  } else if (e.key === "ArrowRight") {
    audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 5);
  } else if (e.key === "ArrowLeft") {
    audio.currentTime = Math.max(0, audio.currentTime - 5);
  } else if (e.key.toLowerCase() === "r") {
    refreshLibrary(state.scope);
  } else if (e.key.toLowerCase() === "d") {
    if (state.scope !== "all") {
      downloadDialog({ account: state.scope }).then((c) => c && startDownload(c));
    }
  } else if (e.key.toLowerCase() === "m") {
    e.preventDefault();
    $("#btn-mute").click();
  } else if (e.key === "M") {
    location.hash = "#/most-played";
  } else if (e.key.toLowerCase() === "s") {
    location.hash = "#/status";
  } else if (e.key.toLowerCase() === "u") {
    location.hash = "#/duplicates";
  } else if (e.key.toLowerCase() === "a") {
    location.hash = "#/accounts";
  }
});

/* -------------------------------------------------------------------------
 * Init
 * ---------------------------------------------------------------------- */

function wireTopbar() {
  $("#btn-back").onclick = () => history.back();
  $("#btn-search").onclick = openSearch;
  $("#btn-refresh").onclick = () => refreshLibrary(state.scope);
  $("#search-input").oninput = (e) => renderSearchResults(e.target.value);
  $("#search-input").onkeydown = (e) => {
    if (e.key === "Enter") {
      const first = $(".search-row[data-type=song]");
      if (first) first.click();
    }
  };
  window.addEventListener("hashchange", navigate);
}

async function init() {
  applyAccent(localStorage.getItem("ls-accent") || "#1db954");
  wirePlayer();
  wireTopbar();
  connectEvents();
  try {
    await loadLibrary();
  } catch (e) {
    $("#view").innerHTML = `<div class="alert alert-error">Failed to load library: ${esc(e.message)}</div>`;
  }
  restorePlayer();
  navigate();
}

document.addEventListener("DOMContentLoaded", init);
