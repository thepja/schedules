"use strict";

// Par défaut : tram A à Christ Roi direction Lycée J. Monnet, en temps réel.
const DEFAULTS = { stop: "Christ Roi", quai: "TTR:CHRI-1T", line: "A", rt: "1" };
const REFRESH_MS = 20000;
const WAKE_RETRY_MS = 5000;
const WAKE_STATUSES = [502, 503, 504];
const WAKE_MESSAGE = "Démarrage des services… (jusqu'à 1 min après une période d'inactivité)";

const FAVORITES_KEY = "filbleu.favorites";

const $ = (id) => document.getElementById(id);
let favorites = loadFavorites();
const state = readUrl();
let lastData = null;
let timer = null;
let tab = new URLSearchParams(location.search).get("tab") === "trains" ? "trains" : "bus";

function readUrl() {
  const p = new URLSearchParams(location.search);
  if (!p.has("stop")) {
    // Sans paramètres : premier favori, sinon le trajet par défaut.
    const f = favorites[0];
    return f ? { stop: f.stop, quai: f.quai, line: f.line, rt: DEFAULTS.rt } : { ...DEFAULTS };
  }
  return { stop: p.get("stop"), quai: p.get("quai") || "", line: p.get("line") || "", rt: p.get("rt") || "" };
}

function writeUrl() {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(state)) if (v) p.set(k, v);
  if (tab === "trains") {
    p.set("tab", "trains");
    if (trainTo) p.set("to", trainTo);
  }
  history.replaceState(null, "", "?" + p);
}

async function api(path, params) {
  const url = new URL("api/" + path, location.href);
  for (const [k, v] of Object.entries(params)) if (v !== "" && v != null) url.searchParams.append(k, v);
  let resp;
  try {
    resp = await fetch(url);
  } catch {
    throw Object.assign(new Error(WAKE_MESSAGE), { waking: true });
  }
  const body = await resp.json().catch(() => ({}));
  if (WAKE_STATUSES.includes(resp.status)) throw Object.assign(new Error(WAKE_MESSAGE), { waking: true });
  if (!resp.ok) throw new Error(body.detail || `Erreur ${resp.status}`);
  return body;
}

// --- Rendu -------------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  node.append(...children.filter((c) => c != null && c !== false));
  return node;
}

function readableText(hex) {
  if (!/^[0-9a-f]{6}$/i.test(hex || "")) return "#fff";
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  return 0.299 * r + 0.587 * g + 0.114 * b > 0.6 ? "#111" : "#fff";
}

function badge(line, color, textColor) {
  const b = el("span", { class: "badge" }, line);
  if (/^[0-9a-f]{6}$/i.test(color || "")) {
    b.style.background = "#" + color;
    b.style.color = textColor ? "#" + textColor : readableText(color);
  }
  return b;
}

const pretty = (s) => (s || "").toLowerCase();
const hhmm = (d) => d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });

function renderDirections(data) {
  const nav = $("directions");
  nav.replaceChildren();
  if (!data || data.directions.length < 2) return;
  const all = el("button", { class: "chip all", type: "button", "aria-pressed": String(!state.quai) }, "Toutes");
  all.addEventListener("click", () => select("", ""));
  nav.append(all);
  for (const d of data.directions) {
    const shown = d.headsigns.slice(0, 2).map(pretty).join(" / ");
    const more = d.headsigns.length > 2 ? ` +${d.headsigns.length - 2}` : "";
    const active = state.quai === d.stop_id && (!state.line || state.line === d.line);
    const chip = el("button", { class: "chip", type: "button", "aria-pressed": String(active), title: d.headsigns.join(", ") },
      badge(d.line, d.color, d.text_color), el("span", { class: "dest" }, "→ " + shown + more));
    chip.addEventListener("click", () => select(d.stop_id, d.line));
    nav.append(chip);
  }
  // Rend visible la direction choisie quand la liste défile horizontalement.
  nav.querySelector('[aria-pressed="true"]')?.scrollIntoView({ block: "nearest", inline: "center" });
}

