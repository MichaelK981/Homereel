// Any request can come back 401 for two different reasons: the session died
// (bounce to /login), or the session is fine but no profile has been picked
// yet (bounce to /profiles instead — see current_profile() in main.py, which
// sets this header specifically so the two cases don't look the same).
async function api(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    window.location.href = res.headers.get("X-Auth-Reason") === "profile" ? "/profiles" : "/login";
    throw new Error("not signed in");
  }
  return res;
}

const collectionsGrid = document.getElementById("collectionsGrid");
const collectionGrid = document.getElementById("collectionGrid");

// Libraries are mapped in Settings, so every tab, panel and grid below is built
// at runtime. A library's tab and panel are both keyed "lib-<name>".
const tabId = (library) => `lib-${library}`;

// Icons for the three built-ins; anything mapped later gets the generic one.
const LIBRARY_ICONS = {
  movies: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 4v16M16 4v16M3 12h18M3 8h5M3 16h5M16 8h5M16 16h5"/>',
  series: '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="m8 3 4 4 4-4"/>',
  anime: '<path d="M12 3l2.3 6.2L20.5 11l-6.2 2.3L12 19.5l-2.3-6.2L3.5 11l6.2-1.8z"/>',
  _default: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
};

const navIcon = (library) =>
  `<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${
    LIBRARY_ICONS[library] || LIBRARY_ICONS._default
  }</svg>`;

// The tab the detail page returns to, and — for a show — what it is
// currently showing, so refreshWatched() can repaint its episode rows in
// place after playback without a full re-navigation.
let detailReturnTab = null;
let currentDetail = null;
let currentCollection = null;

// Titles and usernames are user-supplied, so never interpolate them raw into
// innerHTML — a show with an & or a quote in its name would break the markup.
const esc = (value) =>
  String(value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

// A card with its poster behind the title. `no-art` marks a card with no
// artwork — its .card-box background is just the placeholder gradient, same
// as a real poster's letterbox bars — so a card() caller (or the <img>'s own
// onerror) can tell the difference without a second lookup. Episode cards
// never carry a poster, hence the flag at build time as well as the onerror
// path.
// What this account has already watched, in the three identities cards are
// keyed by. Held here rather than baked into each listing so a mark can be
// updated after playback without refetching the grid — see /api/watched.
let watchedState = { items: new Set(), shows: new Set(), collections: new Set() };

// A show is identified by its library *and* title: the same show can sit in
// both Series and Anime, and they are separate entries.
const showKey = (library, title) => `${library}\n${title}`;

async function loadWatched() {
  const res = await api("/api/watched");
  const data = await res.json();
  watchedState = {
    items: new Set(data.items),
    shows: new Set(data.shows.map(([library, title]) => showKey(library, title))),
    collections: new Set(data.collections),
  };
}

// Same two identities as watchedState (movie/episode ids, [library, show]
// pairs), plus the full cards for the My List tab itself — one call covers
// both so opening a title's popup never needs a second round trip just to
// know what its own button should say.
let myListState = { items: new Set(), shows: new Set() };

async function loadMyList() {
  const res = await api("/api/list");
  const data = await res.json();
  myListState = {
    items: new Set(data.items),
    shows: new Set(data.shows.map(([library, title]) => showKey(library, title))),
  };
  const grid = document.getElementById("mylistGrid");
  grid.innerHTML =
    data.cards
      .map((it) =>
        card({
          poster: it.poster,
          title: it.title,
          meta: it.meta,
          onclick:
            it.kind === "show"
              ? `openDetail('show', '${it.library}', '${jsString(it.title)}')`
              : `openDetail('movie', ${it.id}, '${jsString(it.title)}')`,
          watched:
            it.kind === "movie"
              ? watchedState.items.has(it.id)
              : watchedState.shows.has(showKey(it.library, it.title)),
        })
      )
      .join("") ||
    '<p class="shelf-empty">Nothing saved yet. Open a title and choose Add to My List.</p>';
}

// One call for both directions — the server is the only thing that actually
// knows which way to flip. Reloads My List state afterward so the button
// that just fired, and the tab if it happens to be open, agree with it.
async function toggleMyList(kind, library, ref) {
  const res = await api("/api/list/toggle", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, library, ref: String(ref) }),
  });
  const data = await res.json();
  await loadMyList();
  return data.in_list;
}

// Every accessible library at once, filterable by genre. Remembered across
// tab switches — leaving Browse and coming back keeps the chip you picked
// rather than resetting to "All" every time.
let currentGenre = null;

async function loadBrowse(genre = currentGenre) {
  currentGenre = genre;
  const res = await api(`/api/discover${genre ? `?genre=${encodeURIComponent(genre)}` : ""}`);
  const data = await res.json();

  const count = data.items.length;
  document.getElementById("browseCount").textContent =
    `${count} title${count === 1 ? "" : "s"}${genre ? ` in ${genre}` : ""}`;

  document.getElementById("genreChips").innerHTML =
    `<div class="genre-chip${genre ? "" : " active"}" onclick="loadBrowse(null)">All</div>` +
    data.genres
      .map(
        (g) =>
          `<div class="genre-chip${
            g === genre ? " active" : ""
          }" onclick="loadBrowse('${jsString(g)}')">${esc(g)}</div>`
      )
      .join("");

  document.getElementById("browseGrid").innerHTML =
    data.items
      .map((it) =>
        card({
          poster: it.poster,
          title: it.title,
          meta: it.meta,
          onclick:
            it.kind === "show"
              ? `openDetail('show', '${it.library}', '${jsString(it.title)}')`
              : `openDetail('movie', ${it.id}, '${jsString(it.title)}')`,
          watched:
            it.kind === "movie"
              ? watchedState.items.has(it.id)
              : watchedState.shows.has(showKey(it.library, it.title)),
        })
      )
      .join("") || '<p class="shelf-empty">Nothing matches that genre yet.</p>';
}

