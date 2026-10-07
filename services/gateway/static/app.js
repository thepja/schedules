"use strict";

// Par défaut : tram A à Christ Roi direction Lycée J. Monnet, en temps réel.
const DEFAULTS = { stop: "Christ Roi", quai: "TTR:CHRI-1T", line: "A", rt: "1" };
const REFRESH_MS = 20000;

const $ = (id) => document.getElementById(id);
const state = readUrl();
let lastData = null;
let timer = null;

function readUrl() {
  const p = new URLSearchParams(location.search);
  if (!p.has("stop")) return { ...DEFAULTS };
  return { stop: p.get("stop"), quai: p.get("quai") || "", line: p.get("line") || "", rt: p.get("rt") || "" };
}

function writeUrl() {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(state)) if (v) p.set(k, v);
  history.replaceState(null, "", "?" + p);
}

async function api(path, params) {
  const url = new URL("api/" + path, location.href);
  for (const [k, v] of Object.entries(params)) if (v !== "" && v != null) url.searchParams.append(k, v);
  const resp = await fetch(url);
  const body = await resp.json().catch(() => ({}));
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
    showNotice(e.message, true);
  }
  renderDirections(directionsData);
  await loadDepartures();
}

async function loadDepartures() {
  clearTimeout(timer);
  writeUrl();
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
    showNotice(e.message, true);
  }
  renderDepartures();
  timer = setTimeout(loadDepartures, REFRESH_MS);
}

function select(quai, line) {
  state.quai = quai;
  state.line = line;
  renderDirections(directionsData);
  loadDepartures();
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
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadDepartures(); });
setInterval(renderDepartures, 15000);  // décompte des minutes entre deux actualisations

loadStop();