function renderDepartures() {
  const list = $("departures");
  list.replaceChildren();
  if (!lastData) return;
  const now = Date.now();
  const deps = lastData.departures.filter((d) => new Date(d.expected).getTime() >= now - 30000);
  if (!deps.length) {
    const msg = state.rt
      ? "Aucun passage suivi en temps réel pour le moment."
      : "Aucun passage prévu dans les 2 prochaines heures.";
    list.append(el("li", { class: "empty" }, msg));
    return;
  }
  for (const d of deps) {
    const expected = new Date(d.expected);
    const mins = Math.max(0, Math.floor((expected - now) / 60000));
    const when = d.canceled
      ? el("div", { class: "mins" }, hhmm(expected))
      : mins === 0
        ? el("div", { class: "now" }, "À l'approche")
        : el("div", { class: "mins" }, String(mins), el("small", {}, "min"));

    const meta = [el("span", {}, hhmm(expected))];
    if (d.canceled) meta.push(el("span", { class: "canceled-tag" }, "Supprimé"));
    else if (d.realtime) {
      meta.push(el("span", { class: "live" }, "temps réel"));
      const delay = Math.round((d.delay_seconds || 0) / 60);
      if (delay) meta.push(el("span", { class: "late" }, (delay > 0 ? "+" : "−") + Math.abs(delay) + " min"));
    } else meta.push(el("span", {}, "horaire prévu"));

    list.append(el("li", { class: "dep" + (d.canceled ? " canceled" : d.realtime ? "" : " scheduled") },
      badge(d.line, d.line_color),
      el("div", { style: "min-width:0" },
        el("div", { class: "dest" }, pretty(d.headsign) || "—"),
        el("div", { class: "meta" }, ...meta)),
      el("div", { class: "when" }, when)));
  }
}

function showNotice(text, isError = false) {
  const n = $("notice");
  n.hidden = !text;
  n.textContent = text || "";
  n.classList.toggle("error", isError);
}

// --- Favoris (stockés dans le navigateur) ------------------------------------------

function loadFavorites() {
  try {
    const list = JSON.parse(localStorage.getItem(FAVORITES_KEY) || "[]");
    return Array.isArray(list) ? list.filter((f) => f && f.stop) : [];
  } catch {
    return [];
  }
}

function saveFavorites() {
  try { localStorage.setItem(FAVORITES_KEY, JSON.stringify(favorites)); } catch { /* stockage indisponible */ }
}

const sameFavorite = (f, s) => f.stop === s.stop && (f.quai || "") === (s.quai || "") && (f.line || "") === (s.line || "");
const currentFavorite = () => favorites.find((f) => sameFavorite(f, state));

function currentDirection() {
  return directionsData?.directions.find((d) => d.stop_id === state.quai && (!state.line || d.line === state.line));
}

function toggleFavorite() {
  const existing = currentFavorite();
  if (existing) {
    favorites = favorites.filter((f) => f !== existing);
  } else {
    const d = currentDirection();
    favorites.push({
      stop: state.stop,
      quai: state.quai,
      line: state.line,
      color: d?.color || "",
      text_color: d?.text_color || "",
      direction: d ? d.headsigns.slice(0, 2).map(pretty).join(" / ") : "",
    });
  }
  saveFavorites();
  renderFavorites();
}

function openFavorite(f) {
  const sameStop = f.stop === state.stop && directionsData;
  Object.assign(state, { stop: f.stop, quai: f.quai, line: f.line });
  if (sameStop) select(f.quai, f.line);
  else loadStop();
  renderFavorites();
}

function renderFavorites() {
  const nav = $("favorites");
  nav.hidden = !favorites.length;
  nav.replaceChildren(...favorites.map((f) => {
    const subtitle = f.direction ? "→ " + f.direction : f.line ? "Ligne " + f.line : "Toutes directions";
    return el("div", { class: "fav" + (sameFavorite(f, state) ? " current" : "") },
      el("button", { class: "fav-open", type: "button", onclick: () => openFavorite(f) },
        f.line ? badge(f.line, f.color, f.text_color) : null,
        el("span", { class: "fav-text" }, el("strong", {}, f.stop), el("small", { class: f.direction ? "cap" : "" }, subtitle))),
      el("button", {
        class: "fav-remove", type: "button", title: "Retirer des favoris", "aria-label": `Retirer ${f.stop} des favoris`,
        onclick: () => { favorites = favorites.filter((x) => x !== f); saveFavorites(); renderFavorites(); },
      }, "×"));
  }));
  const isFav = !!currentFavorite();
  const star = $("fav-toggle");
  star.textContent = isFav ? "★" : "☆";
  star.setAttribute("aria-pressed", String(isFav));
  star.title = isFav ? "Retirer des favoris" : "Ajouter aux favoris";
}

// --- Chargement ----------------------------------------------------------------

let directionsData = null;