// Titles go into onclick handlers as single-quoted JS string literals.
const jsString = (value) => String(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");

// Every card's placeholder art: a hue derived from the title, not chosen or
// random, so the same title always gets the same background — real posters
// sit on top of this and hide it, so it only ever shows through when one is
// missing or fails to load. static/profiles.html has its own tiny copy of
// this same idea for profile tiles, for the same reason it has its own api():
// there is no shared module system between pages here.
function placeholderHue(str) {
  let h = 0;
  for (const ch of str) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}
function placeholderGradient(str) {
  const h = placeholderHue(str);
  const h2 = (h + 34) % 360;
  return `linear-gradient(155deg, hsl(${h} 46% 22%), hsl(${h2} 40% 11%)), `
       + `repeating-linear-gradient(112deg, rgba(255,255,255,.05) 0 2px, transparent 2px 12px)`;
}

// Sits above the poster and outside .card-box, so it stays visible against
// any artwork. currentColor keeps it in step with the badge's own colour.
const WATCHED_BADGE = `<span class="watched-badge" role="img" aria-label="Watched" title="Watched"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 13 4 4 10-10"/></svg></span>`;

// Structure matches the Claude Design prototype (Aug 2026 handoff) exactly:
// the poster box is its own bordered, clipped element with the title always
// overlaid at its bottom edge — not a hover-reveal — and the meta line sits
// below the box in normal flow rather than inside the scrim.
function card({ poster, title, meta, onclick, watched = false, percent = null, onRemove = null }) {
  const art = poster
    ? `<img class="poster" src="${poster}" loading="lazy" alt=""
            onerror="this.closest('.card')?.classList.add('no-art'); this.remove()">`
    : "";
  // How far in you are, pinned to the bottom edge — read at a glance across
  // a shelf, not something that should wait on a hover.
  const bar =
    percent === null
      ? ""
      : `<div class="card-progress" role="img"
              aria-label="${Math.round(percent)}% watched"><span
              style="width: ${Math.max(percent, 1.5)}%"></span></div>`;
  // Top-left, mirroring the watched badge's top-right — hover-only, since
  // unlike "watched" this isn't something a shelf needs to read at a glance.
  // event.stopPropagation() keeps a tap on it from also hitting the card's
  // own onclick and starting playback of the thing being removed.
  const removeBtn = onRemove
    ? `<button type="button" class="card-remove" title="Remove from Continue Watching"
              aria-label="Remove from Continue Watching"
              onclick="event.stopPropagation(); ${onRemove}">&times;</button>`
    : "";
  return `
    <div class="card${poster ? "" : " no-art"}${watched ? " watched" : ""}" onclick="${onclick}">
      <div class="card-box" style="background: ${placeholderGradient(title)}">
        ${art}
        <div class="card-scrim"></div>
        ${watched ? WATCHED_BADGE : ""}
        ${removeBtn}
        <div class="card-title">${esc(title)}</div>
        ${bar}
      </div>
      ${meta ? `<div class="card-meta">${esc(meta)}</div>` : ""}
    </div>`;
}

// Drops one Continue Watching card without a full tab reload — DELETE first,
// only touch the DOM once the server actually agrees it's gone. Removing the
// whole shelf block when it empties out matches shelf() in renderTabShelves,
// which never renders "Continue Watching" with nothing in it either.
async function removeFromContinue(itemId, btn) {
  try {
    const res = await api(`/api/continue/${itemId}`, { method: "DELETE" });
    if (!res.ok) return;
  } catch (_) {
    return;
  }
  const cardEl = btn.closest(".card");
  const shelfBlock = cardEl?.closest(".shelf-block");
  cardEl?.remove();
  if (shelfBlock && !shelfBlock.querySelector(".card")) shelfBlock.remove();
}

const showPoster = (library, title) =>
  `/api/poster/show/${library}/${encodeURIComponent(title)}`;

// "S01E04", or nothing at all: the scanner leaves the numbers null for a file
// it could not read an SxxEyy out of, and "SnullEnull" is worse than the name.
const episodeLabel = (episode) =>
  episode.season == null || episode.episode == null
    ? ""
    : `S${String(episode.season).padStart(2, "0")}E${String(episode.episode).padStart(2, "0")}`;

// An episode's own banner, when the artwork sync found one for it. Asking only
// for the ones that have one keeps the grid from firing a request per episode
// and collecting 404s.
const episodeStill = (episode) => (episode.has_still ? `/api/still/item/${episode.id}` : "");

const modal = document.getElementById("playerModal");
const video = document.getElementById("videoPlayer");
const playerVideoArea = document.getElementById("playerVideoArea");
const playerAnnotation = document.getElementById("playerAnnotation");
const playerPlayGlyph = document.getElementById("playerPlayGlyph");
const playerTitle = document.getElementById("playerTitle");
const playerSubtitle = document.getElementById("playerSubtitle");
const transcodeStatus = document.getElementById("transcodeStatus");
const resumeNotice = document.getElementById("resumeNotice");
const resumeNoticeText = document.getElementById("resumeNoticeText");
const episodeNavBar = document.getElementById("episodeNav");
const nextUpTitleEl = document.getElementById("nextUpTitle");
const prevEpisodeBtn = document.getElementById("prevEpisodeBtn");
const nextEpisodeBtn = document.getElementById("nextEpisodeBtn");
const skipIntroBtn = document.getElementById("skipIntroBtn");
const playerScrub = document.getElementById("playerScrub");
const playerBuffered = document.getElementById("playerBuffered");
const playerFill = document.getElementById("playerFill");
const playerKnob = document.getElementById("playerKnob");
const playPauseBtn = document.getElementById("playPauseBtn");
const playerTimeEl = document.getElementById("playerTime");
const playerTotalEl = document.getElementById("playerTotal");
const volumeSlider = document.getElementById("volumeSlider");
const audioChip = document.getElementById("audioChip");
const audioMenu = document.getElementById("audioMenu");
const subsChip = document.getElementById("subsChip");
const subsMenu = document.getElementById("subsMenu");
const qualityChip = document.getElementById("qualityChip");
const qualityMenu = document.getElementById("qualityMenu");
const qualityChipLabel = document.getElementById("qualityChipLabel");
const fullscreenChip = document.getElementById("fullscreenChip");

let currentItemId = null;
let hls = null;

// Absolute position, in the title, that "Skip Intro" jumps to — null for a
// movie, or before /api/resume has answered. There is no per-title intro
// detection, so this is the single server-configured offset (SKIP_INTRO_SECONDS)
// and the button is only ever offered on an episode.
let skipIntroSeconds = null;

// The current title's place among its show's episodes, so Prev/Next know
// what to play. Null for a movie, or before it has been looked up.
let episodeNav = null;
// Bumped by every updateEpisodeNav(), same idea as detailGeneration: a fast
// Next click must not have an in-flight lookup for the title it just left
// overwrite the nav bar for the title it landed on.
let episodeNavGeneration = 0;

// Re-derived on every playItem() rather than carried over from the detail
// page, because Continue Watching can land on an episode directly without
// its show's detail page ever having been opened this session.
async function updateEpisodeNav(id) {
  const generation = ++episodeNavGeneration;
  episodeNav = null;
  episodeNavBar.hidden = true;

  let row;
  try {
    const res = await api(`/api/item/${id}`);
    row = res.ok ? await res.json() : null;
  } catch {
    row = null;                     // nav is a convenience, never a blocker
  }
  if (generation !== episodeNavGeneration || !row || row.type !== "episode") return;

  let episodes = [];
  try {
    const res = await api(
      `/api/shows/${encodeURIComponent(row.show_title)}/episodes?library=${row.library}`
    );
    episodes = res.ok ? await res.json() : [];
  } catch {
    return;
  }
  if (generation !== episodeNavGeneration) return;

  const index = episodes.findIndex((e) => e.id === id);
  if (index === -1) return;

  episodeNav = { episodes, index };
  episodeNavBar.hidden = false;
  prevEpisodeBtn.disabled = index <= 0;
  nextEpisodeBtn.disabled = index >= episodes.length - 1;

  if (row.season != null && row.episode != null) {
    playerSubtitle.textContent = `Season ${row.season} · Episode ${row.episode}`;
  }
  const next = episodes[index + 1];
  nextUpTitleEl.textContent = next ? next.title || episodeLabel(next) : "";
}

prevEpisodeBtn.onclick = () => {
  if (!episodeNav || episodeNav.index <= 0) return;
  const e = episodeNav.episodes[episodeNav.index - 1];
  playItem(e.id, e.title || episodeLabel(e));
};

nextEpisodeBtn.onclick = () => {
  if (!episodeNav || episodeNav.index >= episodeNav.episodes.length - 1) return;
  const e = episodeNav.episodes[episodeNav.index + 1];
  playItem(e.id, e.title || episodeLabel(e));
};

// Where this session's timeline zero sits in the film. Direct play seeks the
// file itself and leaves this at 0; a transcode is a fresh HLS stream ffmpeg
// started at the offset, so its currentTime counts from there and every
// position reported has to add this back on.
let playbackOffset = 0;

// Direct Play is the source file as-is, which no browser is obliged to
// understand: Safari plays no Matroska at all, and every browser refuses some
// codec or other. When it fails there is nothing to see and nothing logged
// server-side, because the file was served perfectly well — so the player
// watches for the failure itself and switches to a transcode.
const FALLBACK_QUALITY = "1080p";
// Bumped by every setSource, so an error arriving late from a source already
// switched away from cannot trigger a fallback for the current one.
let sourceGeneration = 0;
// Once per title. A fallback that could fire twice is a loop.
let directFallbackUsed = false;

const activeMode = () =>
  document.querySelector("#qualityMenu .option.active")?.dataset.mode || "direct";

// The absolute point in the title the player is at, whichever mode it is in.
const playbackPosition = () =>
  playbackOffset + (Number.isFinite(video.currentTime) ? video.currentTime : 0);

// "1:24:07" or "42:07" — the hour only appears once there is one.
function timecode(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const parts = [Math.floor(total / 60) % 60, total % 60];
  if (total >= 3600) parts.unshift(Math.floor(total / 3600));
  return parts
    .map((n, i) => (i === 0 ? String(n) : String(n).padStart(2, "0")))
    .join(":");
}

async function playItem(id, title, { from = null } = {}) {
  currentItemId = id;
  lastReportedAt = 0;
  directFallbackUsed = false;
  skipIntroSeconds = null;
  skipIntroBtn.hidden = true;
  playerTitle.textContent = title;
  playerSubtitle.textContent = "";
  nextUpTitleEl.textContent = "";
  subsChip.hidden = true;
  subsMenu.hidden = true;
  subsMenu.innerHTML = "";
  audioChip.hidden = true;
  audioMenu.hidden = true;
  audioMenu.innerHTML = "";
  setActiveQuality("direct");
  modal.classList.remove("hidden");
  modal.classList.add("paused");   // autoplay hasn't started yet — starts true, corrected by the play event

  // Where to pick up. Asked of the server rather than remembered here, because
  // it has to be the current number — the same title may have been watched
  // further on another device since this page loaded. `from: 0` is how "Start
  // from the beginning" overrides it. Fetched unconditionally now (not just
  // when `from` is null) because it also carries skip_intro_seconds, which a
  // Continue Watching resume needs just as much as a fresh open does.
  let resumeInfo = null;
  try {
    const res = await api(`/api/resume/${id}`);
    resumeInfo = res.ok ? await res.json() : null;
  } catch {
    resumeInfo = null;              // resuming is a convenience, not a gate
  }
  const start = from !== null ? from : resumeInfo?.position || 0;
  skipIntroSeconds = resumeInfo?.skip_intro_seconds ?? null;
  showResumeNotice(start);
  loadSubtitles(id);
  // Awaited, unlike loadSubtitles()/updateEpisodeNav() below: this decides
  // currentAudioIndex, which the very first setSource() call — the
  // audio_incompatible branch right below, or the plain Direct Play attempt
  // after it — bakes into its transcode URL. Firing it and moving on (the
  // subtitle/episode-nav pattern) would start playback on whatever the file's
  // own track order happens to default to, before the English-preferring
  // pick below ever got a chance to land.
  await loadAudioTracks(id);
  updateEpisodeNav(id);

  if (resumeInfo?.audio_incompatible) {
    // Direct Play would serve the video track fine and just play it silent —
    // no browser decodes Dolby/DTS audio, and nothing about that is a video
    // element `error` the reactive fallback below could ever catch. Skip
    // straight to a transcode instead of starting someone off muted.
    directFallbackUsed = true;
    transcodeStatus.textContent =
      `This file's audio isn't supported here — starting at ${FALLBACK_QUALITY}.`;
    setSource(id, FALLBACK_QUALITY, start);
    return;
  }

  // autoFallback only here: opening a title picks Direct Play on the viewer's
  // behalf, so recovering from it is the player's business. Choosing it by
  // hand from the quality row is a decision, and gets left alone.
  setSource(id, "direct", start, { autoFallback: true });
}

// Sidecar subtitle tracks, re-read per title. They are <track> elements rather
// than anything burned into the picture, so the same ones work in Direct Play
// and in transcode mode, and switching between the two does not disturb them.
async function loadSubtitles(id) {
  video.querySelectorAll("track").forEach((t) => t.remove());
  let tracks = [];
  try {
    const res = await api(`/api/subtitles/${id}`);
    tracks = res.ok ? await res.json() : [];
  } catch {
    return;                       // subtitles are a bonus, never a blocker
  }
  let defaultTrack = null;
  for (const entry of tracks) {
    const track = document.createElement("track");
    track.kind = "subtitles";
    track.label = entry.label;
    if (entry.lang) track.srclang = entry.lang;
    track.src = entry.url;
    video.appendChild(track);
    // Plain "English" wins over a CC/SDH/Forced variant even if one of those
    // sorted earlier — anyone who wants the flagged track can still pick it
    // from the browser's own menu.
    if (entry.label === "English" || (!defaultTrack && entry.label.startsWith("English"))) {
      defaultTrack = track;
    }
  }
  if (defaultTrack) defaultTrack.track.mode = "showing";
  renderSubsMenu();
}

// The "Subtitles" chip's popover — replaces the picker the browser's native
// <video controls> used to offer for these same <track> elements, now that
// the player draws its own controls instead.
function renderSubsMenu() {
  const tracks = [...video.querySelectorAll("track")];
  if (!tracks.length) {
    subsChip.hidden = true;
    return;
  }
  subsChip.hidden = false;
  const options = [{ label: "Off", track: null }, ...tracks.map((t) => ({ label: t.label, track: t }))];
  subsMenu.innerHTML = options
    .map((o, i) => `<div class="option" data-index="${i}">${esc(o.label)}</div>`)
    .join("");
  const refreshActive = () => {
    const activeIndex = tracks.findIndex((t) => t.track.mode === "showing");
    subsMenu.querySelectorAll(".option").forEach((el, i) => {
      el.classList.toggle("active", i === (activeIndex === -1 ? 0 : activeIndex + 1));
    });
  };
  subsMenu.querySelectorAll(".option").forEach((el, i) => {
    el.onclick = (event) => {
      event.stopPropagation();
      tracks.forEach((t) => (t.track.mode = "hidden"));
      if (options[i].track) options[i].track.mode = "showing";
      refreshActive();
      subsMenu.hidden = true;
    };
  });
  refreshActive();
}

subsChip.addEventListener("click", (event) => {
  event.stopPropagation();
  subsMenu.hidden = !subsMenu.hidden;
});
document.addEventListener("click", () => {
  subsMenu.hidden = true;
  audioMenu.hidden = true;
  qualityMenu.hidden = true;
});

// Audio tracks embedded in the file itself — a release with a dub, or a
// separate commentary track, rather than the picture's own single mix.
// Re-read per title, same as loadSubtitles(). Unlike a subtitle <track>,
// though, picking one is not free: it re-fetched below in switchAudioTrack().
let audioTracks = [];
let currentAudioIndex = 0;

async function loadAudioTracks(id) {
  audioTracks = [];
  currentAudioIndex = 0;
  try {
    const res = await api(`/api/audio/${id}`);
    audioTracks = res.ok ? await res.json() : [];
  } catch {
    audioTracks = [];              // audio track choice is a bonus, never a blocker
  }
  // Default to English when the file has it — the same convention
  // loadSubtitles() already uses. A release's own "default" disposition flag
  // is set by whoever muxed it and is not reliably the language most viewers
  // here want (some AMZN/ITA rips default to Italian outright); matching by
  // label sidesteps trusting it, exactly like the subtitle picker already does.
  const english = audioTracks.findIndex(
    (t) => t.label === "English" || t.label.startsWith("English")
  );
  if (english !== -1) currentAudioIndex = english;
  renderAudioMenu();
}

// The "Audio" chip's popover. A file with only one audio track has nothing to
// pick between, so the chip stays hidden exactly like Subtitles does with no
// tracks — there is no "Off" option here the way subtitles have one, since a
// video always needs *some* audio track playing.
function renderAudioMenu() {
  if (audioTracks.length < 2) {
    audioChip.hidden = true;
    return;
  }
  audioChip.hidden = false;
  audioMenu.innerHTML = audioTracks
    .map((t, i) => `<div class="option" data-index="${i}">${esc(t.label)}</div>`)
    .join("");
  const refreshActive = () => {
    audioMenu.querySelectorAll(".option").forEach((el, i) => {
      el.classList.toggle("active", i === currentAudioIndex);
    });
  };
  audioMenu.querySelectorAll(".option").forEach((el, i) => {
    el.onclick = (event) => {
      event.stopPropagation();
      audioMenu.hidden = true;
      if (i === currentAudioIndex) return;
      currentAudioIndex = i;
      refreshActive();
      switchAudioTrack();
    };
  });
  refreshActive();
}

// Direct Play serves the file's own default audio track as-is — there is no
// way to swap a muxed track without re-encoding, so picking anything but that
// default forces the same switch into a transcode that an incompatible codec
// already does automatically at open time (see playItem()'s
// audio_incompatible check). Shares the quality row's own click cooldown:
// this starts a new ffmpeg session exactly like a quality switch does, and
// is exactly as vulnerable to the same rapid-re-pick GPU pileup.
function switchAudioTrack() {
  if (!currentItemId || Date.now() < qualitySwitchCooldownUntil) return;
  qualitySwitchCooldownUntil = Date.now() + QUALITY_SWITCH_COOLDOWN_MS;
  const at = playbackPosition();
  const mode = activeMode() === "direct" ? FALLBACK_QUALITY : activeMode();
  setSource(currentItemId, mode, at, { autoFallback: false });
}

audioChip.addEventListener("click", (event) => {
  event.stopPropagation();
  audioMenu.hidden = !audioMenu.hidden;
});

function showResumeNotice(start) {
  if (!start) {
    resumeNotice.hidden = true;
    return;
  }
  resumeNoticeText.textContent = `Resuming from ${timecode(start)}`;
  resumeNotice.hidden = false;
}

document.getElementById("restartBtn").onclick = () => {
  if (currentItemId === null) return;
  resumeNotice.hidden = true;
  lastReportedAt = 0;
  setSource(currentItemId, activeMode(), 0);
};

// `start` is a point in the *title*, not in the stream: direct play seeks the
// file once its metadata is in, and a transcode asks ffmpeg to begin there.
const qualityLabel = (mode) => (mode === "direct" ? "Direct Play" : mode);

// The single place that keeps the quality dropdown's own label, its
// highlighted option, and the paused-state annotation all agreeing with each
// other — called from setSource() itself, so anything that changes the
// active quality by starting a new source (a manual pick, a Direct Play
// fallback, an audio-track switch that forces a transcode) gets this for
// free instead of repeating the same three updates at every call site.
function setActiveQuality(mode) {
  document.querySelectorAll("#qualityMenu .option").forEach((el) =>
    el.classList.toggle("active", el.dataset.mode === mode)
  );
  qualityChipLabel.textContent = qualityLabel(mode);
  playerAnnotation.textContent = qualityLabel(mode);
}

async function setSource(id, mode, start = 0, { autoFallback = false } = {}) {
  setActiveQuality(mode);
  if (hls) {
    hls.destroy();
    hls = null;
  }
  video.pause();
  const generation = ++sourceGeneration;

  if (mode === "direct") {
    transcodeStatus.textContent = "";
    playbackOffset = 0;
    video.src = `/stream/${id}`;
    if (start > 0) {
      // currentTime cannot be set before the browser knows how long the file
      // is, and a fresh src has thrown that away.
      video.addEventListener(
        "loadedmetadata",
        () => { video.currentTime = start; },
        { once: true }
      );
    }
    video.addEventListener(
      "error",
      () => {
        if (generation !== sourceGeneration) return;    // already moved on
        if (!autoFallback || directFallbackUsed) {
          transcodeStatus.textContent =
            "This browser can't play this file directly — try 1080p.";
          return;
        }
        directFallbackUsed = true;
        transcodeStatus.textContent =
          `This file can't play directly here — switching to ${FALLBACK_QUALITY}.`;
        setSource(id, FALLBACK_QUALITY, start);
      },
      { once: true }
    );
    video.play().catch(() => {});
    return;
  }

  // Transcode mode: ffmpeg needs a few seconds to produce the first segments.
  // Its output timeline restarts at zero from `start`, so hold the offset.
  transcodeStatus.textContent = "Starting transcode...";
  playbackOffset = start;
  const url = `/transcode/${id}/${mode}/${Math.floor(start)}/${currentAudioIndex}/playlist.m3u8`;

  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    // Safari / iOS: native HLS support, no hls.js needed
    video.src = url;
    video.addEventListener("loadedmetadata", () => (transcodeStatus.textContent = ""), { once: true });
    video.play().catch(() => {});
  } else if (window.Hls && Hls.isSupported()) {
    hls = new Hls({
      maxBufferLength: 30,
      // hls.js's own default (10s, one retry ~1s later — gives up around 21s)
      // is shorter than the server's budget for ffmpeg to produce the first
      // segment (transcoder.wait_for_playlist's 25s), which matters under
      // real load: several viewers sharing one GPU visibly slows ffmpeg
      // startup. Without this, hls.js can give up before the server ever
      // answers — the player just never loads, with nothing obviously wrong.
      manifestLoadingTimeOut: 35000,
      // Every transcode session's own HLS timeline starts at zero, however
      // far into the film `start` actually seeked ffmpeg's source — so 0 is
      // always the right place to begin, never "auto". Without this, hls.js
      // treats the still-growing playlist (no #EXT-X-ENDLIST until ffmpeg
      // finishes) as *live* — that classification depends only on ENDLIST
      // being absent, not on -hls_playlist_type — and seeks near the "live
      // edge" instead of segment 0. GPU encode races at 10-13x realtime, so
      // by the time hls.js makes that call the edge is already minutes in:
      // playback starts fine, then jumps far ahead and stalls waiting for a
      // segment nowhere near what's actually buffered.
      startPosition: 0,
    });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      transcodeStatus.textContent = "";
      video.play().catch(() => {});
    });
    hls.on(Hls.Events.ERROR, (event, data) => {
      if (data.fatal) transcodeStatus.textContent = "Transcode error — check server logs";
    });
  } else {
    transcodeStatus.textContent = "This browser can't play HLS. Try Direct Play instead.";
  }
}

