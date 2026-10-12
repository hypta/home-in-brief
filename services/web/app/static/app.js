// Page flow: address search -> PropertyContext and map -> rules and knowledge-base notes -> render -> summary -> questions.
// Each request fails on its own: a failed call only affects the block it feeds.
import {$, byId, esc, floodMapped, link, render, renderSummary, renderThread, renderWait, ticked, typeAnswer} from "./report.js";
import {drawMap} from "./map.js";

// kb: null means the notes could not be loaded; inputs: what the rules on the page were calculated with
const CLEARED = {rules: null, rulesError: null, inputs: null, kb: [], report: null, reportError: null};
const state = {ctx: null, ...CLEARED, sources: {}, banks: {}, thread: [], done: new Set(), first: true};  // done: the next steps ticked for this address; first: the first report in this tab
let timer, lastTyped = "", searchSeq = 0, lookupSeq = 0, calcSeq = 0, rulesSources, kbIds, kbItems;
let asked = {};  // the summary last asked for: its key and the pending or settled answer
let chosen = null;  // the address picked from the list; null once the box has been typed in again
// year_built and cladding: the value a web search put into the form. While the form still holds it, that value is sent
// and shown as found on the web and not checked; once the visitor changes it, it is their own.
let found = {};
const FROM_SEARCH = "Filled in from a web search, not checked. Change or clear it if it is wrong.";

const num = id => $(id).value.trim() === "" ? null : Number($(id).value);  // a figure typed into the form, or null
const show = id => { $(id).scrollIntoView(); $(id).focus({preventScroll: true}); };
// The form is open until the first report is on the page; after that it is folded behind "Change" and the readout says what was used.
function details(open) {
  $("details").hidden = !open; $("change").textContent = open ? "Close" : "Change"; $("change").setAttribute("aria-expanded", open);
}

// What a council property category says about the build date, to show the visitor how it was read
// (LINZ Rating Valuations Rules 2008, Table 21). The web service does the same reading before it asks rules-api.
const categoryBuilt = text => {
  const m = /^(?:R[ACDFHNR]|LI)(\d{3}|XXX)[ABC]?$/i.exec(text.trim()), code = m && m[1].toUpperCase();
  return !m ? "" : code === "XXX" ? "in more than one period" : code === "191" ? "before 1914" : code === "192" ? "between 1914 and 1929"
    : code >= "193" && code <= "209" ? `between ${code}0 and ${code}9` : "";
};