async function loadStop() {
  $("stop-name").textContent = state.stop;
  $("rt-only").checked = !!state.rt;
  try {
    directionsData = await api("stops/directions", { q: state.stop });
    state.stop = directionsData.name;
    $("stop-name").textContent = directionsData.name;
    document.title = `${directionsData.name} — Fil Bleu`;
  } catch (e) {
    directionsData = null;
    showNotice(e.message, !e.waking);
    if (e.waking) {
      setTimeout(loadStop, WAKE_RETRY_MS);
      return;
    }
  }
  renderDirections(directionsData);
  renderFavorites();
  await loadDepartures();
}

async function loadDepartures() {
  clearTimeout(timer);
  if (tab !== "bus") return;
  writeUrl();
  let delay = REFRESH_MS;
  try {
    lastData = await api("departures", {
      stop: state.quai || state.stop,
      line: state.line,
      realtime_only: state.rt ? "true" : "",
      limit: 12,
    });
    showNotice(lastData.realtime_available ? "" : "Temps réel indisponible : horaires prévus uniquement.");
    $("updated").textContent = "Mis à jour à " + hhmm(new Date());
  } catch (e) {
    showNotice(e.message, !e.waking);
    if (e.waking) delay = WAKE_RETRY_MS;
  }
  renderDepartures();
  timer = setTimeout(loadDepartures, delay);
}

function select(quai, line) {
  state.quai = quai;
  state.line = line;
  renderDirections(directionsData);
  renderFavorites();
  loadDepartures();
}

// --- Trains au départ de la gare de Tours ---------------------------------------------

let trainsData = null;
let trainsTimer = null;
let trainTo = new URLSearchParams(location.search).get("to") || "";
const TRAIN_FAVORITES_KEY = "filbleu.trainDestinations";
let trainFavorites = loadTrainFavorites();
let destinations = null;  // gares desservies, chargées à la première recherche

function loadTrainFavorites() {
  try {
    const list = JSON.parse(localStorage.getItem(TRAIN_FAVORITES_KEY) || "[]");
    return Array.isArray(list) ? list.filter((x) => typeof x === "string" && x) : [];
  } catch {
    return [];
  }
}

function saveTrainFavorites() {
  try { localStorage.setItem(TRAIN_FAVORITES_KEY, JSON.stringify(trainFavorites)); } catch { /* stockage indisponible */ }
}

const norm = (s) => (s || "").normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();
// Même règle que le service : « Paris » couvre « Paris Austerlitz », « Paris Montparnasse… ».
const cityMatch = (q, name) => { const a = norm(q), b = norm(name); return b === a || b.startsWith(a + " ") || b.startsWith(a + "-"); };
const isTrainFavorite = (to) => trainFavorites.some((f) => norm(f) === norm(to));

function setTrainTo(to) {
  trainTo = to;
  renderTrainDestinations();
  trainsData = null;
  renderTrains();
  loadTrains();
}

function toggleTrainFavorite() {
  if (!trainTo) return;
  trainFavorites = isTrainFavorite(trainTo)
    ? trainFavorites.filter((f) => norm(f) !== norm(trainTo))
    : [...trainFavorites, trainTo];
  saveTrainFavorites();
  renderTrainDestinations();
}

function renderTrainDestinations() {
  $("to-title").hidden = !trainTo;
  $("to-title").textContent = trainTo ? "→ " + trainTo : "";
  const star = $("to-fav");
  star.hidden = !trainTo;
  const fav = isTrainFavorite(trainTo);
  star.textContent = fav ? "★" : "☆";
  star.setAttribute("aria-pressed", String(fav));
  star.title = fav ? "Retirer la destination des favoris" : "Ajouter la destination aux favoris";

  const nav = $("to-chips");
  nav.replaceChildren();
  if (!trainFavorites.length && !trainTo) return;
  nav.append(el("button", { class: "chip all", type: "button", "aria-pressed": String(!trainTo), onclick: () => setTrainTo("") },
    "Toutes destinations"));
  for (const f of trainFavorites) {
    nav.append(el("button", { class: "chip all", type: "button", "aria-pressed": String(norm(f) === norm(trainTo)), onclick: () => setTrainTo(f) },
      "★ " + f,
      el("span", {
        class: "x", role: "button", title: "Retirer des favoris", "aria-label": `Retirer ${f} des favoris`,
        onclick: (e) => {
          e.stopPropagation();
          trainFavorites = trainFavorites.filter((x) => x !== f);
          saveTrainFavorites();
          renderTrainDestinations();
        },
      }, " ×")));
  }
}

function showTrainsNotice(text, isError = false) {
  const n = $("trains-notice");
  n.hidden = !text;
  n.textContent = text || "";
  n.classList.toggle("error", isError);
}

function modeClass(mode) {
  const m = mode.toLowerCase();
  return m.includes("tgv") || m.includes("ouigo") ? "tgv" : m.includes("car") ? "car" : "";
}