// A transcode can take several real seconds to produce its first segment —
// long enough that re-picking a quality option reads as "my click didn't
// work" rather than "it's starting." Each pick used to launch its own
// ffmpeg session regardless, and two or three of those for the same title
// competing for one GPU encoder is what actually broke playback (each one
// slows every other one down). The cooldown below is the client-side half of
// the fix — the server also now kills any sibling session for the same item
// (see start_transcode() in transcoder.py) as a second line of defense.
let qualitySwitchCooldownUntil = 0;
const QUALITY_SWITCH_COOLDOWN_MS = 4000;

qualityMenu.querySelectorAll(".option").forEach((opt) => {
  opt.onclick = async (event) => {
    event.stopPropagation();
    qualityMenu.hidden = true;
    if (!currentItemId || Date.now() < qualitySwitchCooldownUntil) return;
    qualitySwitchCooldownUntil = Date.now() + QUALITY_SWITCH_COOLDOWN_MS;
    // Carry the current place across the switch. Changing quality is not a
    // decision to watch the film again from the top, and in transcode mode the
    // new session would otherwise start ffmpeg at zero.
    const at = playbackPosition();
    await reportProgress(true);
    lastReportedAt = at;
    setSource(currentItemId, opt.dataset.mode, at, { autoFallback: false });
  };
});

qualityChip.addEventListener("click", (event) => {
  event.stopPropagation();
  qualityMenu.hidden = !qualityMenu.hidden;
});

// Nothing tells the server where playback got to but the player, so it reports
// in while it runs — every fifteen seconds of playback is often enough to
// survive a closed laptop, and cheap enough that a film costs a couple of
// hundred tiny posts.
const PROGRESS_INTERVAL_SECONDS = 15;
let lastReportedAt = 0;

async function reportProgress(force = false) {
  if (currentItemId === null) return;
  const position = playbackPosition();
  if (!Number.isFinite(position) || position <= 0) return;
  if (!force && Math.abs(position - lastReportedAt) < PROGRESS_INTERVAL_SECONDS) return;
  lastReportedAt = position;
  // hls.js reports the length of what ffmpeg has written so far, not the
  // film's — the server prefers its own ffprobe and only falls back to this.
  // Even that fallback has to be shifted: a session that began 40 minutes in is
  // 40 minutes shorter than the title it came from.
  const duration = Number.isFinite(video.duration)
    ? video.duration + playbackOffset
    : null;
  try {
    await api("/api/progress", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: currentItemId, position, duration }),
    });
  } catch {
    // A dropped beat is not worth interrupting playback over; the next one, or
    // the final one on close, carries the same high-water mark.
  }
}

// Shown only while the current absolute position is still short of the
// offset — naturally hides itself once playback (or a manual seek) passes it,
// with no separate "dismissed" flag to track.
function updateSkipIntroVisibility() {
  skipIntroBtn.hidden = skipIntroSeconds === null || playbackPosition() >= skipIntroSeconds;
}

skipIntroBtn.onclick = () => {
  if (skipIntroSeconds === null) return;
  skipIntroBtn.hidden = true;
  seekToAbsolute(skipIntroSeconds);
};

video.addEventListener("timeupdate", () => {
  reportProgress();
  updateSkipIntroVisibility();
  updateScrubUI();
});
video.addEventListener("pause", () => reportProgress(true));
video.addEventListener("ended", async () => {
  await reportProgress(true);
  resumeNotice.hidden = true;     // it finished; there is nothing to resume
  refreshWatched();               // the mark can appear without closing the player
});

// ---- custom control bar ----

function syncPlayPauseUI() {
  playPauseBtn.textContent = video.paused ? "▶" : "❙❙";
  modal.classList.toggle("paused", video.paused);
}
video.addEventListener("play", syncPlayPauseUI);
video.addEventListener("pause", syncPlayPauseUI);

playPauseBtn.onclick = () => (video.paused ? video.play() : video.pause());

// Clicking the video toggles play — except the Back button and Skip Intro,
// which sit visually on top of it and have their own actions.
playerVideoArea.addEventListener("click", (event) => {
  if (event.target.closest(".player-topbar, .skip-intro-btn")) return;
  if (video.paused) video.play();
  else video.pause();
});

// The whole title's length, whichever mode is active — a transcode's own
// video.duration is only the length of the HLS session ffmpeg has written so
// far, so the offset it started at has to be added back on.
function totalDuration() {
  return Number.isFinite(video.duration) ? playbackOffset + video.duration : null;
}

function updateScrubUI() {
  const total = totalDuration();
  const pos = playbackPosition();
  playerTimeEl.textContent = timecode(pos);
  playerTotalEl.textContent = total ? timecode(total) : "--:--";
  if (!total) return;
  const percent = Math.min(100, Math.max(0, (pos / total) * 100));
  playerFill.style.width = `${percent}%`;
  playerKnob.style.left = `${percent}%`;
  if (video.buffered.length) {
    const bufferedEnd = playbackOffset + video.buffered.end(video.buffered.length - 1);
    playerBuffered.style.width = `${Math.min(100, (bufferedEnd / total) * 100)}%`;
  }
}

// Seeking to an absolute point in the title. Direct play can just jump there;
// a transcode session's own timeline starts at zero from wherever it began,
// so reaching a new point means asking ffmpeg for a fresh one there — the
// same two cases Skip Intro below has always had to handle.
async function seekToAbsolute(target) {
  if (currentItemId === null) return;
  if (activeMode() === "direct") {
    video.currentTime = target;
  } else {
    await reportProgress(true);
    lastReportedAt = target;
    setSource(currentItemId, activeMode(), target);
  }
}

playerScrub.addEventListener("click", (event) => {
  const total = totalDuration();
  if (!total) return;
  const rect = playerScrub.getBoundingClientRect();
  const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
  seekToAbsolute(fraction * total);
});

document.getElementById("back10Btn").onclick = () =>
  seekToAbsolute(Math.max(0, playbackPosition() - 10));
document.getElementById("fwd10Btn").onclick = () => {
  const total = totalDuration();
  const target = playbackPosition() + 10;
  seekToAbsolute(total ? Math.min(target, total) : target);
};

// The --vol-fill custom property drives the track's gradient fill in Chromium
// (style.css); Firefox fills via its own ::-moz-range-progress and ignores it.
function syncVolumeFill() {
  volumeSlider.style.setProperty("--vol-fill", `${volumeSlider.value}%`);
}
volumeSlider.addEventListener("input", () => {
  video.volume = volumeSlider.value / 100;
  syncVolumeFill();
});
syncVolumeFill();