async function api(path, body) {
  const res = await fetch("/api/" + path, body && {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await res.json().catch(() => null);
  if (!res.ok) throw Object.assign(new Error(data?.error?.message || "HTTP " + res.status), {code: data?.error?.code, fields: data?.error?.fields || []});
  return data;
}

// One summary request for a lookup and a set of inputs: asking again with nothing changed reuses it, in flight or
// answered. A failed one is asked again, which is what "Try again" does.
async function summarise() {
  const mine = calcSeq, {inputs} = state, address_id = state.ctx.address.linz_address_id, key = JSON.stringify([address_id, state.ctx.generated_at, inputs]);
  if (key !== asked.key) asked = {key, answer: api("report", {address_id, user_inputs: inputs})};
  const {answer} = asked;
  renderSummary(Object.assign(state, {report: null, reportError: null}));
  let update;
  try { update = {report: await answer}; }
  catch (e) { update = {reportError: e}; if (asked.answer === answer) asked = {}; }
  if (mine === calcSeq) renderSummary(Object.assign(state, update));
}

async function calculate() {
  const mine = ++calcSeq, cv = state.ctx.property.cv, rules = inputs => api("rules", {property_context: state.ctx, user_inputs: inputs});
  const chat_supplied = {year_built: $("year-built").value.trim() || null, cladding: $("cladding").value || null};
  const searched = Object.keys(found).filter(k => found[k] === chat_supplied[k]);
  const home = {chat_supplied, ...(searched.length && {chat_supplied_source: Object.fromEntries(searched.map(k => [k, "web_search"]))})};
  try { sessionStorage.setItem("found", JSON.stringify(found)); } catch {}
  const built = categoryBuilt($("year-built").value);
  $("year-built-read").textContent = built ? `Read as a council property category: built ${built}.` : searched.includes("year_built") ? FROM_SEARCH : "";
  $("cladding-read").textContent = searched.includes("cladding") ? FROM_SEARCH : "";
  const price = num("price");
  const update = {...CLEARED};
  let inputs = {price_override: cv.status === "found" && price === cv.value ? null : price, deposit: num("deposit"),
    household_income: num("income"), existing_debts: num("debts"), rate_pct: num("rate"), household_type: $("household").value || null, ...home};
  try {
    update.rules = await rules(inputs);
  } catch (e) {
    update.rulesError = Object.assign(e, {fields: e.fields || []});
    // rejected loan inputs (a negative amount, say) must not blank the property notes: ask again without them
    if (e.fields.length) update.rules = await rules(inputs = home).catch(() => null);
  }
  if (update.rules) {
    const ids = update.rules.kb_items.join(",");
    const [kb, extra] = await Promise.all([
      !ids ? [] : ids === kbIds ? kbItems : api("kb/items?ids=" + encodeURIComponent(ids)).catch(() => null),
      (rulesSources ??= api("sources").then(byId)).catch(() => (rulesSources = null, {}))]);
    if (kb) [kbIds, kbItems] = [ids, kb];
    Object.assign(update, {kb, inputs, sources: {...extra, ...byId(state.ctx.sources)}});
  }
  if (mine !== calcSeq) return;
  render(Object.assign(state, update));
  if (update.rules) summarise();
}

// The sheet with the chosen address and the first step, while its records are read.
function wait(a, failed) {
  $("report").hidden = $("readout-row").hidden = false; $("report").classList.add("waiting");
  $("change").disabled = !failed;  // nothing to change while the records are on their way; after a failure the form's button asks again
  renderWait(a.full_address, failed, {deposit: num("deposit"), household_income: num("income"), existing_debts: num("debts")});
  if (!failed) show("matched");
}

// The report for the records in state goes on the page.
function reveal() {
  const mine = lookupSeq, {ctx} = state;
  $("status").textContent = "The report is below."; $("report").hidden = $("readout-row").hidden = false; $("report").classList.remove("waiting");
  $("go").textContent = "Update report"; $("change").disabled = false;
  // explanations are open, and our margin notes on show, for the first report in a tab only
  try { state.first = !sessionStorage.getItem("seen"); sessionStorage.setItem("seen", "1"); } catch {}
  document.body.dataset.seen = state.first ? "0" : "1";
  $("sec-hazards").open = floodMapped(ctx).length > 0;  // the map is on show when a flood layer is mapped here
  render(state);
  show("matched");
  drawMap(() => api("map?address_id=" + ctx.address.linz_address_id), () => mine === lookupSeq);
  calculate();
}

// While the form is open, an address picked from the list only fills the box: nothing is asked for until the button is
// pressed, so the visitor can add details first. Picked with the form folded away, behind a report, it is looked up at once.
function choose(a) {
  const before = chosen?.address_id ?? state.ctx?.address.linz_address_id;
  clearTimeout(timer); ++searchSeq;  // drop any address search still pending
  $("suggestions").innerHTML = ""; $("q").value = a.full_address; chosen = a;
  if (before != null && String(before) !== String(a.address_id)) { $("price").value = $("year-built").value = $("cladding").value = ""; found = {}; }  // facts about the home before this one
  if ($("details").hidden) lookup(a);
  else $("status").textContent = `Add your details below if you like, then choose ${$("go").textContent}.`;
}

async function lookup(a) {
  const mine = ++lookupSeq;
  ++calcSeq;  // drop a calculation for the address before this one
  details(false); wait(a);
  $("status").textContent = "Reading the council and LINZ records…";
  try {
    const ctx = await api(`property-context?address_id=${a.address_id}&input=${encodeURIComponent(lastTyped)}`);
    if (mine !== lookupSeq) return;  // a newer lookup owns the page
    Object.assign(state, CLEARED, {ctx, sources: byId(ctx.sources), thread: [], done: new Set()});  // earlier answers were about another report
    try { sessionStorage.setItem("address", JSON.stringify({address_id: a.address_id, full_address: a.full_address})); } catch {}
    const cv = ctx.property.cv, site = id => { const l = ctx.links.find(x => x.id === id); return l ? link(l.url, l.label) : id; };
    const typed = $("price").value !== "";  // a price typed before the records arrived is the visitor's
    if (!typed) $("price").value = cv.status === "found" ? cv.value : "";
    $("price-hint").innerHTML = (cv.status !== "found" ? "The council valuation could not be found, so enter a price. " : typed ? "The figure you entered. "
      : `Filled in with the council valuation (CV) as at ${esc(cv.valuation_date)}, which is not a market value. `) + `Check a current estimate on ${site("homes")}, ${site("oneroof")} or the listing, then change this figure.`;
    $("year-built-hint").innerHTML = `Only used when the council records do not give a build decade. Check the listing, ${site("homes")} or ${site("oneroof")} for the decade, or copy the Property Category from the ${site("council_valuation")}. A year (1997), a decade (1990s) or the category code (for example RD199B) all work.`;
    $("cladding-hint").innerHTML = `Check the listing, ${site("homes")} or ${site("oneroof")} for it.`;
    reveal();
  } catch (e) {
    if (mine !== lookupSeq) return;
    state.ctx = null; $("status").textContent = "Lookup failed: " + e.message;
    wait(a, true);
  }
}

// Loan details the visitor typed stay for this tab (sessionStorage), so they are entered once and used for every
// address checked in it. Only fields inside the form reach this listener: the price and the two facts about the home
// belong to one address. Storage can be switched off in the browser; the page works without it.
try { for (const field of $("loan-form").elements) field.value = sessionStorage.getItem("loan." + field.id) || field.value; } catch {}
$("loan-form").addEventListener("change", e => { try { sessionStorage.setItem("loan." + e.target.id, e.target.value); } catch {} });
$("summary").addEventListener("click", e => { if (e.target.id === "retry-summary") summarise(); });
// a tick survives the summary being written again for the same address
$("summary").addEventListener("change", e => {
  if (!e.target.matches(".todo input")) return;
  state.done[e.target.checked ? "add" : "delete"](e.target.closest("label").textContent);
  $("ticked").textContent = ticked($("summary").querySelectorAll(".todo input:checked").length);
});

// A year built or cladding stated in a question, or found by a web search, goes into the form when the report on the
// page was not calculated with it, unless the records already have that fact: the rules then use the record, and
// report-api's answer says how the two compare. `source` says which values a search found. Returns what was put into
// the form: the field, the value in words, and whether a search found it.
function fill({year_built, cladding}, source = {}) {
  const added = [], option = [...$("cladding").options].find(o => o.value === cladding), p = state.ctx.property;
  const recorded = f => f.status === "found" && f.value != null, used = state.inputs?.chat_supplied || {};
  const put = (field, value, text) => {
    const web = source[field] === "web_search";
    $(field.replace("_", "-")).value = value; added.push({field, text, web});
    if (web) found[field] = String(value); else delete found[field];
  };
  if (year_built != null && !recorded(p.year_built) && String(used.year_built ?? "") !== String(year_built)) put("year_built", year_built, "year built " + year_built);
  if (option && !recorded(p.cladding) && used.cladding !== cladding) put("cladding", cladding, "cladding " + option.text);
  return added;
}

// One question about the report on the page, sent with the last 3 answered turns. A turn that failed is replaced by the
// next one asked. When the answer adds a fact about the home to the form, the report is calculated again with it first,
// so the notes the answer cites are on the page; the turn keeps those notes, whatever the report is changed to later.
// With `search_field`, the turn is the button that searches the web for one field the records lack: report-api is sent
// the field in place of a question, and answers in the language of the last answer in the conversation.
async function ask(question, search_field) {
  if ($("ask").disabled) return;
  const turn = {question, search_field}, answered = state.thread.filter(t => t.answer);
  const said = a => (a.web?.text || a.answer.map(s => s.text).join(" ")).slice(0, 2000);
  const history = answered.slice(-3).flatMap(t => [{role: "user", text: t.question}, {role: "assistant", text: said(t.answer)}]);
  state.thread = [...answered, turn];
  renderThread(state);
  try {
    const asked = search_field ? {search_field, language: answered.at(-1)?.answer.language || "en"} : {question};
    const answer = await api("followup", {address_id: state.ctx.address.linz_address_id, user_inputs: state.inputs, history, ...asked});
    if (!Array.isArray(answer?.answer)) throw new Error("not an answer");
    turn.answer = answer;
  } catch (e) { turn.error = e; }
  if (!state.thread.includes(turn)) return;  // another address owns the page
  if (turn.answer) {
    turn.added = fill(turn.answer.user_supplied || {}, turn.answer.user_supplied_source);
    if (turn.added.length) await calculate();
    turn.kb = state.kb;
  }
  renderThread(state);
  if (turn.answer) typeAnswer();
}
// The question panel is a bar at the bottom of a narrow screen that opens upward over the page; beside the report on a wide one it is always open.
const wide = matchMedia("(min-width: 1000px)");
function panel(open) {
  $("ask-panel").classList.toggle("open", open); $("handle").setAttribute("aria-expanded", open); $("handle").setAttribute("aria-label", open ? "Hide the answers" : "Show the answers");
}
$("handle").addEventListener("click", () => panel(!$("ask-panel").classList.contains("open")));
$("question").addEventListener("focus", () => panel(true));
addEventListener("keydown", e => { if (e.key === "Escape") panel(false); });
document.addEventListener("click", e => {
  const to = e.target.closest('a[href^="#sec-"]'), starter = e.target.closest(".starters button"), search = e.target.closest("[data-search]");
  if (to) { $(to.hash.slice(1)).open = true; if (!wide.matches) panel(false); }  // a link to a folded section opens it
  if (starter) ask(starter.textContent);
  if (search && !$("ask").disabled) { panel(true); ask("Search the web for the " + search.dataset.search.replace("_", " "), search.dataset.search); }
  if (e.target.closest('a[href="#details"]')) { e.preventDefault(); if (!wide.matches) panel(false); details(true); show("details"); }
});
$("ask-form").addEventListener("submit", e => {
  e.preventDefault();
  const question = $("question").value.trim();
  if (question && !$("ask").disabled) { $("question").value = ""; ask(question); }  // the box is free for the next question; a failed turn keeps its own "Try again"
});
$("thread").addEventListener("click", e => {
  const last = state.thread.at(-1);
  if (e.target.id === "retry-answer") ask(last.question, last.search_field);
  if (!e.target.dataset.remove) return;
  // "Remove": what a web search added leaves the form, unless the visitor has changed it since, and the report is calculated without it
  const fields = e.target.dataset.remove.split(",");
  for (const field of fields) {
    const input = $(field.replace("_", "-"));
    if (input.value.trim() === found[field]) input.value = "";
    delete found[field];
  }
  for (const turn of state.thread) for (const a of turn.added || []) if (a.web && fields.includes(a.field)) a.removed = true;
  renderThread(state); calculate();
});

$("loan-form").addEventListener("submit", e => {
  e.preventDefault();
  // with nothing picked from the list since, the button is about the report on the page
  if (chosen && String(state.ctx?.address.linz_address_id) !== String(chosen.address_id)) lookup(chosen);  // no report for this address yet
  else if (state.ctx) { calculate(); details(false); show("matched"); }
  else { $("status").textContent = "Choose an address from the list first."; $("q").focus(); }
});
$("change").addEventListener("click", () => details($("details").hidden));
// The theme follows the system until the visitor picks the other one; picking the system's own goes back to following it.
$("theme").addEventListener("click", () => {
  const root = document.documentElement, system = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  const next = (root.dataset.theme || system) === "dark" ? "light" : "dark";
  if (next === system) delete root.dataset.theme; else root.dataset.theme = next;
  try { next === system ? localStorage.removeItem("theme") : localStorage.setItem("theme", next); } catch {}
});

// Bank names, and the default interest rate: the middle of the banks' published 1-year standard rates.
api("rates/latest").then(snap => {
  state.banks = Object.fromEntries(snap.banks.map(b => [b.bank_id, b.name]));
  if (state.ctx) render(state);
  const quoted = snap.banks.filter(b => b.standard?.["1y"] != null), rates = quoted.map(b => b.standard["1y"]).sort((a, b) => a - b);
  if (!rates.length || $("rate").value) return;
  const mid = rates.length >> 1, median = Math.round((rates.length % 2 ? rates[mid] : (rates[mid - 1] + rates[mid]) / 2) * 100) / 100;
  const oldest = quoted.map(b => (b.captured_at || "").slice(0, 10)).filter(Boolean).sort()[0];
  $("rate").value = median;
  $("rate-hint").textContent = `Filled in with ${median}%, the middle of ${rates.length} banks' published 1-year standard rates (collected by hand${oldest ? "; the oldest is from " + oldest : ""}). Change it to a rate you have been offered.`;
}).catch(() => {});  // without it the rate stays blank and banks show by id

// The address on the page is kept for the tab, with what a web search put into the form: after a reload, or on the way
// back from another page, the same report is read again. The browser brings back what was typed into the form; the
// conversation is not kept (a search answer is only stored while it is on the page).
try {
  found = JSON.parse(sessionStorage.getItem("found")) || {};
  const kept = JSON.parse(sessionStorage.getItem("address"));
  if (kept) { $("q").value = kept.full_address; chosen = kept; lookup(kept); }
} catch {}

$("q").addEventListener("input", () => {
  clearTimeout(timer);
  if ($("q").value !== chosen?.full_address) chosen = null;  // the box no longer names the address that was picked
  const q = $("q").value.trim();
  if (q.length < 3) { ++searchSeq; $("suggestions").innerHTML = ""; return; }
  timer = setTimeout(async () => {
    const mine = ++searchSeq; lastTyped = q; $("status").textContent = "Searching addresses…";
    try {
      const found = await api("addresses?q=" + encodeURIComponent(q));
      if (mine !== searchSeq) return;
      $("status").textContent = found.length ? "" : "No Auckland address starts with that text.";
      $("suggestions").innerHTML = "";
      for (const a of found) {
        const li = document.createElement("li"), b = document.createElement("button");
        b.type = "button"; b.textContent = a.full_address; b.onclick = () => choose(a);
        li.append(b); $("suggestions").append(li);
      }
    } catch (e) { if (mine === searchSeq) $("status").textContent = "Address search failed: " + e.message; }
  }, 400);
});