function renderTrains() {
  const list = $("trains");
  list.replaceChildren();
  if (!trainsData) return;
  const now = Date.now();
  const deps = trainsData.departures.filter((d) => new Date(d.expected).getTime() >= now - 60000);
  if (!deps.length) {
    list.append(el("li", { class: "empty" }, trainTo
      ? `Aucun train vers ${trainTo} dans les 16 prochaines heures.`
      : "Aucun départ dans les 4 prochaines heures."));
    return;
  }
  for (const d of deps) {
    const expected = new Date(d.expected);
    const scheduled = d.scheduled ? new Date(d.scheduled) : expected;
    const delay = Math.round((d.delay_seconds || 0) / 60);
    const mins = Math.max(0, Math.floor((expected - now) / 60000));
    const day = dayLabel(scheduled);

    const time = d.realtime && !d.canceled && delay > 0
      ? el("div", { class: "t-time" }, day, el("s", {}, hhmm(scheduled)), el("span", { class: "late" }, hhmm(expected)))
      : el("div", { class: "t-time" }, day, el("strong", {}, hhmm(scheduled)));

    let status;
    if (d.canceled) status = el("span", { class: "canceled-tag" }, "Supprimé");
    else if (!d.realtime) status = el("span", { class: "muted" }, "Prévu");
    else if (delay > 0) status = el("span", { class: "late" }, `+${delay} min`);
    else status = el("span", { class: "live on-time" }, "À l'heure");

    const label = [d.mode, d.number].filter(Boolean).join(" ");
    list.append(el("li", { class: "dep train" + (d.canceled ? " canceled" : "") },
      time,
      el("div", { style: "min-width:0" },
        el("div", { class: "dest" }, d.destination || "—"),
        el("div", { class: "meta" }, label ? el("span", { class: "mode " + modeClass(d.mode) }, label) : null,
          d.arrival
            ? el("span", { class: "arrival" }, `arrivée ${hhmm(new Date(d.arrival))}` +
                (norm(d.arrival_stop) !== norm(d.destination) && norm(d.arrival_stop) !== norm(trainTo)
                ? ` · ${d.arrival_stop}` : ""))
            : d.via.length ? el("span", { class: "via" }, "via " + d.via.slice(0, 3).join(", ") + (d.via.length > 3 ? "…" : "")) : null)),
      el("div", { class: "t-status" }, status,
        d.canceled ? null : el("span", { class: "in" }, mins === 0 ? "maintenant" : mins < 120 ? `dans ${mins} min` : `dans ${Math.floor(mins / 60)} h ${String(mins % 60).padStart(2, "0")}`))));
  }
}

async function loadTrains() {
  clearTimeout(trainsTimer);
  if (tab !== "trains") return;
  let delay = REFRESH_MS;
  try {
    writeUrl();
    trainsData = await api("trains/departures", { limit: 20, to: trainTo });
    $("station-name").textContent = "Gare de " + trainsData.station;
    showTrainsNotice(trainsData.realtime_available ? "" : "Temps réel SNCF indisponible : horaires prévus uniquement.");
    $("trains-updated").textContent = "Mis à jour à " + hhmm(new Date());
  } catch (e) {
    showTrainsNotice(e.message, !e.waking);
    if (e.waking) delay = WAKE_RETRY_MS;
  }
  renderTrains();
  trainsTimer = setTimeout(loadTrains, delay);
}

function dayLabel(date) {
  const today = new Date();
  const tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
  if (date.toDateString() === today.toDateString()) return null;
  const label = date.toDateString() === tomorrow.toDateString()
    ? "Demain"
    : date.toLocaleDateString("fr-FR", { weekday: "short" });
  return el("span", { class: "day" }, label);
}

// Recherche de destination : suggestions filtrées localement.
const toInput = $("to-input");
const toBox = $("to-suggestions");
let toActive = -1;

function closeToSuggestions() { toBox.hidden = true; toActive = -1; }

function pickDestination(name) {
  toInput.value = "";
  closeToSuggestions();
  toInput.blur();
  setTrainTo(name);
}