// Cross-browser: Chrome/Firefox/desktop Safari support Element.requestFullscreen
// (Safari needs its webkit-prefixed form pre-16.4). iPhone Safari has neither —
// it never implemented fullscreen for arbitrary elements, only for <video> via
// the older webkitEnterFullscreen — so that's the fallback once both are absent.
function fullscreenElement() {
  return document.fullscreenElement || document.webkitFullscreenElement || null;
}
fullscreenChip.onclick = () => {
  if (fullscreenElement()) {
    // webkitExitFullscreen (older Safari) returns undefined, not a promise.
    Promise.resolve((document.exitFullscreen || document.webkitExitFullscreen).call(document)).catch(() => {});
    return;
  }
  if (modal.requestFullscreen) {
    modal.requestFullscreen().catch(() => {});
  } else if (modal.webkitRequestFullscreen) {
    modal.webkitRequestFullscreen();
  } else if (video.webkitEnterFullscreen) {
    video.webkitEnterFullscreen();
  }
};
function syncFullscreenUI() {
  fullscreenChip.classList.toggle("active", !!fullscreenElement());
  fullscreenChip.textContent = fullscreenElement() ? "Exit full screen" : "Full screen";
}
document.addEventListener("fullscreenchange", syncFullscreenUI);
document.addEventListener("webkitfullscreenchange", syncFullscreenUI);

// Fullscreen only: fade the topbar/control bar out after a few seconds of no
// mouse/keyboard activity during playback, the way YouTube/Netflix do — so
// idle chrome doesn't sit over the picture indefinitely.
let controlsIdleTimer = null;
function showControls() {
  modal.classList.remove("controls-hidden");
  if (controlsIdleTimer) clearTimeout(controlsIdleTimer);
  if (fullscreenElement() && !video.paused) {
    controlsIdleTimer = setTimeout(() => modal.classList.add("controls-hidden"), 3000);
  }
}
modal.addEventListener("mousemove", showControls);
modal.addEventListener("mousedown", showControls);
modal.addEventListener("keydown", showControls);
video.addEventListener("play", showControls);
video.addEventListener("pause", showControls);
document.addEventListener("fullscreenchange", showControls);
document.addEventListener("webkitfullscreenchange", showControls);

document.getElementById("closePlayer").onclick = async () => {
  // Before anything is torn down: currentTime is gone once src is cleared, and
  // this last beat is what makes closing the tab mid-film resumable.
  await reportProgress(true);
  if (hls) {
    hls.destroy();
    hls = null;
  }
  video.pause();
  video.src = "";
  currentItemId = null;
  playbackOffset = 0;
  resumeNotice.hidden = true;
  episodeNav = null;
  episodeNavBar.hidden = true;
  skipIntroSeconds = null;
  skipIntroBtn.hidden = true;
  playerFill.style.width = "0%";
  playerKnob.style.left = "0%";
  playerBuffered.style.width = "0%";
  playerTimeEl.textContent = "0:00";
  playerTotalEl.textContent = "0:00";
  subsMenu.hidden = true;
  modal.classList.remove("paused");
  modal.classList.add("hidden");
  refreshWatched();
};

// Sidebar collapse, remembered across visits
const COLLAPSE_KEY = "homereel.sidebarCollapsed";

function setCollapsed(collapsed) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  const toggle = document.getElementById("sidebarToggle");
  const label = collapsed ? "Expand sidebar" : "Collapse sidebar";
  toggle.title = label;
  toggle.setAttribute("aria-label", label);
  localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
}

setCollapsed(localStorage.getItem(COLLAPSE_KEY) === "1");

document.getElementById("sidebarToggle").onclick = () =>
  setCollapsed(!document.body.classList.contains("sidebar-collapsed"));

// Tabs. Panels without a sidebar button (episodes, search results) are shown
// with showPanel() alone, so the nav keeps highlighting where you came from.
function showPanel(id) {
  const panels = Array.from(document.querySelectorAll(".tab-panel"));
  const current = panels.find((p) => p.classList.contains("active"));
  const next = panels.find((p) => p.id === id);
  if (!next || next === current || !swipeHero(current, next)) {
    panels.forEach((p) => p.classList.toggle("active", p === next));
  }
}

// Library tabs (Movies/Series/Anime, ...) each keep one fixed hero, so
// clicking between them used to hard-cut straight to the next poster. This
// slides the outgoing hero out while the incoming one slides in from the
// opposite edge, direction taken from the two tabs' left-to-right order in
// the sidebar. Returns false (and does nothing) for any panel pair that
// doesn't carry a hero — detail/collection panels, Browse, Search — so
// those keep the plain instant swap.
function swipeHero(current, next) {
  const oldHero = current?.querySelector(".tab-hero");
  const newHero = next.querySelector(".tab-hero");
  if (!oldHero || !newHero) return false;

  // Guard against a hero mid-swipe from a click fired before the last one settled.
  document.querySelectorAll(".tab-hero-clone").forEach((c) => c.remove());
  document.querySelectorAll(".tab-hero").forEach((h) =>
    h.classList.remove("hero-swipe-in-left", "hero-swipe-in-right", "hero-swipe-in-active")
  );

  const order = Array.from(document.querySelectorAll(".tab-btn")).map((b) => b.dataset.tab);
  const forward = order.indexOf(next.id) >= order.indexOf(current.id);

  const rect = oldHero.getBoundingClientRect();
  const clone = oldHero.cloneNode(true);
  clone.classList.add("tab-hero-clone");
  // cloneNode(true) copies descendant ids too (heroListBtn) — strip them so
  // this throwaway overlay never shadows the real, still-interactive button.
  clone.querySelectorAll("[id]").forEach((el) => el.removeAttribute("id"));
  Object.assign(clone.style, {
    top: `${rect.top}px`,
    left: `${rect.left}px`,
    width: `${rect.width}px`,
    height: `${rect.height}px`,
  });
  document.body.appendChild(clone);

  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p === next));
  newHero.classList.add(forward ? "hero-swipe-in-right" : "hero-swipe-in-left");

  requestAnimationFrame(() => {
    clone.classList.add(forward ? "hero-swipe-out-left" : "hero-swipe-out-right");
    newHero.classList.add("hero-swipe-in-active");
  });

  const cleanup = () => {
    clone.remove();
    newHero.classList.remove("hero-swipe-in-left", "hero-swipe-in-right", "hero-swipe-in-active");
  };
  clone.addEventListener("transitionend", cleanup, { once: true });
  setTimeout(cleanup, 500); // safety net if transitionend never fires (tab hidden mid-animation, etc.)
  return true;
}

function activateTab(tab) {
  document.querySelectorAll(".tab-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab)
  );
  if (tab === tabId("movies") || tab === "collections") setMoviesOpen(true);
  showPanel(tab);
  syncLivePolling(tab);
  if (tab === "browse") loadBrowse();
  if (tab === "search") loadSearchPills();
  if (tab === "mylist") loadMyList();
  // Home lives at "/" rather than "/home" — it's the app's root, not a tab
  // named after itself. Skipped when the URL already matches: without this,
  // a popstate-driven call here (see below) would push a brand new entry on
  // top of the one the browser just navigated to, breaking Back/Forward.
  const path = tab === "home" ? "/" : `/${tab}`;
  if (location.pathname !== path) history.pushState(null, "", path);
}

// Home — Continue Watching plus a Recently Added row per accessible library —
// is the landing tab. It exists (and renders something, even if empty) for
// every signed-in account regardless of which libraries are granted, so
// unlike a per-library tab it needs no fallback.
const firstTab = () => "home";

const activeTab = () =>
  document.querySelector(".tab-btn.active")?.dataset.tab || firstTab();

// The tab the current URL names, if any — "/" is Home, "/movies" is the tab
// with data-tab="movies", and so on. null for a path that matches no known
// tab (a stale link, a library since unmapped), so the caller can fall back
// to firstTab() instead of landing on a dead panel.
function tabFromUrl() {
  const id = location.pathname.replace(/^\/+|\/+$/g, "") || "home";
  return document.querySelector(`.tab-btn[data-tab="${CSS.escape(id)}"]`) ? id : null;
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.onclick = () => activateTab(btn.dataset.tab);
});

// The wordmark doubles as a Home shortcut, same as clicking a logo does on
// most sites. role="button" in the markup makes it keyboard-focusable, so
// Enter/Space have to be wired by hand the way a native <button> gets for free.
const brandHome = document.getElementById("brandHome");
brandHome.onclick = () => activateTab("home");
brandHome.onkeydown = (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    activateTab("home");
  }
};

// Back/Forward: the browser has already updated location.pathname by the
// time this fires, so activateTab() sees its own path check pass and skips
// pushing a redundant new entry — it only re-renders the panel.
window.addEventListener("popstate", () => {
  const tab = tabFromUrl();
  if (tab) activateTab(tab);
});

// Movies is a disclosure group holding Collections. Landing on either opens it;
// the caret toggles it on its own without changing tabs. Both are built by
// buildLibraryNav(), so the wiring runs again every time the nav is rebuilt.
function setMoviesOpen(open) {
  const group = document.getElementById("moviesGroup");
  if (!group) return;               // no Movies library on this account
  group.classList.toggle("open", open);
  document.getElementById("moviesCaret").title = open ? "Hide collections" : "Show collections";
}

function wireMoviesCaret() {
  const caret = document.getElementById("moviesCaret");
  if (!caret) return;
  caret.onclick = (event) => {
    event.stopPropagation();        // the caret sits inside the Movies button
    setMoviesOpen(!document.getElementById("moviesGroup").classList.contains("open"));
  };
}

// The play was recorded server-side the moment /stream was hit, so the marks on
// screen are stale as soon as the player closes. Re-read them and repaint what
// is visible — a finished episode can also complete its show, and a finished
// film its collection, neither of which the grid behind the player would show.
async function refreshWatched() {
  await loadWatched();
  const isOpen = (id) => document.getElementById(id)?.classList.contains("active");
  await Promise.all([
    loadLibraries(),         // every tab's hero + shelves, Continue Watching included
    isOpen("detail") && currentDetail
      ? openDetail(currentDetail.kind, currentDetail.idOrLibrary, currentDetail.title)
      : null,
    isOpen("collection") && currentCollection ? openCollection(currentCollection) : null,
    isOpen("search") ? runSearch(document.getElementById("searchInput").value.trim()) : null,
  ]);
}

// The hero for one top-level tab (Movies/Series/Anime, or any library Settings
// maps beyond those three — the treatment is the same either way). null when
// the library has nothing indexed yet.
function renderTabHero(el, hero) {
  if (!hero) {
    el.innerHTML = "";
    return;
  }
  const metaParts = [
    hero.year,
    hero.meta,
    hero.rating != null ? `★ ${Number(hero.rating).toFixed(1)}` : null,
  ].filter(Boolean);
  const inList =
    hero.kind === "movie"
      ? myListState.items.has(hero.id)
      : myListState.shows.has(showKey(hero.library, hero.title));
  const listRef = hero.kind === "movie" ? String(hero.id) : hero.title;
  const detailTarget = hero.kind === "movie" ? hero.id : `'${jsString(hero.library)}'`;

  el.innerHTML = `
    <div class="tab-hero" style="background: ${placeholderGradient(hero.title)}">
      <div class="tab-hero-stripes"></div>
      <div class="tab-hero-scrim"></div>
      ${
        hero.poster
          ? `<div class="tab-hero-poster">
               <img src="${esc(hero.poster)}" alt="" loading="lazy"
                    onerror="this.closest('.tab-hero-poster').remove()">
             </div>`
          : ""
      }
      <div class="tab-hero-body">
        <div class="tab-hero-badge">${esc(hero.badge)}</div>
        <h1 class="tab-hero-title">${esc(hero.title)}</h1>
        <div class="tab-hero-meta">${metaParts.map(esc).join(' <span class="tab-hero-dot">/</span> ')}</div>
        <p class="tab-hero-synopsis">${esc(hero.synopsis || "")}</p>
        <div class="tab-hero-actions">
          <button type="button" onclick="playItem(${hero.id}, '${jsString(hero.title)}')">&#9654; Play</button>
          <button type="button" class="secondary"
                  onclick="openDetail('${hero.kind}', ${detailTarget}, '${jsString(hero.title)}')">Details</button>
          <button type="button" class="secondary" id="heroListBtn"
                  onclick="toggleHeroList('${hero.kind}', '${jsString(hero.library)}', '${jsString(listRef)}')">${
            inList ? "✓ Saved" : "+ My List"
          }</button>
        </div>
      </div>
    </div>`;
}

async function toggleHeroList(kind, library, ref) {
  const btn = document.getElementById("heroListBtn");
  btn.disabled = true;
  const nowIn = await toggleMyList(kind, library, ref);
  btn.disabled = false;
  btn.textContent = nowIn ? "✓ Saved" : "+ My List";
}

