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
    volume: 0.8,
    audio: new Audio(),
    lastVol: 0.8,
  },
  token: localStorage.getItem("ls-token") || "",
  _tokenPrompting: false,
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
    const close = (value) => { root.classList.add("hidden"); root.innerHTML = ""; resolve(value); };
    $(".modal-bg", root).onclick = () => close(null);
    $$("button[data-a]", root).forEach((btn) => {
      btn.onclick = () => {
        const action = actions[Number(btn.dataset.a)];
        const value = action.value ? action.value() : true;
        if (value !== false) close(value);
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
  state.refreshing = {};
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

function setQueue(tracks, startIndex = 0) {
  state.player.queue = tracks.filter((t) => t.stream);
  state.player.index = -1;
  if (state.player.queue.length) playIndex(startIndex);
}

function playIndex(index) {
  if (index < 0 || index >= state.player.queue.length) return;
  state.player.index = index;
  const track = state.player.queue[index];
  audio.src = streamUrl(track);
  audio.volume = state.player.volume;
  audio.play().then(() => { state.player.playing = true; }).catch(() => {});
  updatePlayerUI();
}

function togglePlay() {
  if (state.player.index < 0) {
    const first = visibleTracks().find((t) => t.stream);
    if (first) setQueue(visibleTracks(), visibleTracks().indexOf(first));
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
  if (state.player.shuffle && q.length > 1) {
    let i;
    do { i = Math.floor(Math.random() * q.length); } while (i === state.player.index);
    return i;
  }
  return (state.player.index + 1) % q.length;
}

function prevIndex() {
  const q = state.player.queue;
  if (!q.length) return -1;
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
  if (state.player.repeat === "one") {
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
  $("#seek").value = String(Math.round(frac));
  $("#cur-time").textContent = fmtTime(audio.currentTime);
  $("#dur-time").textContent = fmtTime(audio.duration);
});

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
  $$("table.tracks tbody tr").forEach((row) => {
    row.classList.toggle("playing", track && row.dataset.id === track.id && state.scopeMatches && row.dataset.account === (track.account || ""));
  });
  markPlayingRows(track);
}

function markPlayingRows(track) {
  $$("table.tracks tbody tr").forEach((row) => {
    row.classList.toggle("playing", !!track && row.dataset.id === track.id && row.dataset.account === track.account);
  });
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
  const snapshot = await api("/api/downloads", { method: "POST", body });
  state.downloads[config.account] = snapshot;
  renderDownloadsBadge();
  return snapshot;
}

async function downloadDialog({ account, title = "Start download", bodyHTML = "" } = {}) {
  const value = await modal(title, `
    <div class="label">Audio quality</div>
    <select name="quality" class="input">
      ${QUALITIES.map(([v, l]) => `<option value="${v}" ${v === "mp3_320" ? "selected" : ""}>${esc(l)}</option>`).join("")}
    </select>
    <div class="label">If audio already exists</div>
    <select name="policy" class="input">
      <option value="skip">Skip existing file</option>
      <option value="replace">Replace existing file</option>
    </select>
    <div class="label">Session</div>
    <select name="session" class="input">
      <option value="once">Run the whole list once</option>
      <option value="loop">Loop until stopped</option>
      <option value="skip">Download missing across all qualities</option>
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
        <span class="q-badge"><span class="state-dot ${esc(t.status)}"></span>${esc(t.quality || "Not downloaded")}</span>
      </td>
    </tr>`).join("");
}

function trackTable(tracks, opts = {}) {
  if (!tracks.length) return empty(opts.emptyText || "No songs in this view yet.");
  const showAccount = state.scope === "all";
  const head = `
    <th data-col="index">#</th>
    <th data-col="title">Title</th>
    <th data-col="album">Album</th>
    ${showAccount ? '<th data-col="account">Account</th>' : ""}
    <th data-col="year">Year</th>
    <th data-col="duration">Duration</th>
    <th data-col="plays">Plays</th>
    <th data-col="quality">Quality</th>`;

  return `
    <div class="filter-bar">
      <input id="track-filter" class="input" type="text" placeholder="Filter by title, artist, album…" />
      <button id="download-view" class="btn btn-primary">&#8681; Download</button>
    </div>
    <div style="overflow-x:auto">
      <table class="tracks">
        <thead><tr>${head}</tr></thead>
        <tbody>${trackRowsHTML(tracks)}</tbody>
      </table>
    </div>`;
}

function bindTrackTable(container, allTracks) {
  const table = $("table.tracks", container);
  if (table) {
    $$("tbody tr", table).forEach((row) => {
      row.onclick = (e) => {
        if (e.target.closest("a")) return;
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

  const downloadBtn = $("#download-view", container);
  if (downloadBtn) {
    downloadBtn.onclick = async () => {
      if (state.scope === "all") { toast("Select a single account to download", "info"); return; }
      const config = await downloadDialog({ account: state.scope, title: "Download liked music" });
      if (config) { try { await startDownload(config); navigate(); } catch (e) { toast(e.message, "error"); } }
    };
  }
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

  view.innerHTML = `
    <div class="hero">
      <div class="p-type">${esc(selectedAccountName())}</div>
      <h1>Welcome to your library</h1>
      <p>Your downloaded music, organized like a streaming service — search, browse by artist, album, year or genre, and play anything instantly. Everything stays on this machine.</p>
      <div class="hero-actions">
        <button id="hero-refresh" class="btn btn-ghost">&#8635; Refresh library</button>
        ${state.scope !== "all" ? '<button id="hero-download" class="btn btn-primary">&#8681; Download liked music</button>' : ""}
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
    const config = await downloadDialog({ account: state.scope, title: "Download liked music" });
    if (config) { try { await startDownload(config); } catch (e) { toast(e.message, "error"); } }
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
      title: a.title, art: a.art,
      sub: `${a.artist} · ${a.year || "—"} · ${a.count} song${a.count === 1 ? "" : "s"}`,
    })), (a) => `#/album/${encodeURIComponent(a.title + "###" + a.artist)}`) : empty("No albums yet.")}`;
}

async function renderYears() {
  const { years } = aggregates();
  view.innerHTML = `
    <div class="section-head"><h2>Years &amp; decades</h2></div>
    ${years.length ? cardGridHTML(years.map((y) => ({
      name: String(y.year),
      art: null,
      sub: `${y.count} song${y.count === 1 ? "" : "s"}`,
    })), (y) => `#/year/${y.year}`) : empty("No year data yet — downloaded files carry year tags.")}`;
}

async function renderGenres() {
  const { genres } = aggregates();
  view.innerHTML = `
    <div class="section-head"><h2>Genres</h2></div>
    ${genres.length ? cardGridHTML(genres.map((g) => ({
      name: g.genre,
      art: null,
      sub: `${g.count} song${g.count === 1 ? "" : "s"}`,
    })), (g) => `#/genre/${encodeURIComponent(g.genre)}`) : empty("No genre data yet — downloaded files carry genre tags.")}`;
}

async function renderArtist(name) {
  const tracks = tracksForArtist(name);
  const plays = tracks.reduce((s, t) => s + (t.plays || 0), 0);
  const art = tracks.find((t) => t.art);
  const albums = [...new Set(tracks.map((t) => t.album).filter(Boolean))];
  const { artists } = aggregates();
  const exact = artists.find((a) => a.name === name);

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
          <button id="artist-download" class="btn">&#8681; Download all</button>
        </div>
      </div>
    </div>
    <div class="section-head"><h2>Songs</h2></div>
    ${trackTable(tracks, { emptyText: "No downloaded songs for this artist yet." })}`;

  $("#artist-play").onclick = () => setQueue(tracks.filter((t) => t.stream), 0);
  $("#artist-download").onclick = async () => {
    if (state.scope === "all") { toast("Select a single account first", "info"); return; }
    const config = await downloadDialog({ account: state.scope, title: `Download ${name}` });
    if (config) {
      config.video_ids = tracks.map((t) => t.id);
      try { await startDownload(config); } catch (e) { toast(e.message, "error"); }
    }
  };
  bindTrackTable(view, tracks);
}

async function renderAlbum(key) {
  const [albumTitle, artist] = key.split("###");
  const tracks = tracksForAlbum(key);
  const art = tracks.find((t) => t.art);

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
          <button id="album-download" class="btn">&#8681; Download album</button>
        </div>
      </div>
    </div>
    ${trackTable(tracks, { emptyText: "No downloaded songs in this album yet." })}`;

  $("#album-play").onclick = () => setQueue(tracks.filter((t) => t.stream), 0);
  $("#album-download").onclick = async () => {
    if (state.scope === "all") { toast("Select a single account first", "info"); return; }
    const config = await downloadDialog({ account: state.scope, title: `Download ${albumTitle}` });
    if (config) {
      config.video_ids = tracks.map((t) => t.id);
      try { await startDownload(config); } catch (e) { toast(e.message, "error"); }
    }
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
  const data = await api(`/api/most-played/${state.scope}`);
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
          ${QUALITIES.map(([v, l]) => `<option value="${v}" ${v === "mp3_320" ? "selected" : ""}>${esc(l)}</option>`).join("")}
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
}

async function renderDuplicates() {
  view.innerHTML = spinner();
  try {
    const data = await api("/api/duplicates");
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

    view.innerHTML = html;

    const update = () => {
      const checked = $$(".dup-cb:checked");
      $("#dup-remove").disabled = checked.length === 0;
    };
    $$(".dup-cb").forEach((cb) => { cb.onchange = update; });

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
  } catch (e) {
    view.innerHTML = `<div class="alert alert-error">${esc(e.message)}</div>`;
  }
}

async function renderStatus() {
  view.innerHTML = spinner();
  const targets = state.scope === "all" && state.library
    ? state.library.accounts.map((a) => a.slug)
    : [state.scope];

  let html = `<div class="section-head"><h2>Library status — ${esc(selectedAccountName())}</h2></div>`;

  for (const slug of targets) {
    let status = {};
    try { status = await api(`/api/status/${slug}`); } catch (e) {}
    const counts = status.counts || {};

    html += `
      <div class="panel">
        <h3>${esc(slug)} <span style="color:var(--text-faint);font-weight:600;font-size:12px">${status.quality || ""} · ${status.timestamp ? new Date(status.timestamp * 1000).toLocaleString() : "no snapshot yet"}</span></h3>
        <div class="stat-grid" style="margin:6px 0 0">
          ${[["liked", "Liked"], ["downloaded", "Downloaded"], ["not_downloaded", "Not downloaded"], ["missing", "Missing files"], ["no_longer_liked", "No longer liked"], ["failed", "Failed"], ["skipped", "Skipped"]].map(([k, label]) => `
            <div class="stat-card" style="padding:14px 16px">
              <div class="stat-value" style="font-size:20px">${counts[k] ?? 0}</div>
              <div class="stat-label">${label}</div>
            </div>`).join("")}
        </div>
      </div>`;

    for (const [key, label, icon] of [
      ["missing", "MISSING FILES", "&#9888;"],
      ["not_downloaded", "NOT DOWNLOADED", "&#128718;"],
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
}

async function renderDownloads() {
  const sessions = Object.values(state.downloads);
  view.innerHTML = `
    <div class="section-head"><h2>Downloads</h2></div>
    ${sessions.length ? "" : `<div class="empty-state"><div class="big">&#8681;</div>No active download sessions. Start one from any song list or the download buttons.</div>`}`;

  sessions.forEach((s) => {
    const counts = s.counts || {};
    const node = document.createElement("div");
    node.className = "session-card";
    node.id = `session-${s.slug}`;
    node.innerHTML = `
      <div class="session-head">
        <div>
          <div class="session-title">${esc(s.name)} <span class="slug">${s.quality || ""}</span></div>
          <div class="session-meta">
            ${s.track ? `${s.track.number}/${s.track.total} — ${esc(s.track.title)}` : "Starting…"}
          </div>
        </div>
        <div class="session-title">${Math.round(s.progress)}%</div>
      </div>
      <div class="progress-track"><div class="progress-fill" style="width:${Math.max(0, Math.min(100, s.progress))}%"></div></div>
      <div class="session-footer">
        <span>D:${counts.downloaded || 0} S:${counts.skipped || 0} F:${counts.failed || 0} U:${counts.unavailable || 0}</span>
        <span>
          <button class="btn btn-ghost" style="padding:6px 14px;font-size:12px" data-act="stop" data-slug="${esc(s.slug)}">Stop after stage</button>
          <button class="btn btn-danger" style="padding:6px 14px;font-size:12px" data-act="cancel" data-slug="${esc(s.slug)}">Cancel</button>
        </span>
      </div>`;
    view.appendChild(node);
  });

  $$("[data-act]", view).forEach((btn) => {
    btn.onclick = async () => {
      const slug = btn.dataset.slug;
      const act = btn.dataset.act;
      try {
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
      <div class="label">API token</div>
      <div class="label">Token (only if the server was started with --token)</div>
      <div class="filter-bar">
        <input id="set-token" class="input" type="password" placeholder="Bearer token" value="${esc(state.token)}" autocomplete="off" />
        <button id="set-token-save" class="btn">Save token</button>
        <button id="set-token-clear" class="btn btn-ghost">Clear</button>
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
    };
    try {
      await api("/api/settings", { method: "POST", body });
      toast("Settings saved", "success");
    } catch (e) { toast(e.message, "error"); }
  };

  $("#set-backup").onclick = async () => {
    try {
      const r = await api("/api/backup", { method: "POST", body: {} });
      toast(`Backup written to ${r.file}`, "info");
    } catch (e) { toast(e.message, "error"); }
  };
}

function applyAccent(color) {
  localStorage.setItem("ls-accent", color);
  document.documentElement.style.setProperty("--accent", color);
  document.documentElement.style.setProperty("--accent-hover", color);
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

  box.innerHTML = html || `<div class="empty-state" style="padding:26px">No matches for “${esc(q)}”.</div>`;

  $$(".search-row[data-type=song]", box).forEach((row) => {
    row.onclick = () => {
      closeSearch();
      const all = tracks.filter((t) => t.stream);
      const idx = all.findIndex((t) => t.id === row.dataset.id && t.account === row.dataset.account);
      if (idx >= 0) setQueue(all, idx);
    };
  });
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
};

async function navigate() {
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
    accounts: "Accounts", settings: "Settings",
  };
  topbarTitle.textContent = titles[key] || "Home";

  view.innerHTML = spinner();

  try {
    if (key === "artist" && rest[0]) return renderArtist(decodeURIComponent(rest[0]));
    if (key === "album" && rest[0]) return renderAlbum(decodeURIComponent(rest[0]));
    if (key === "year" && rest[0]) return renderYear(Number(rest[0]));
    if (key === "genre" && rest[0]) return renderGenre(decodeURIComponent(rest[0]));
    const fn = routes[key];
    if (fn) return fn();
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
  const source = new EventSource("/api/events");
  source.onmessage = (event) => {
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

    if (data.type === "session_update") {
      const s = data.session;
      state.downloads[s.slug] = s;
      renderDownloadsBadge();
      if (state.route === "downloads") renderDownloads();
      return;
    }

    if (data.type === "session_finished") {
      const slug = data.slug;
      setTimeout(() => {
        delete state.downloads[slug];
        renderDownloadsBadge();
        if (state.route === "downloads") renderDownloads();
      }, 8000);
      const detail = `${data.downloaded} downloaded, ${data.failed} failed, ${data.skipped} skipped`;
      toast(`Download finished — ${detail}`, data.failed ? "info" : "success");
      loadLibrary().then(() => { if (state.route === "songs" || state.route === "home") navigate(); });
      return;
    }

    if (data.type === "refresh_finished") {
      delete state.refreshing[data.slug];
      renderAccountsNav();
      toast(`${data.slug} refreshed (${data.liked_count ?? "?"} liked tracks)`, "success");
      loadLibrary().then(() => { if (state.route === "home" || state.route === "songs") navigate(); });
      return;
    }

    if (data.type === "refresh_failed") {
      delete state.refreshing[data.slug];
      renderAccountsNav();
      toast(`${data.slug}: refresh failed — ${data.error}`, "error");
    }
  };
  source.onerror = () => { /* auto-reconnect by EventSource */ };
}

/* -------------------------------------------------------------------------
 * Player bar wiring
 * ---------------------------------------------------------------------- */

function wirePlayer() {
  $("#btn-play").onclick = togglePlay;
  $("#btn-next").onclick = next;
  $("#btn-prev").onclick = prev;
  $("#btn-shuffle").onclick = () => { state.player.shuffle = !state.player.shuffle; updatePlayerUI(); };
  $("#btn-repeat").onclick = () => {
    state.player.repeat = state.player.repeat === "off" ? "all" : state.player.repeat === "all" ? "one" : "off";
    updatePlayerUI();
  };

  const seek = $("#seek");
  let seeking = false;
  seek.addEventListener("pointerdown", () => { seeking = true; });
  seek.addEventListener("pointerup", () => { seeking = false; });
  seek.addEventListener("input", () => {
    if (!audio.duration) return;
    audio.currentTime = (Number(seek.value) / 1000) * audio.duration;
    $("#cur-time").textContent = fmtTime(audio.currentTime);
  });

  const vol = $("#volume");
  vol.addEventListener("input", () => {
    const v = Number(vol.value) / 100;
    state.player.volume = v;
    audio.volume = v;
  });
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
  navigate();
}

document.addEventListener("DOMContentLoaded", init);