toInput.addEventListener("input", async () => {
  const q = toInput.value.trim();
  if (q.length < 2) return closeToSuggestions();
  if (!destinations) {
    try { destinations = await api("trains/destinations", {}); } catch { return; }
  }
  const nq = norm(q);
  const stations = destinations.filter((n) => norm(n).includes(nq)).slice(0, 12);
  // Une ville à plusieurs gares (« Paris ») est proposée en premier.
  const city = destinations.filter((n) => cityMatch(q, n)).length > 1 ? q.charAt(0).toUpperCase() + q.slice(1) : null;
  const options = [
    ...(city ? [[city, `${city} (toutes les gares)`]] : []),
    ...stations.map((n) => [n, n]),
  ];
  toBox.replaceChildren(...options.map(([value, label]) =>
    el("li", { role: "option", "data-value": value, onmousedown: (e) => { e.preventDefault(); pickDestination(value); } }, label)));
  if (!options.length) toBox.append(el("li", { "aria-disabled": "true" }, "Aucune gare desservie depuis Tours"));
  toBox.hidden = false;
  toActive = -1;
});

toInput.addEventListener("keydown", (e) => {
  const items = [...toBox.querySelectorAll('li[role="option"]')];
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!items.length) return;
    toActive = (toActive + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items.forEach((li, i) => li.setAttribute("aria-selected", String(i === toActive)));
  } else if (e.key === "Escape") closeToSuggestions();
});

$("to-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const items = [...toBox.querySelectorAll('li[role="option"]')];
  const choice = items[toActive] || items[0];
  if (choice) pickDestination(choice.dataset.value);
});
toInput.addEventListener("blur", () => setTimeout(closeToSuggestions, 100));
$("to-fav").addEventListener("click", toggleTrainFavorite);

// --- Onglets ---------------------------------------------------------------------

let busLoaded = false;

function switchTab(next) {
  tab = next;
  for (const t of ["bus", "trains"]) {
    $("tab-" + t).setAttribute("aria-selected", String(t === tab));
    $("view-" + t).hidden = t !== tab;
  }
  $("search-form").hidden = tab !== "bus";
  writeUrl();
  if (tab === "trains") {
    clearTimeout(timer);
    document.title = "Trains au départ de Tours";
    loadTrains();
  } else {
    clearTimeout(trainsTimer);
    if (busLoaded) loadDepartures();
    else { busLoaded = true; loadStop(); }
  }
}

// --- Recherche d'arrêt -------------------------------------------------------------

const input = $("search");
const box = $("suggestions");
let searchSeq = 0;
let active = -1;

function closeSuggestions() { box.hidden = true; active = -1; }

function pickStop(name) {
  Object.assign(state, { stop: name, quai: "", line: "" });
  input.value = "";
  closeSuggestions();
  input.blur();
  loadStop();
}

input.addEventListener("input", async () => {
  const q = input.value.trim();
  const seq = ++searchSeq;
  if (q.length < 2) return closeSuggestions();
  let results = [];
  try { results = await api("stops/search", { q }); } catch { /* ignoré */ }
  if (seq !== searchSeq) return;
  box.replaceChildren(...results.map((r) =>
    el("li", { role: "option", onmousedown: (e) => { e.preventDefault(); pickStop(r.name); } }, r.name)));
  if (!results.length) box.append(el("li", { "aria-disabled": "true" }, "Aucun arrêt trouvé"));
  box.hidden = false;
  active = -1;
});

input.addEventListener("keydown", (e) => {
  const items = [...box.querySelectorAll('li[role="option"]')];
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!items.length) return;
    active = (active + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items.forEach((li, i) => li.setAttribute("aria-selected", String(i === active)));
  } else if (e.key === "Escape") closeSuggestions();
});

$("search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const items = [...box.querySelectorAll('li[role="option"]')];
  const choice = items[active] || items[0];
  if (choice) pickStop(choice.textContent);
});
input.addEventListener("blur", () => setTimeout(closeSuggestions, 100));

$("rt-only").addEventListener("change", (e) => { state.rt = e.target.checked ? "1" : ""; loadDepartures(); });
$("refresh").addEventListener("click", loadDepartures);
$("fav-toggle").addEventListener("click", toggleFavorite);
// Favoris modifiés dans un autre onglet.
window.addEventListener("storage", (e) => {
  if (e.key === FAVORITES_KEY) { favorites = loadFavorites(); renderFavorites(); }
  if (e.key === TRAIN_FAVORITES_KEY) { trainFavorites = loadTrainFavorites(); renderTrainDestinations(); }
});
$("trains-refresh").addEventListener("click", loadTrains);
$("tab-bus").addEventListener("click", () => switchTab("bus"));
$("tab-trains").addEventListener("click", () => switchTab("trains"));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) (tab === "bus" ? loadDepartures : loadTrains)();
});
setInterval(() => (tab === "bus" ? renderDepartures : renderTrains)(), 15000);  // décompte des minutes

renderFavorites();
renderTrainDestinations();
switchTab(tab);