// The four shelves under a tab's hero. Continue Watching disappears outright
// when empty (nothing part-watched reads as "not there" rather than a bare
// heading); the others show an empty note instead, since "New"/"Top 10"/
// "Recommended" existing-but-empty is still useful to see once, on a library
// that only just started scanning.
// Shared by every hero+shelves tab — one library's (renderTabShelves) or the
// aggregate Home's (renderHomeShelves) — so a card looks and behaves
// identically wherever it shows up.
function detailCard(entry) {
  return card({
    poster: entry.poster,
    title: entry.title,
    meta: entry.meta,
    onclick:
      entry.kind === "movie"
        ? `openDetail('movie', ${entry.id}, '${jsString(entry.title)}')`
        : `openDetail('show', '${entry.library}', '${jsString(entry.title)}')`,
    watched:
      entry.kind === "movie"
        ? watchedState.items.has(entry.id)
        : watchedState.shows.has(showKey(entry.library, entry.title)),
  });
}

function continueCard(entry) {
  return card({
    poster: entry.poster,
    title: entry.title,
    meta: entry.meta,
    percent: entry.percent,
    onclick: `playItem(${entry.id}, '${jsString(entry.title)}', { from: ${entry.position} })`,
    onRemove: `removeFromContinue(${entry.id}, this)`,
  });
}

function shelfBlock(label, sub, items, builder, emptyNote) {
  if (!items.length && !emptyNote) return "";
  return `
    <section class="shelf-block">
      <h2>${esc(label)}</h2>
      <p class="shelf-sub">${esc(sub)}</p>
      <div class="shelf">${
        items.map(builder).join("") || `<p class="shelf-empty">${esc(emptyNote)}</p>`
      }</div>
    </section>`;
}

function renderTabShelves(el, library, data) {
  el.innerHTML =
    [
      shelfBlock("Continue Watching", "Pick up where you left off", data.continue, continueCard, null),
      shelfBlock(`New in ${esc(library.label)}`, "Most recently added", data.new, detailCard,
            "Nothing indexed yet."),
      shelfBlock("Top 10", "Highest rated right now", data.top10, detailCard,
            "Nothing rated yet — run an artwork sync once titles are indexed."),
      shelfBlock("Recommended", "One pick per genre", data.recommended, detailCard,
            "Nothing to recommend yet."),
    ].join("") || '<p class="shelf-empty">Nothing indexed in this library yet.</p>';
}

// The landing page: Continue Watching across every library, then one
// "Recently added in X" row per accessible library — Plex's Home, not any
// one library's own tab. A library with nothing indexed yet just has no row,
// rather than an empty one taking up space next to real shelves.
function renderHomeShelves(el, data) {
  el.innerHTML =
    [
      shelfBlock("Continue Watching", "Pick up where you left off", data.continue, continueCard, null),
      ...data.shelves.map((s) =>
        shelfBlock(`Recently added in ${esc(s.label)}`, "Newest first", s.items, detailCard, null)
      ),
    ].join("") ||
    '<p class="shelf-empty">Nothing indexed yet — map a library in Settings to get started.</p>';
}

async function loadCollections() {
  const res = await api("/api/collections");
  const collections = await res.json();
  collectionsGrid.innerHTML =
    collections
      .map((c) =>
        card({
          poster: c.poster,
          title: c.name,
          meta: `${c.count} movies`,
          onclick: `openCollection('${jsString(c.name)}')`,
          watched: watchedState.collections.has(c.name),
        })
      )
      .join("") ||
    '<p class="shelf-empty">No collections yet — they come from Radarr, so run a rescan once it has matched your films.</p>';
}

async function openCollection(name) {
  currentCollection = name;
  document.getElementById("collectionTitle").textContent = name;
  const res = await api(`/api/collection?name=${encodeURIComponent(name)}`);
  const movies = await res.json();
  collectionGrid.innerHTML = movies
    .map((m) =>
      card({
        poster: `/api/poster/item/${m.id}`,
        title: m.title,
        meta: m.year || "",
        onclick: `openDetail('movie', ${m.id}, '${jsString(m.title)}')`,
        watched: watchedState.items.has(m.id),
      })
    )
    .join("");
  showPanel("collection");
}

document.getElementById("backToCollections").onclick = () => activateTab("collections");

// Build the sidebar entries and their panels from the libraries this account can
// see. Called on load and again whenever Settings maps or unmaps one.
function buildLibraryNav() {
  const tabs = document.getElementById("libraryTabs");
  const panels = document.getElementById("libraryPanels");
  tabs.innerHTML = "";
  panels.innerHTML = "";

  for (const library of me.libraries) {
    const id = tabId(library.name);
    const button = document.createElement("button");
    button.className = "tab-btn";
    button.dataset.tab = id;
    button.title = library.label;
    button.innerHTML = `${navIcon(library.name)}<span class="nav-label">${esc(library.label)}</span>`;
    button.onclick = () => activateTab(id);

    // The built-in Movies library owns the Collections view, so it becomes a
    // disclosure group; every other library is a plain entry.
    if (library.name === "movies") {
      const group = document.createElement("div");
      group.className = "nav-group";
      group.id = "moviesGroup";
      button.insertAdjacentHTML(
        "beforeend",
        `<span class="nav-caret" id="moviesCaret" title="Show collections" aria-hidden="true">
           <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                stroke-linecap="round" stroke-linejoin="round"><path d="m9 6 6 6-6 6"/></svg>
         </span>`
      );
      const sub = document.createElement("div");
      sub.className = "nav-sub";
      const subButton = document.createElement("button");
      subButton.className = "tab-btn tab-sub";
      subButton.dataset.tab = "collections";
      subButton.title = "Collections";
      subButton.innerHTML =
        `<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
              stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="7" y="3" width="10" height="14" rx="1.5"/><path d="M4.5 6v13.5h13"/></svg>
         <span class="nav-label">Collections</span>`;
      subButton.onclick = () => activateTab("collections");
      sub.appendChild(subButton);
      group.append(button, sub);
      tabs.appendChild(group);
      wireMoviesCaret();
    } else {
      tabs.appendChild(button);
    }

    const panel = document.createElement("section");
    panel.id = id;
    panel.className = "tab-panel";
    panel.innerHTML = `<div id="hero-${id}" class="tab-hero-slot"></div><div id="shelves-${id}"></div>`;
    panels.appendChild(panel);
  }
}

// One loader for every library, whatever its kind — a hero (the featured
// title) plus Continue Watching / New / Top 10 / Recommended, all scoped to
// this one library. See /api/tab/{library} in main.py.
async function loadLibrary(library) {
  const heroEl = document.getElementById(`hero-${tabId(library.name)}`);
  const shelvesEl = document.getElementById(`shelves-${tabId(library.name)}`);
  if (!heroEl || !shelvesEl) return;
  const res = await api(`/api/tab/${encodeURIComponent(library.name)}`);
  const data = await res.json();
  renderTabHero(heroEl, data.hero);
  renderTabShelves(shelvesEl, library, data);
}

// Home's hero rotates through a small pool (top rated movies + series, see
// /api/home) instead of showing one fixed pick. One timer at a time — cleared
// before a new one starts, so a re-fetch (after finishing a play, a rescan)
// can't stack a second interval advancing the same element twice as fast.
let homeHeroTimer = null;

function renderHomeHero(el, heroes, rotateMs) {
  clearInterval(homeHeroTimer);
  homeHeroTimer = null;
  if (!heroes.length) {
    renderTabHero(el, null);
    return;
  }
  let index = 0;
  renderTabHero(el, heroes[index]);
  if (heroes.length > 1) {
    homeHeroTimer = setInterval(() => {
      index = (index + 1) % heroes.length;
      renderTabHero(el, heroes[index]);
    }, rotateMs || 15000);
  }
}

// Home: same hero+shelves shape as loadLibrary(), but /api/home already spans
// every accessible library rather than being scoped to one.
async function loadHome() {
  const heroEl = document.getElementById("hero-home");
  const shelvesEl = document.getElementById("shelves-home");
  if (!heroEl || !shelvesEl) return;
  const res = await api("/api/home");
  const data = await res.json();
  renderHomeHero(heroEl, data.heroes || [], data.hero_rotate_ms);
  renderHomeShelves(shelvesEl, data);
}

// The description+cast block every detail page shows. `kind` decides both
// which endpoint answers and what the primary button ends up doing.
function castRow(cast) {
  if (!cast || !cast.length) return "";
  return `
    <div class="detail-section">
      <h4>Cast</h4>
      <div class="cast-row">${cast
        .map(
          (c) => `
        <div class="cast-member">
          ${
            c.image
              ? `<img src="${esc(c.image)}" alt="" loading="lazy" onerror="this.remove()">`
              : '<div class="cast-avatar-fallback"></div>'
          }
          <div class="cast-name">${esc(c.name || "")}</div>
          ${c.character ? `<div class="cast-character">${esc(c.character)}</div>` : ""}
        </div>`
        )
        .join("")}</div>
    </div>`;
}

// Bumped by every openDetail(), same idea as playback's sourceGeneration: a
// fast second click before the first fetch lands must not paint stale detail
// over the newer selection.
let detailGeneration = 0;

// A movie/episode's own file; a show has none, so its "Play" jumps straight
// to the first episode instead — the episode list below has the id for it.
async function openDetail(kind, idOrLibrary, title) {
  const generation = ++detailGeneration;
  // Only capture where "Back" goes the first time — a re-render from
  // refreshWatched(), or opening a "More like this" card while already on
  // this page, must not overwrite it with "detail" itself.
  if (!document.getElementById("detail").classList.contains("active")) {
    detailReturnTab = activeTab();
  }
  currentDetail = { kind, idOrLibrary, title };
  showPanel("detail");
  window.scrollTo(0, 0);

  const bg = placeholderGradient(title);
  document.getElementById("detailBackdrop").style.background = bg;
  document.getElementById("detailPosterFrame").style.background = bg;
  document.getElementById("detailPosterImg").src =
    kind === "show" ? showPoster(idOrLibrary, title) : `/api/poster/item/${idOrLibrary}`;
  document.getElementById("detailKind").textContent = "";
  document.getElementById("detailPageTitle").textContent = title;
  document.getElementById("detailMetaRow").innerHTML = "";
  document.getElementById("detailSynopsis").textContent = "Loading…";
  for (const id of ["detailActions", "detailEpisodes", "detailCast", "detailSimilar", "detailFacts", "detailFormats"]) {
    document.getElementById(id).innerHTML = "";
  }

  const url =
    kind === "show"
      ? `/api/detail/show/${idOrLibrary}/${encodeURIComponent(title)}`
      : `/api/detail/item/${idOrLibrary}`;
  let detail = null;
  try {
    const res = await api(url);
    detail = res.ok ? await res.json() : null;
  } catch {
    detail = null;                  // the page degrades to poster + title, not a dead end
  }
  if (generation !== detailGeneration) return;

  document.getElementById("detailKind").textContent =
    libraryLabel(detail?.library ?? (kind === "show" ? idOrLibrary : ""));
  document.getElementById("detailSynopsis").textContent =
    detail?.overview || "No description available.";

  const metaParts = [];
  if (kind === "movie" && detail?.year) metaParts.push(detail.year);
  if (kind === "movie" && detail?.runtime) metaParts.push(`${detail.runtime} min`);
  if (kind === "show" && detail?.episode_count != null) {
    metaParts.push(`${detail.episode_count} episode${detail.episode_count === 1 ? "" : "s"}`);
  }
  if (detail?.genres?.length) metaParts.push(detail.genres[0]);
  if (detail?.rating != null) metaParts.push(`★ ${Number(detail.rating).toFixed(1)}`);
  document.getElementById("detailMetaRow").innerHTML =
    metaParts.map(esc).join(' <span class="dot">/</span> ');

  // Episodes — fetched before the actions row, since Play (for a show) needs
  // to know the first one.
  let episodes = [];
  if (kind === "show") {
    try {
      const res = await api(`/api/shows/${encodeURIComponent(title)}/episodes?library=${idOrLibrary}`);
      episodes = res.ok ? await res.json() : [];
    } catch {
      episodes = [];
    }
    if (generation !== detailGeneration) return;
  }

  const listTarget =
    kind === "show"
      ? { kind: "show", library: idOrLibrary, ref: title }
      : { kind: "movie", library: detail?.library || "movies", ref: String(idOrLibrary) };
  const inList =
    kind === "show"
      ? myListState.shows.has(showKey(listTarget.library, listTarget.ref))
      : myListState.items.has(idOrLibrary);

  const firstEpisode = episodes[0] || null;
  const playLabel = kind === "show"
    ? (firstEpisode ? `Play ${episodeLabel(firstEpisode) || "Episode 1"}` : "Play")
    : "Play";
  document.getElementById("detailActions").innerHTML =
    `<button type="button" id="detailPlayBtn">▶ ${esc(playLabel)}</button>` +
    `<button type="button" id="detailListBtn" class="secondary">${inList ? "✓ In My List" : "+ My List"}</button>`;
  document.getElementById("detailPlayBtn").onclick = () => {
    if (kind === "show") {
      if (firstEpisode) playItem(firstEpisode.id, firstEpisode.title || episodeLabel(firstEpisode));
    } else {
      playItem(idOrLibrary, title);
    }
  };
  document.getElementById("detailListBtn").onclick = async function () {
    this.disabled = true;
    const nowIn = await toggleMyList(listTarget.kind, listTarget.library, listTarget.ref);
    this.disabled = false;
    this.textContent = nowIn ? "✓ In My List" : "+ My List";
  };

  // Episode rows — shows/anime only. One season showing at a time, picked
  // from a dropdown rather than every season stacked on one page: a 25-season
  // show like a long-running anime made the old scroll-past-everything layout
  // unusable. The dropdown itself is built once; only the rows underneath it
  // are replaced on change, so picking a season doesn't lose the select's own
  // state or re-wire its listener.
  if (kind === "show" && episodes.length) {
    const bySeason = new Map();
    for (const e of episodes) {
      const key = e.season == null ? null : e.season;
      if (!bySeason.has(key)) bySeason.set(key, []);
      bySeason.get(key).push(e);
    }
    const seasonKeys = [...bySeason.keys()].filter((k) => k !== null).sort((a, b) => a - b);
    if (bySeason.has(null)) seasonKeys.push(null);
    const seasonLabel = (season) => (season === null ? "Other episodes" : `Season ${season}`);

    const episodeRow = (e, list) => {
      const label = episodeLabel(e);
      const thumb = episodeStill(e);
      const num = String(e.episode ?? list.indexOf(e) + 1).padStart(2, "0");
      return `
        <div class="episode-row${episodes.indexOf(e) === 0 ? " first-episode" : ""}"
             onclick="playItem(${e.id}, '${jsString(e.title || label)}')">
          <div class="num">${num}</div>
          <div class="thumb" style="${
            thumb ? `background-image:url('${thumb}')` : `background:${placeholderGradient(e.title || label || String(e.id))}`
          }"></div>
          <div class="body">
            <div class="ep-title">${esc(label ? `${label} · ${e.title || ""}` : e.title || "")}</div>
            <p class="blurb">${esc(e.overview || "")}</p>
          </div>
          <div class="dur">${e.duration_seconds ? timecode(e.duration_seconds) : ""}</div>
        </div>`;
    };

    const renderSeasonRows = (season) => {
      const list = bySeason.get(season) || [];
      document.getElementById("detailSeasonRows").innerHTML =
        `<div class="episode-rows">${list.map((e) => episodeRow(e, list)).join("")}</div>`;
      document.getElementById("detailSeasonCount").textContent =
        `${list.length} episode${list.length === 1 ? "" : "s"}`;
    };

    document.getElementById("detailEpisodes").innerHTML = `
      <div class="detail-section-head">
        <select class="season-select" id="detailSeasonSelect">
          ${seasonKeys
            .map((s) => `<option value="${s === null ? "" : s}">${esc(seasonLabel(s))}</option>`)
            .join("")}
        </select>
        <span class="count" id="detailSeasonCount"></span>
      </div>
      <div id="detailSeasonRows"></div>`;

    // Whichever season the first episode overall belongs to — the same one
    // the "Play"/"Play S01E01" button above already targets — so the season
    // shown by default is always the one Play would actually start playing.
    const defaultSeason = firstEpisode ? firstEpisode.season ?? null : seasonKeys[0];
    const select = document.getElementById("detailSeasonSelect");
    select.value = defaultSeason === null ? "" : String(defaultSeason);
    select.onchange = () => renderSeasonRows(select.value === "" ? null : Number(select.value));
    renderSeasonRows(defaultSeason);
  }

  document.getElementById("detailCast").innerHTML = castRow(detail?.cast);

  const similar = detail?.similar || [];
  if (similar.length) {
    document.getElementById("detailSimilar").innerHTML = `
      <div class="detail-section-head"><h3>More like this</h3></div>
      <div class="grid grid-similar">${similar
        .map((it) =>
          card({
            poster: it.poster,
            title: it.title,
            meta: it.meta,
            onclick:
              it.kind === "show"
                ? `openDetail('show', '${it.library}', '${jsString(it.title)}')`
                : `openDetail('movie', ${it.id}, '${jsString(it.title)}')`,
          })
        )
        .join("")}</div>`;
  }

  const facts = detail?.facts || [];
  document.getElementById("detailFacts").innerHTML = facts.length
    ? `<div class="label">Details</div>${facts
        .map((f) => `<div class="detail-fact-row"><span class="k">${esc(f.k)}</span><span class="v">${esc(f.v)}</span></div>`)
        .join("")}`
    : "";

  const formats = detail?.formats || [];
  document.getElementById("detailFormats").innerHTML = formats.length
    ? `<div class="label">Available in</div><div class="format-chips">${formats
        .map((f) => `<span class="format-chip">${esc(f)}</span>`)
        .join("")}</div>`
    : "";
}

document.getElementById("detailBack").onclick = () => activateTab(detailReturnTab || firstTab());

// The cheap pass: index what changed rather than everything. The full rescan
// next to it re-parses every file, which is what you want after a rename.
document.getElementById("checkBtn").onclick = async () => {
  const btn = document.getElementById("checkBtn");
  const done = (label) => {
    btn.textContent = label;
    btn.disabled = false;
    // Leave the result up long enough to read, then go back to the label.
    setTimeout(() => { btn.textContent = "Check for Changes"; }, 4000);
  };
  btn.disabled = true;
  btn.textContent = "Checking...";
  const res = await api("/api/scan/changes", { method: "POST" });
  const { totals } = await res.json();
  if (!totals.changed) {
    done(totals.skipped.length ? "No changes (some skipped)" : "No changes");
    return;
  }
  if (totals.added) {
    btn.textContent = "Fetching posters...";
    await api("/api/artwork/sync", { method: "POST" });
  }
  await loadWatched();
  await loadLibraries();
  done(`+${totals.added} added, -${totals.removed} removed`);
};

document.getElementById("scanBtn").onclick = async () => {
  const btn = document.getElementById("scanBtn");
  btn.textContent = "Scanning...";
  await api("/api/scan", { method: "POST" });
  btn.textContent = "Fetching posters...";
  await api("/api/artwork/sync", { method: "POST" });
  await loadWatched();
  await loadLibraries();
  btn.textContent = "Rescan Library";
};

// Typing "breaking bad" fired eleven searches across the whole index. Wait for
// a pause instead — long enough to swallow a fast typist, short enough that it
// still feels like it is searching as you type.
const SEARCH_DEBOUNCE_MS = 250;
let searchTimer = null;
const searchInput = document.getElementById("searchInput");

searchInput.addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runSearch(e.target.value.trim()), SEARCH_DEBOUNCE_MS);
});

// The pills reuse /api/discover's own genre list — same source Browse's chips
// come from, so a genre only ever shows up here if there is actually
// something in it. Loaded once per visit to the tab, not on every keystroke.
async function loadSearchPills() {
  const res = await api("/api/discover");
  const { genres } = await res.json();
  document.getElementById("searchPills").innerHTML = genres
    .map((g) => `<div class="search-pill" onclick="searchFor('${jsString(g)}')">${esc(g)}</div>`)
    .join("");
}

function searchFor(q) {
  searchInput.value = q;
  runSearch(q);
}

async function runSearch(q) {
  const grid = document.getElementById("searchGrid");
  const countEl = document.getElementById("searchCount");
  if (!q) {
    grid.innerHTML = "";
    countEl.textContent = "";
    return;
  }
  const res = await api(`/api/search?q=${encodeURIComponent(q)}`);
  const results = await res.json();
  countEl.textContent = `${results.length} result${results.length === 1 ? "" : "s"} for "${q}"`;
  grid.innerHTML =
    results
      .map((r) =>
        card({
          poster: r.poster,
          title: r.title,
          meta: r.meta,
          onclick:
            r.kind === "show"
              ? `openDetail('show', '${r.library}', '${jsString(r.title)}')`
              : `openDetail('movie', ${r.id}, '${jsString(r.title)}')`,
          watched:
            r.kind === "movie"
              ? watchedState.items.has(r.id)
              : watchedState.shows.has(showKey(r.library, r.title)),
        })
      )
      .join("") || '<p class="shelf-empty">Nothing matched that search.</p>';
}

// Account chip + sign out
let me = null;

// ---------- Live: who is watching right now ----------

// Fast enough that someone hitting play shows up while you are still looking at
// the screen, slow enough to be nothing next to the video it is reporting on.
const LIVE_POLL_MS = 5000;
let livePoll = null;

// "1:24:07" or "44:53" — the hour only appears once there is one.
function clock(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const parts = [Math.floor(total / 60) % 60, total % 60];
  if (total >= 3600) parts.unshift(Math.floor(total / 3600));
  return parts
    .map((n, i) => (i === 0 ? String(n) : String(n).padStart(2, "0")))
    .join(":");
}

// "32 minutes", "2 hours" — how long they have had it open.
function since(seconds) {
  if (seconds < 90) return `${Math.max(seconds, 1)} sec`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

function syncLivePolling(tab) {
  const wanted = tab === "watching" && me && me.role === "admin";
  if (wanted && !livePoll) {
    loadWatching();
    livePoll = setInterval(loadWatching, LIVE_POLL_MS);
  } else if (!wanted && livePoll) {
    clearInterval(livePoll);
    livePoll = null;
  }
}

// A background tab polling every five seconds is pure waste; pick it back up on
// return, and refresh immediately rather than waiting out the interval.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (livePoll) {
      clearInterval(livePoll);
      livePoll = null;
    }
  } else {
    syncLivePolling(activeTab());
  }
});

function watchingRow(entry) {
  const known = entry.percent !== null && entry.percent !== undefined;
  // The position is inferred from what the player asked the server for, not
  // reported by it, so it is shown as an approximation rather than a fact.
  const where = known
    ? `~${clock(entry.position)}${entry.duration ? ` / ${clock(entry.duration)}` : ""} · ${Math.round(entry.percent)}%`
    : "position unknown";
  const label = entry.mode === "direct" ? "Direct Play" : entry.mode;
  return `
    <div class="live-row">
      <img class="live-poster" src="${entry.poster}" loading="lazy" alt=""
           onerror="this.style.visibility='hidden'">
      <div class="live-body">
        <div class="live-title">
          <span>${esc(entry.title)}</span>
          <span class="live-mode${entry.mode === "direct" ? "" : " transcoding"}">${esc(label)}</span>
        </div>
        ${entry.subtitle ? `<div class="live-sub">${esc(entry.subtitle)}</div>` : ""}
        <div class="live-who">
          <strong>${esc(entry.username)}</strong>
          ${entry.client ? ` · ${esc(entry.client)}` : ""}
          · watching for ${since(entry.watching_for)}
        </div>
        <div class="live-bar"><span style="width: ${known ? Math.max(entry.percent, 0.8) : 0}%"></span></div>
        <div class="live-pos">${esc(where)}</div>
      </div>
    </div>`;
}

async function loadWatching() {
  const list = document.getElementById("watchingList");
  const res = await api("/api/watching");
  if (!res.ok) {
    list.innerHTML = '<p class="shelf-empty">The live view is for administrators.</p>';
    return;
  }
  const { watching } = await res.json();
  document.getElementById("watchingSub").textContent = watching.length
    ? `${watching.length} ${watching.length === 1 ? "person is" : "streams are"} playing right now.`
    : "Who is playing something right now.";
  list.innerHTML =
    watching.map(watchingRow).join("") ||
    '<p class="shelf-empty">Nobody is watching anything right now.</p>';
}

async function loadAccount() {
  const res = await api("/api/auth/me");
  me = await res.json();
  if (!me.profile) {
    // Signed in, but nobody has picked who's watching yet.
    window.location.href = "/profiles";
    return false;
  }
  document.getElementById("whoami").innerHTML =
    `${esc(me.profile.name)}${me.role === "admin" ? '<span class="role">admin</span>' : ""}`;
  const libraryNames = me.libraries.map((l) => l.label).join(", ") || "no libraries";
  document.getElementById("accountSummary").textContent =
    `Signed in as ${me.username} (${me.role}), watching as ${me.profile.name}. `
    + `Library access: ${libraryNames}.`;
  renderTwoFactor();

  document.getElementById("navProfileInitial").textContent =
    (me.profile.name.trim().charAt(0) || "?").toUpperCase();
  document.getElementById("navProfileName").textContent = me.profile.name;

  // User management and rescanning are admin-only; the API enforces both, this
  // just avoids showing controls that would only ever return 403.
  document.getElementById("liveTab").hidden = me.role !== "admin";
  document.getElementById("usersPanel").hidden = me.role !== "admin";
  document.getElementById("librariesPanel").hidden = me.role !== "admin";
  document.getElementById("appearancePanel").hidden = me.role !== "admin";
  document.getElementById("checkBtn").hidden = me.role !== "admin";
  document.getElementById("scanBtn").hidden = me.role !== "admin";
  if (me.role === "admin") {
    renderNewUserLibraries();
    loadUsers();
    loadLibrariesPanel();
    loadAppearancePanel();
    browseTo("/");
  }
  return true;
}

// A library's display name, for anything that only has the slug in hand.
const libraryLabel = (name) =>
  (me.all_libraries || me.libraries).find((l) => l.name === name)?.label || name;

// Access is applied by rebuilding: an ungranted library has no tab and no panel
// at all, because /api/auth/me only ever lists the ones this account can see.
function applyLibraryAccess() {
  buildLibraryNav();
  // Access can be revoked while someone is signed in, so the tab they were last
  // on may have just disappeared from under them. On first load there is no
  // active tab yet — land on whatever the URL names (a refresh, a bookmark,
  // a shared link), falling back to Home for a stale or unknown path.
  const active = document.querySelector(".tab-btn.active");
  if (!active || active.closest("[hidden]")) activateTab(tabFromUrl() || firstTab());
}

async function loadLibraries() {
  const granted = me.libraries.map((l) => l.name);
  await Promise.all([
    loadHome(),
    ...me.libraries.map((library) => loadLibrary(library)),
    granted.includes("movies") ? loadCollections() : null,
  ]);
}

async function loadUsers() {
  const res = await api("/api/users");
  const users = await res.json();
  document.getElementById("userRows").innerHTML = users
    .map(
      (u) => `
    <tr>
      <td>${esc(u.username)}</td>
      <td><span class="pill ${u.role === "admin" ? "pill-admin" : ""}">${esc(u.role)}</span></td>
      <td>${accessCell(u)}</td>
      <td>${twoFactorCell(u)}</td>
      <td class="muted-cell">${esc((u.created_at || "").split(" ")[0])}</td>
      <td>${
        u.username === me.username
          ? '<span class="muted-cell">that\'s you</span>'
          : `<button class="link-danger" onclick="deleteUser('${esc(u.username)}')">Remove</button>`
      }</td>
    </tr>`
    )
    .join("");
}

// Enrolment is not something an admin can do for someone — only undo, so the
// person can do it again on a phone they still have.
function twoFactorCell(user) {
  const reset = `<button class="link-danger" onclick="resetTwoFactor('${esc(user.username)}')">Reset</button>`;
  if (!user.two_factor) {
    return `<span class="pill pill-warn">Not set up</span>
            <div class="muted-cell">Enrols at next sign-in</div>`;
  }
  return `<span class="pill pill-ok">On</span> ${reset}
          <div class="muted-cell">${user.backup_codes_left} backup ${
            user.backup_codes_left === 1 ? "code" : "codes"} left</div>`;
}

async function resetTwoFactor(username) {
  const mine = username === me.username;
  if (!confirm(
    `Reset two-factor authentication for "${username}"?\n\n` +
    "It signs them out everywhere and voids their backup codes. They set up an " +
    "authenticator again at their next sign-in, using their existing password." +
    (mine ? "\n\nThis is your own account — you will be signed out now." : ""))) return;

  const res = await api(`/api/users/${encodeURIComponent(username)}/2fa`, { method: "DELETE" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Could not reset that user's two-factor authentication");
    return;
  }
  if (mine) {
    window.location.href = "/login";
    return;
  }
  loadUsers();
}

// Access is editable in place — ticking a box saves straight away, and applies
// on that user's next request rather than at their next sign-in.
function accessCell(user) {
  const granted = new Set(user.libraries);
  const admin = user.role === "admin";
  return `<div class="lib-choices${admin ? " lib-choices-locked" : ""}"${
    admin ? ' title="Admins always have access to every library"' : ""
  }>${me.all_libraries
    .map(
      (library) => `
      <label class="lib-choice">
        <input type="checkbox" value="${esc(library.name)}" ${granted.has(library.name) ? "checked" : ""}
               ${admin ? "disabled" : ""}
               onchange="saveUserLibraries('${esc(user.username)}', this)">
        ${esc(library.label)}
      </label>`
    )
    .join("")}</div>`;
}

async function saveUserLibraries(username, changed) {
  const cell = changed.closest(".lib-choices");
  const libraries = [...cell.querySelectorAll("input")].filter((i) => i.checked).map((i) => i.value);
  const res = await api(`/api/users/${encodeURIComponent(username)}/libraries`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ libraries }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Could not change that user's library access");
    changed.checked = !changed.checked;      // put the box back
  }
}

async function deleteUser(username) {
  if (!confirm(`Remove the user "${username}"? Any session they have is revoked immediately.`)) return;
  const res = await api(`/api/users/${encodeURIComponent(username)}`, { method: "DELETE" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Could not remove that user");
    return;
  }
  loadUsers();
}

// ---- Settings > Libraries -------------------------------------------------

async function loadLibrariesPanel() {
  const res = await api("/api/libraries");
  const libraries = await res.json();
  document.getElementById("libraryRows").innerHTML = libraries
    .map(
      (l) => `
    <tr>
      <td>${esc(l.label)}${l.builtin ? '<span class="pill">built in</span>' : ""}</td>
      <td class="muted-cell">${esc(l.path)}${
        l.exists ? "" : '<span class="pill pill-warn">missing</span>'
      }</td>
      <td class="muted-cell">${l.kind === "movie" ? "Movies" : "Shows"}</td>
      <td class="muted-cell">${l.items}</td>
      <td>${
        l.builtin
          ? ""
          : `<button class="link-danger" onclick="deleteLibrary('${esc(l.name)}')">Unmap</button>`
      }</td>
    </tr>`
    )
    .join("");
}

async function deleteLibrary(name) {
  if (
    !confirm(
      `Unmap "${name}"? Its index and everyone's access to it are removed. ` +
        `The files on disk are not touched.`
    )
  )
    return;
  const res = await api(`/api/libraries/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Could not unmap that library");
    return;
  }
  await refreshLibraries();
}

// A mapping change moves the sidebar, the grids and the access checkboxes, so
// everything that reads the library list has to be rebuilt.
async function refreshLibraries() {
  await loadAccount();
  applyLibraryAccess();
  await loadLibraries();
}

// ---- folder picker -------------------------------------------------------

let browsePath = "/";
let browseParent = null;

async function browseTo(path) {
  const res = await api(`/api/browse?path=${encodeURIComponent(path)}`);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    showMessage(document.getElementById("addLibraryMsg"), body.detail || "Cannot open that folder", false);
    return;
  }

  browsePath = body.path;
  browseParent = body.parent;
  document.getElementById("browserPath").textContent = body.path;
  document.getElementById("browseUp").disabled = !body.parent;

  const rows = body.entries
    .map(
      (entry) => `
      <div class="browser-row">
        <button type="button" class="browser-open" onclick="browseTo('${esc(entry.path).replace(/'/g, "\\'")}')">
          <span class="browser-folder">${esc(entry.name)}</span>
        </button>
        ${
          entry.mapped
            ? `<span class="pill">${esc(entry.mapped)}</span>`
            : `<button type="button" class="browser-pick"
                 onclick="pickFolder('${esc(entry.path).replace(/'/g, "\\'")}')">Use this</button>`
        }
      </div>`
    )
    .join("");

  const here =
    body.path === "/" || body.mapped_as
      ? ""
      : `<div class="browser-row browser-here">
           <span class="muted-cell">this folder</span>
           <button type="button" class="browser-pick" onclick="pickFolder('${esc(body.path).replace(/'/g, "\\'")}')">Use this</button>
         </div>`;

  document.getElementById("browserList").innerHTML =
    here + rows || '<p class="shelf-empty">No sub-folders here.</p>';
}

// Picking fills in the rest of the form: the folder name becomes the library
// name, and the layout is guessed from what is inside it. Both stay editable.
async function pickFolder(path) {
  const res = await api(`/api/browse?path=${encodeURIComponent(path)}`);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) return;

  document.getElementById("newLibraryPath").value = path;
  const label = document.getElementById("newLibraryLabel");
  if (!label.value.trim()) label.value = body.suggested_label || "";
  document.getElementById("newLibraryKind").value = body.suggested_kind || "show";
  document.getElementById("addLibraryBtn").disabled = false;
  document.getElementById("addLibraryBtn").textContent = `Map ${path}`;
  showMessage(
    document.getElementById("addLibraryMsg"),
    `Selected ${path} — looks like ${body.suggested_kind === "show" ? "shows" : "films"}.`,
    true
  );
}

document.getElementById("browseUp").onclick = () => browseTo(browseParent || "/");

document.getElementById("addLibraryForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const msg = document.getElementById("addLibraryMsg");
  showMessage(msg, "Mapping and indexing…", true);

  const res = await api("/api/libraries", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      label: document.getElementById("newLibraryLabel").value.trim(),
      path: document.getElementById("newLibraryPath").value,
      kind: document.getElementById("newLibraryKind").value,
    }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    showMessage(msg, body.detail || "Could not map that library", false);
    return;
  }
  showMessage(msg, `Mapped ${body.label} — ${body.items} items indexed.`, true);
  event.target.reset();
  document.getElementById("addLibraryBtn").disabled = true;
  document.getElementById("addLibraryBtn").textContent = "Map library";
  await refreshLibraries();
  browseTo(browsePath);          // the folder just mapped now shows as taken
});

function showMessage(el, text, ok) {
  el.textContent = text;
  el.className = `form-msg ${ok ? "ok" : "bad"}`;
}

document.getElementById("addUserForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const msg = document.getElementById("addUserMsg");
  const password = document.getElementById("newUserPassword").value;
  if (password.length < 8) {
    showMessage(msg, "Password must be at least 8 characters", false);
    return;
  }

  const res = await api("/api/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: document.getElementById("newUsername").value.trim(),
      password,
      role: document.getElementById("newUserRole").value,
      libraries: newUserLibraries(),
    }),
  });
  const body = await res.json().catch(() => ({}));

  if (!res.ok) {
    showMessage(msg, body.detail || "Could not add that user", false);
    return;
  }
  const access = body.libraries.map(libraryLabel).join(", ") || "no libraries";
  showMessage(msg, `Added ${body.username} as ${body.role}, with access to ${access}.`, true);
  event.target.reset();
  syncNewUserLibraries();
  loadUsers();
});

function renderNewUserLibraries() {
  document.getElementById("newUserLibraries").innerHTML = (me.all_libraries || [])
    .map(
      (library) => `
      <label class="lib-choice">
        <input type="checkbox" value="${esc(library.name)}" checked> ${esc(library.label)}
      </label>`
    )
    .join("");
}

const newUserLibraryBoxes = () =>
  [...document.querySelectorAll("#newUserLibraries input")];

const newUserLibraries = () =>
  newUserLibraryBoxes().filter((box) => box.checked).map((box) => box.value);

// An admin always gets everything, so the picker follows the role rather than
// letting someone tick a restriction that would not be honoured.
function syncNewUserLibraries() {
  const admin = document.getElementById("newUserRole").value === "admin";
  newUserLibraryBoxes().forEach((box) => {
    box.disabled = admin;
    if (admin) box.checked = true;
  });
  document.getElementById("newUserLibraries").classList.toggle("lib-choices-locked", admin);
  document.getElementById("newUserLibrariesNote").textContent = admin
    ? "Admins always have access to every library."
    : "Only the ticked libraries appear in their sidebar.";
}

document.getElementById("newUserRole").addEventListener("change", syncNewUserLibraries);

// ---- Settings > Two-factor ------------------------------------------------
// Two actions share one flow: confirm the password, then either show a new QR
// or hand back a fresh set of backup codes. `twoFactorIntent` remembers which.
let twoFactorIntent = null;

const twoFactorPanels = {
  actions: document.getElementById("twoFactorActions"),
  password: document.getElementById("twoFactorPasswordForm"),
  setup: document.getElementById("twoFactorSetupForm"),
  codes: document.getElementById("twoFactorCodes"),
};

function showTwoFactor(name) {
  for (const [key, element] of Object.entries(twoFactorPanels)) element.hidden = key !== name;
}

function renderTwoFactor() {
  const state = me.two_factor || {};
  const left = state.backup_codes_left ?? 0;
  document.getElementById("twoFactorSummary").textContent =
    `Set up on ${(state.confirmed_at || "").split(" ")[0]}. ` +
    `${left} unused backup ${left === 1 ? "code" : "codes"}.`;
  showTwoFactor("actions");
}

function startTwoFactor(intent) {
  twoFactorIntent = intent;
  document.getElementById("twoFactorPassword").value = "";
  showMessage(document.getElementById("twoFactorMsg"), "", true);
  showTwoFactor("password");
  document.getElementById("twoFactorPassword").focus();
}

document.getElementById("moveAuthBtn").onclick = () => startTwoFactor("move");
document.getElementById("newCodesBtn").onclick = () => startTwoFactor("codes");
document.getElementById("twoFactorCancel").onclick = () => showTwoFactor("actions");
document.getElementById("twoFactorSetupCancel").onclick = () => {
  // The pending secret is left behind on the server, but it is not the
  // confirmed one — the authenticator already on the phone still works.
  showTwoFactor("actions");
};

document.getElementById("twoFactorPasswordForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const msg = document.getElementById("twoFactorMsg");
  const password = document.getElementById("twoFactorPassword").value;
  const url = twoFactorIntent === "move" ? "/api/auth/2fa/new" : "/api/auth/2fa/backup-codes";

  const res = await api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  const body = await res.json().catch(() => ({}));
  document.getElementById("twoFactorPassword").value = "";
  if (!res.ok) {
    showMessage(msg, body.detail || "Could not confirm your password", false);
    return;
  }

  if (twoFactorIntent === "move") {
    // Server-rendered SVG from our own endpoint, not anything a user supplied.
    document.getElementById("twoFactorQr").innerHTML = body.qr;
    document.getElementById("twoFactorSecret").textContent = body.secret;
    document.getElementById("twoFactorCode").value = "";
    showMessage(document.getElementById("twoFactorSetupMsg"), "", true);
    showTwoFactor("setup");
    return;
  }
  showBackupCodes(body.backup_codes);
});

document.getElementById("twoFactorSetupForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const msg = document.getElementById("twoFactorSetupMsg");
  const res = await api("/api/auth/2fa/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: document.getElementById("twoFactorCode").value }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    showMessage(msg, body.detail || "That code is not right", false);
    return;
  }
  showBackupCodes(body.backup_codes);
});

// Confirming an authenticator always reissues the backup codes, so both paths
// end here — and both end with the panel reloaded from the server.
function showBackupCodes(codes) {
  const text = codes.join("\n");
  document.getElementById("twoFactorCodeList").textContent = text;
  document.getElementById("twoFactorDownload").href =
    "data:text/plain;charset=utf-8," + encodeURIComponent(
      "HomeReel backup codes\nEach code works once.\n\n" + text + "\n");
  showTwoFactor("codes");
}

document.getElementById("twoFactorCopy").addEventListener("click", async (event) => {
  const text = document.getElementById("twoFactorCodeList").textContent;
  // navigator.clipboard needs a secure context and this is usually plain http
  // on the LAN, so the old execCommand path is the one that actually runs.
  try {
    await navigator.clipboard.writeText(text);
  } catch (err) {
    const scratch = document.createElement("textarea");
    scratch.value = text;
    document.body.appendChild(scratch);
    scratch.select();
    document.execCommand("copy");
    scratch.remove();
  }
  event.target.textContent = "Copied";
  setTimeout(() => { event.target.textContent = "Copy"; }, 1500);
});

document.getElementById("twoFactorCodesDone").onclick = async () => {
  await loadAccount();           // refreshes the count and drops back to the buttons
  if (me.role === "admin") loadUsers();
};

document.getElementById("passwordForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const msg = document.getElementById("passwordMsg");
  const next = document.getElementById("newPassword").value;

  if (next !== document.getElementById("confirmPassword").value) {
    showMessage(msg, "The two new passwords do not match", false);
    return;
  }
  if (next.length < 8) {
    showMessage(msg, "New password must be at least 8 characters", false);
    return;
  }

  const res = await api("/api/auth/password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      current_password: document.getElementById("currentPassword").value,
      new_password: next,
    }),
  });
  const body = await res.json().catch(() => ({}));

  if (!res.ok) {
    showMessage(msg, body.detail || "Could not change the password", false);
    return;
  }
  showMessage(msg, "Password changed. Other sessions keep working — sign them out if you need to.", true);
  event.target.reset();
});

document.getElementById("logoutBtn").onclick = async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.href = "/login";
};

// Signing out of the profile alone (not the account) — the session stays
// live, so no server call is needed, just a trip to the picker.
document.getElementById("navProfileChip").onclick = () => {
  window.location.href = "/profiles";
};

// ---- Theme (per profile, falling back to the household default) ----
// The registry and the engine are static/themes.js (window.HomeReelThemes);
// this is only the Settings panel and the bit that decides which one is on.

let householdTheme = "default";
const Themes = window.HomeReelThemes;

// The profile's own pick, else the household's.
const effectiveTheme = () => me?.profile?.theme || householdTheme;

function applyEffectiveTheme() {
  if (Themes) Themes.apply(effectiveTheme());
  renderThemePanel();
}

function themeTile(theme, selected) {
  return `<button type="button" class="theme-tile" data-theme-choice="${theme.id}"
      aria-pressed="${selected}" aria-label="${esc(theme.name)} theme">
    <span class="theme-preview" data-theme="${theme.id}" aria-hidden="true">
      <span class="tp-rail"></span>
      <span class="tp-main">
        <span class="tp-title">${esc(theme.name)}</span>
        <span class="tp-cards"><i></i><i></i><i></i></span>
        <span class="tp-btn">Play</span>
      </span>
    </span>
    <span class="theme-name">${esc(theme.name)}${selected ? '<span class="theme-current">On</span>' : ""}</span>
    <span class="theme-tagline">${esc(theme.tagline)}</span>
  </button>`;
}

function renderThemePanel() {
  const gallery = document.getElementById("themeGallery");
  if (!gallery || !Themes || !me?.profile) return;
  const own = me.profile.theme;
  const active = effectiveTheme();
  gallery.innerHTML = Themes.list.map((t) => themeTile(t, t.id === active)).join("");
  document.getElementById("themeFollowHousehold").checked = !own;
  document.getElementById("themeHouseholdName").textContent =
    `(${Themes.get(householdTheme)?.name || householdTheme})`;
  const effects = document.getElementById("themeEffects");
  effects.checked = Themes.effectsEnabled() && !Themes.reducedMotion();
  effects.disabled = Themes.reducedMotion();
  document.getElementById("themeMotionNote").hidden = !Themes.reducedMotion();
}

async function saveProfileTheme(theme) {
  const msg = document.getElementById("themeMsg");
  msg.textContent = "";
  msg.className = "form-msg";
  const previous = me.profile.theme;
  me.profile.theme = theme;          // optimistic: switch first, it is only paint
  applyEffectiveTheme();
  const res = await api("/api/profiles/current/theme", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ theme }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    me.profile.theme = previous;
    applyEffectiveTheme();
    msg.textContent = body.detail || "Could not save the theme";
    msg.classList.add("bad");
  }
}

document.getElementById("themeGallery").addEventListener("click", (event) => {
  const tile = event.target.closest("[data-theme-choice]");
  if (tile) saveProfileTheme(tile.dataset.themeChoice);
});

document.getElementById("themeFollowHousehold").addEventListener("change", (event) => {
  // Unticking keeps whatever is on screen, now as the profile's own pick.
  saveProfileTheme(event.target.checked ? null : effectiveTheme());
});

document.getElementById("themeEffects").addEventListener("change", (event) => {
  Themes?.setEffects(event.target.checked);
});

// ---- Appearance (navMode / posterStyle) ----

function applySettings(settings) {
  document.body.dataset.navMode = settings.nav_mode;
  document.body.dataset.posterStyle = settings.poster_style;
  if (settings.theme) householdTheme = settings.theme;
  // From the server's DOWNLOADS_URL; the panel stays hidden without one.
  if (settings.downloads_url) document.getElementById("appsLink").href = settings.downloads_url;
  document.getElementById("appsPanel").hidden = !settings.downloads_url;
}

async function loadAppearancePanel() {
  const res = await api("/api/settings");
  const settings = await res.json();
  const themeSelect = document.getElementById("householdThemeSelect");
  themeSelect.innerHTML = (Themes?.list || [{ id: "default", name: "HomeReel" }])
    .map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  themeSelect.value = settings.theme;
  document.getElementById("navModeSelect").value = settings.nav_mode;
  document.getElementById("posterStyleSelect").value = settings.poster_style;
}

document.getElementById("appearanceForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const msg = document.getElementById("appearanceMsg");
  msg.textContent = "";
  msg.className = "form-msg";
  const body = {
    theme: document.getElementById("householdThemeSelect").value,
    nav_mode: document.getElementById("navModeSelect").value,
    poster_style: document.getElementById("posterStyleSelect").value,
  };
  const res = await api("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const settings = await res.json().catch(() => ({}));
  if (!res.ok) {
    msg.textContent = settings.detail || "Could not save appearance settings";
    msg.classList.add("bad");
    return;
  }
  applySettings(settings);
  applyEffectiveTheme();      // only changes anything here if this profile follows the household
  msg.textContent = "Saved — applied for everyone now.";
  msg.classList.add("ok");
});

(async function start() {
  // Before the nav renders, or navMode/posterStyle flash from one layout to
  // the other once this lands.
  try {
    const res = await api("/api/settings");
    applySettings(await res.json());
  } catch {
    // Settings are a preference, never a blocker — the CSS defaults (rail,
    // portrait) already match SETTINGS_DEFAULTS on the server.
  }
  const signedIn = await loadAccount();   // has to land first: it decides what to build
  if (!signedIn) return;                  // loadAccount() is already navigating to /profiles
  applyEffectiveTheme();                  // themes.js painted the cached one; this is the real one
  applyLibraryAccess();                   // builds the nav and its panels
  syncNewUserLibraries();
  await loadWatched();          // before any grid renders, or nothing is marked
  await loadMyList();           // same reason — a detail popup needs this before it can open
  loadLibraries();              // every tab's hero + shelves
})();
