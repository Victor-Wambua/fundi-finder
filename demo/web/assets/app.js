// Fundi Finder demo. The page is thin: every rule is answered by the real Python modules
// running in assets/worker.js (Pyodide). This file renders results and sends actions.

const $ = (selector, root = document) => root.querySelector(selector);
const main = $("#main");
const live = $("#live");

// ---------- templating (everything dynamic is escaped) ----------

class Raw {
  constructor(text) {
    this.text = text;
  }
}
const esc = (value) =>
  String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const part = (value) =>
  value instanceof Raw
    ? value.text
    : Array.isArray(value)
      ? value.map(part).join("")
      : value == null || value === false
        ? ""
        : esc(value);
const html = (strings, ...values) => new Raw(strings.reduce((out, s, i) => out + s + (i < values.length ? part(values[i]) : ""), ""));

// ---------- formatting (times are shown in East Africa Time) ----------

const TZ = "Africa/Nairobi";
const timeFormat = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: TZ });
const dateFormat = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: TZ });
const fmtTime = (iso) => (iso ? timeFormat.format(new Date(iso)).replace(",", "") : "");
const fmtDate = (iso) => (iso ? dateFormat.format(new Date(iso)) : "");
const kes = (n) => "KES " + Number(n).toLocaleString("en-US");
const count = (n, one, many = one + "s") => `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
const spaced = (code) => `${code.slice(0, 3)} ${code.slice(3)}`;
// A value for <input type="datetime-local"> in East Africa Time (UTC+3), from a UTC instant in ms.
const eatInput = (ms) => new Date(ms + 3 * 3600 * 1000).toISOString().slice(0, 16);
const eatToIso = (value) => new Date(`${value}:00+03:00`).toISOString();

function announce(text) {
  live.textContent = "";
  setTimeout(() => {
    live.textContent = text;
  }, 30);
}

// ---------- the Python engine (in a worker) ----------

const engine = {
  worker: null,
  waiting: new Map(),
  seq: 0,
  onProgress: () => {},
  start() {
    this.worker = new Worker("assets/worker.js");
    this.worker.onmessage = ({ data }) => {
      if (data.type === "progress") return this.onProgress(data.text);
      const slot = this.waiting.get(data.id);
      if (!slot) return;
      this.waiting.delete(data.id);
      if (data.ok) slot.resolve(data.data);
      else slot.reject(new Error(data.error));
    };
    this.worker.onerror = (event) => {
      for (const slot of this.waiting.values()) slot.reject(new Error(event.message || "The Python worker stopped."));
      this.waiting.clear();
    };
  },
  send(message) {
    return new Promise((resolve, reject) => {
      const id = ++this.seq;
      this.waiting.set(id, { resolve, reject });
      this.worker.postMessage({ id, ...message });
    });
  },
  boot: (saved) => engine.send({ type: "boot", saved }),
  // Returns {ok, data} or {ok:false, error, kind}. Rule refusals are normal answers, not exceptions.
  async call(name, args = {}) {
    try {
      return await this.send({ type: "call", name, args });
    } catch (error) {
      return { ok: false, error: error.message, kind: "bug" };
    }
  },
  exportState: () => engine.send({ type: "export" }),
  reset: () => engine.send({ type: "reset" }),
};

// ---------- state kept in this browser only ----------

const STORE_KEY = "fundi-finder-demo-state-v3";
const store = {
  works: true,
  read() {
    try {
      return localStorage.getItem(STORE_KEY);
    } catch {
      this.works = false;
      return null;
    }
  },
  write(text) {
    try {
      localStorage.setItem(STORE_KEY, text);
    } catch {
      this.works = false;
    }
  },
  clear() {
    try {
      localStorage.removeItem(STORE_KEY);
    } catch {
      this.works = false;
    }
  },
};
let persistTimer = null;
function persist() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(async () => {
    try {
      store.write(await engine.exportState());
    } catch {
      /* the demo still works without saving */
    }
  }, 60);
}

const S = { cat: null, now: null, info: null, manifest: null, flash: {}, filters: null };

async function must(name, args) {
  const out = await engine.call(name, args);
  if (!out.ok) throw new Error(out.error);
  if (out.data && out.data.now) S.now = out.data.now;
  return out.data;
}

// ---------- shared pieces ----------

const MSG_ICON = { ok: "ok", bad: "bad", info: "" };
function msg(kind, text, why = "") {
  return html`<p class="msg ${MSG_ICON[kind] || ""}" role="${kind === "bad" ? "alert" : "status"}" tabindex="-1"><strong>${text}</strong>${why ? html`<span class="why">${why}</span>` : ""}</p>`;
}

function tryThis() {
  return html`<aside class="try" aria-labelledby="try-h">
    <h2 id="try-h">Try this</h2>
    <ol>
      <li><strong>Search.</strong> <a href="#/?trade=Plumber&amp;area=Kilimani&amp;verified=1">Plumbers in Kilimani, verified only</a>. Then find Peter Njoroge: one 5-star review does not outrank a long record.</li>
      <li><strong>Book.</strong> Open a profile and request a quote. In <a href="#/bookings">My bookings</a> the fundi's side sends the price and your side accepts it.</li>
      <li><strong>Confirm.</strong> In My bookings, Joseph Mwangi has marked a job done. Type six wrong digits, then use the code on screen.</li>
      <li><strong>Break the rule.</strong> On that job, try to post a review before confirming. After confirming, enter the code again or review twice.</li>
      <li><strong>Wait.</strong> Five wrong codes lock entry for an hour. Skip the demo clock ahead to lift the lock, or 31 days to close the review window.</li>
    </ol>
    <p class="note">Everything is invented. Changes stay in this browser. <button type="button" class="linkish" data-reset>Reset demo data</button></p>
  </aside>`;
}

const badgeList = (card) => html`<ul class="facts" aria-label="Verification">${card.badges.length ? card.badges.map((b) => html`<li class="have">${b.label}</li>`) : html`<li class="muted">No verification badges yet</li>`}</ul>`;

// ---------- find a fundi ----------

function filtersFromQuery(q) {
  return { trade: q.get("trade") || "", area: q.get("area") || "", verified: q.get("verified") === "1", week: q.get("week") === "1", max: q.get("max") || "" };
}
function queryFromFilters(f) {
  const q = new URLSearchParams();
  if (f.trade) q.set("trade", f.trade);
  if (f.area) q.set("area", f.area);
  if (f.verified) q.set("verified", "1");
  if (f.week) q.set("week", "1");
  if (f.max) q.set("max", f.max);
  const text = q.toString();
  return text ? `#/?${text}` : "#/";
}
async function search(f) {
  const trade = S.cat.trades.find((t) => t.name === f.trade);
  const area = S.cat.areas.find((a) => a.name === f.area);
  const data = await must("search", { trade_id: trade?.id, area_id: area?.id, verified_only: f.verified, available_week: f.week, max_price: f.max ? Number(f.max) : null });
  return { cards: data.results, trade, area };
}

function resultRow(c) {
  return html`<li>
    <div>
      <h2><a href="#/fundi/${c.id}">${c.name}</a></h2>
      <p class="result-meta">${c.trade} · ${count(c.years, "year")} · ${c.areas.join(", ")}</p>
    </div>
    <div class="result-rating">
      ${c.rating === null ? html`<span class="muted">No reviews yet</span>` : html`<span class="big num">${c.rating.toFixed(1)}</span> ${count(c.review_count, "confirmed review")}`}<br>
      <span class="muted num">${count(c.completed_jobs, "job")} confirmed</span>
    </div>
    <div class="result-price num"><strong>${kes(c.price_min)} to ${c.price_max.toLocaleString("en-US")}</strong><br>
      <span class="${c.available_this_week ? "" : "muted"}">${c.available_this_week ? "Free this week" : "Away this week"}</span>
    </div>
    <ul class="facts" aria-label="Verification and ranking">
      ${c.badges.length ? c.badges.map((b) => html`<li class="have">${b.label}</li>`) : html`<li class="muted">No verification badges yet</li>`}
      ${c.boosted ? html`<li class="tag">New: shown in a reserved result slot</li>` : c.is_new ? html`<li class="muted">New: fewer than ${S.cat.limits.new_fundi_jobs} confirmed jobs</li>` : ""}
    </ul>
  </li>`;
}

function resultsBlock(f, found) {
  const { cards, trade, area } = found;
  const what = trade ? trade.plural.toLowerCase() : "fundis";
  const where = area ? ` in ${area.name}` : "";
  const label = cards.length === 0 ? "No fundis match these filters" : `${count(cards.length, what.replace(/s$/, ""), what)}${where}`;
  return html`<p class="result-count" role="status" id="result-count">${label}</p>
    ${cards.length === 0
      ? html`<p>Try a different area, untick the verified or availability box, or <a href="#/">clear all filters</a>.</p>`
      : html`<ol class="results" aria-label="Fundis, best match first">${cards.map(resultRow)}</ol>
        <details class="rule-top">
          <summary>How the order is decided</summary>
          <p class="note">This is the real ranking from <code>ranking.py</code>. Each score blends review quality (an average pulled toward the platform mean, with newer reviews counting more), confirmed job count, reply speed, punctuality and verification. Every fourth slot is held for newcomers with a verified phone, rotating daily, instead of giving them a score bonus. Open a profile to see a score worked out.</p>
        </details>`}`;
}

async function viewFind(route) {
  const f = filtersFromQuery(route.q);
  S.filters = f;
  const found = await search(f);
  const opt = (list, chosen, key = "name") => list.map((x) => html`<option value="${x[key]}"${x[key] === chosen ? " selected" : ""}>${x[key]}</option>`);
  return {
    title: "Find a fundi",
    html: html`<div class="cols">
      <div>
        <h1>Find a fundi whose record you can check</h1>
        <p class="lede">Every review on this site comes from a job that was confirmed with a one-time code. Search the invented fundis below, then book one.</p>
        <form class="filters" id="filters" role="search" aria-label="Find a fundi">
          <div class="field"><label for="f-trade">Trade</label>
            <select id="f-trade" name="trade"><option value="">Any trade</option>${opt(S.cat.trades, f.trade)}</select></div>
          <div class="field"><label for="f-area">Area</label>
            <select id="f-area" name="area"><option value="">Any area</option>${opt(S.cat.areas, f.area)}</select></div>
          <div class="field"><label for="f-max">Most you can pay (KES)</label>
            <input id="f-max" name="max" type="number" min="0" step="100" inputmode="numeric" value="${f.max}" placeholder="No limit"></div>
          <div class="checks">
            <label class="check"><input type="checkbox" name="verified" value="1"${f.verified ? " checked" : ""}> Verified only (2 or more badges)</label>
            <label class="check"><input type="checkbox" name="week" value="1"${f.week ? " checked" : ""}> Free this week</label>
          </div>
          <div class="go"><button class="btn" type="submit">Show fundis</button></div>
        </form>
        <div id="results">${resultsBlock(f, found)}</div>
      </div>
      ${tryThis()}
    </div>`,
    after() {
      const form = $("#filters");
      const read = () => {
        const d = new FormData(form);
        return { trade: d.get("trade") || "", area: d.get("area") || "", verified: d.get("verified") === "1", week: d.get("week") === "1", max: d.get("max") || "" };
      };
      const run = async () => {
        const next = read();
        history.replaceState(null, "", queryFromFilters(next));
        S.filters = next;
        const found = await search(next);
        $("#results").innerHTML = resultsBlock(next, found).text;
        announce($("#result-count").textContent);
      };
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        run();
      });
      form.addEventListener("change", run);
    },
  };
}

// ---------- profile ----------

function figure(label, value) {
  return html`<div><dt>${label}</dt><dd class="num">${value}</dd></div>`;
}

async function viewProfile(id) {
  let p;
  try {
    p = await must("profile", { id });
  } catch (error) {
    return { title: "Fundi not found", html: html`<h1>Fundi not found</h1><p>${error.message} <a href="#/">Back to the search</a>.</p>` };
  }
  const r = p.record;
  const ratio = (pair) => (pair[1] ? `${pair[0]} of ${pair[1]}` : "no record yet");
  const maxHist = Math.max(1, ...p.histogram.map((h) => h.count));
  const first = p.first_name;
  const twoDays = (S.now ? new Date(S.now).getTime() : Date.now()) + 2 * 86400000;
  const defaultTime = `${eatInput(twoDays).slice(0, 10)}T09:00`;
  return {
    title: p.name,
    html: html`<p class="crumbs"><a href="#/">Find a fundi</a> / ${p.name}</p>
    <div class="cols">
      <div>
        <h1>${p.name}</h1>
        <p class="lede">${p.trade} · ${count(p.years, "year")} of experience · works in ${p.areas.join(", ")}</p>
        <div class="rating-line">
          ${p.rating === null ? html`<span class="muted">No reviews yet</span>` : html`<span class="big num">${p.rating.toFixed(1)}</span><span>from ${count(p.review_count, "review")}, each tied to a job confirmed with a one-time code</span>`}
        </div>
        <p><strong>${p.headline}</strong></p>
        <p class="bio">${p.bio} <span class="muted">(Invented profile.)</span></p>
        ${badgeList(p)}

        <h2 class="rule-top">The record</h2>
        <dl class="figures">
          ${figure("jobs confirmed with a code", r.confirmed)}
          ${figure("confirmed in the last 90 days", r.completed_90d)}
          ${figure("quote requests answered within a day", ratio(r.replied_in_time))}
          ${figure("jobs started on time", ratio(r.on_time))}
        </dl>

        <h2 class="rule-top">What counts toward this rating</h2>
        <p class="note">A review can only be written for a job whose code was used. The other bookings below exist, but they add no review and no average.</p>
        <div class="table-wrap"><table>
          <thead><tr><th scope="col">Bookings of ${first}</th><th scope="col" class="r">Jobs</th><th scope="col">Counts toward the rating?</th></tr></thead>
          <tbody>
            <tr><th scope="row">Confirmed with a one-time code</th><td class="r">${r.confirmed}</td><td>Yes. ${count(r.reviews, "review")} written (${count(r.confirmed_without_review, "confirmed job")} without one).</td></tr>
            <tr><th scope="row">Marked done, code not entered</th><td class="r">${r.awaiting_code}</td><td>No. A review is refused until the code is used.</td></tr>
            <tr><th scope="row">Booked or in progress</th><td class="r">${r.in_flight}</td><td>No.</td></tr>
            <tr><th scope="row">Cancelled</th><td class="r">${r.cancelled}</td><td>No. A cancellation by the fundi counts against punctuality.</td></tr>
            <tr><th scope="row">No-show reported</th><td class="r">${r.no_show}</td><td>No. It counts against punctuality.</td></tr>
            <tr><th scope="row">Reviews on a job without a used code</th><td class="r">${r.reviews_without_confirmed_job}</td><td>None can exist: <code>reviews.create_review</code> refuses them.</td></tr>
          </tbody></table></div>

        <h2 class="rule-top">Ratings</h2>
        <div class="table-wrap"><table class="hist">
          <thead><tr><th scope="col">Rating</th><th scope="col" class="r">Reviews</th><th scope="col"><span class="sr-only">Share</span></th></tr></thead>
          <tbody>${p.histogram.map((h) => html`<tr><th scope="row">${h.stars} out of 5</th><td class="r">${h.count}</td><td><span class="bar" aria-hidden="true" style="width:${Math.round((h.count / maxHist) * 100)}%"></span></td></tr>`)}</tbody>
        </table></div>
        <details class="mt">
          <summary>How this fundi's ranking score of ${p.score} was worked out</summary>
          <p class="note">Points out of 100, from <code>ranking.score_fundi</code>. Review quality uses a smoothed average that starts from the platform average of about ${p.platform_mean}, so a handful of reviews moves it only a little.</p>
          <div class="table-wrap"><table><tbody>${p.components.map((c) => html`<tr><th scope="row">${c.label}</th><td class="r">${c.points}</td></tr>`)}</tbody></table></div>
        </details>

        <h2 class="rule-top">Reviews</h2>
        ${p.reviews.length === 0 ? html`<p>No reviews yet. The first one can only come after a job is confirmed with its code.</p>` : html`<ul class="reviews">${p.reviews.map((rv) => html`<li>
          <div class="top"><span class="score num">${rv.rating}<span class="sr-only"> out of 5</span></span><strong>${rv.who}</strong><time datetime="${rv.date}" class="muted">${fmtDate(rv.date)}</time></div>
          <p class="proof">Confirmed job: ${rv.job}, code used ${fmtDate(rv.confirmed_at)}</p>
          ${rv.body ? html`<blockquote>${rv.body}</blockquote>` : ""}
          ${rv.reply ? html`<p class="reply"><strong>${first} replied:</strong> ${rv.reply}</p>` : ""}
        </li>`)}</ul>${p.review_count > p.reviews.length ? html`<p class="note">Showing the latest ${p.reviews.length} of ${p.review_count}.</p>` : ""}`}

        <h2 class="rule-top">Services and prices</h2>
        <div class="table-wrap"><table>
          <thead><tr><th scope="col">Job</th><th scope="col">${first}'s range</th><th scope="col">Paid on the platform</th></tr></thead>
          <tbody>${p.services.map((s) => html`<tr><th scope="row">${s.name}</th><td class="num">${kes(s.min)} to ${s.max.toLocaleString("en-US")}</td><td class="num">${s.bench ? html`Median ${kes(s.bench.median)}<br><span class="muted">typical ${kes(s.bench.low)} to ${s.bench.high.toLocaleString("en-US")}, ${count(s.bench.n, "job")}</span>` : html`<span class="muted">Shown after 5 confirmed jobs</span>`}</td></tr>`)}</tbody>
        </table></div>
      </div>

      <div>
        <section class="side-block" aria-labelledby="rq-h">
          <h2 id="rq-h">Request a quote</h2>
          <p class="note">${first} replies with a price. Nothing is charged: as in the real app, payment is between you and the fundi.</p>
          <form id="quote-form" data-action="request_quote" data-fundi="${p.id}" novalidate>
            <div class="field"><label for="q-job">Job</label><select id="q-job" name="job_type_id">${p.services.map((s) => html`<option value="${s.job_type_id}">${s.name}</option>`)}</select></div>
            <div class="field"><label for="q-area">Area</label><select id="q-area" name="area_id">${p.area_options.map((a) => html`<option value="${a.id}">${a.name}</option>`)}</select></div>
            <div class="field"><label for="q-desc">Describe the job</label>
              <textarea id="q-desc" name="description" aria-describedby="q-desc-hint" required></textarea>
              <p class="hint" id="q-desc-hint">At least 15 characters. What is wrong, and where.</p>
              <p class="field-error" id="q-desc-err" hidden></p></div>
            <div class="field"><label for="q-time">Preferred time (East Africa Time)</label><input id="q-time" name="preferred_time" type="datetime-local" value="${defaultTime}" required></div>
            <button class="btn" type="submit">Request a quote</button>
            <div id="quote-msg"></div>
          </form>
        </section>
        <section class="side-block" aria-labelledby="ver-h">
          <h2 id="ver-h">Verification</h2>
          <ul class="badge-list">${p.pillars.map((b) => {
            const why = p.badges.find((x) => x.label === b.label);
            return html`<li>${b.have ? html`<strong>${b.label}</strong><br><span class="muted">${why ? why.why : ""}</span>` : html`<span class="no">${b.label}: not earned</span>`}</li>`;
          })}</ul>
          <p class="note">Badges are computed from recorded facts. Nobody can add one by hand.</p>
        </section>
        <section class="side-block" aria-labelledby="av-h">
          <h2 id="av-h">Availability</h2>
          <p>${p.hours}</p>
          <p class="${p.available_this_week ? "" : "muted"}">${p.available_this_week ? "Free this week." : "Away this week."}</p>
        </section>
      </div>
    </div>`,
  };
}

// ---------- bookings ----------

const STAGES = ["Requested", "Quoted", "Booked", "Fundi on site", "Work done", "Confirmed with code", "Reviewed"];

function progressOf(it) {
  const j = it.job;
  if (!j) return it.request_status === "open" ? 1 : 2;
  if (j.status === "booked") return 3;
  if (j.status === "in_progress") return 4;
  if (j.status === "awaiting_confirmation") return 5;
  if (j.status === "completed") return j.review ? 7 : 6;
  return null;
}

const stageList = (it) => {
  const done = progressOf(it);
  if (done === null) return "";
  return html`<ol class="stages" aria-label="Progress">${STAGES.map((label, i) => html`<li class="${i < done ? "done" : i === done ? "now" : ""}"${i === done ? ' aria-current="step"' : ""}>${label}<span class="sr-only">${i < done ? " (done)" : i === done ? " (next)" : ""}</span></li>`)}</ol>`;
};

function slotMsg(slot) {
  const f = S.flash[slot];
  return f ? msg(f.kind, f.text, f.why) : "";
}

const submit = (label, cls = "btn", sr = "") => html`<button class="${cls}" type="submit">${label}${sr ? html`<span class="sr-only"> ${sr}</span>` : ""}</button>`;

function codeTicket(it) {
  const j = it.job;
  const first = it.fundi.first_name;
  if (j.code_invalidated) {
    return html`<p class="msg bad" role="status"><strong>This code is disabled.</strong><span class="why">It was disabled after ${S.cat.limits.wrong_before_disabled} wrong tries in total. An admin has to issue a new one.</span></p>
      <form data-action="reissue" data-job="${j.id}" data-req="${it.request_id}">${submit("Demo admin: issue a new code", "btn ghost", `for ${it.fundi.name}'s job`)}</form>
      <p class="hint">In the real app an admin does this from the locked-codes queue, and it goes in the audit log.</p>`;
  }
  return html`<div class="code-ticket">
    <span class="label">Your completion code</span>
    <span class="digits num" aria-label="Completion code ${j.code.split("").join(" ")}">${spaced(j.code)}</span>
    <p>Keep it private until the work is finished and you are happy. Type it below, or read it to ${first} to type on their phone.</p>
  </div>`;
}

function reviewForm(it, label, idSuffix) {
  const j = it.job;
  const id = `rv-${it.request_id}-${idSuffix}`;
  return html`<form class="inline-form wide" data-action="review" data-job="${j.id}" data-req="${it.request_id}" novalidate>
    <fieldset><legend>Rating</legend>
      <div class="rating-pick">${[5, 4, 3, 2, 1].map((n) => html`<label><input type="radio" name="rating" value="${n}"> ${n}<span class="sr-only"> out of 5</span></label>`)}</div></fieldset>
    <div class="field"><label for="${id}">What was it like? (optional)</label><textarea id="${id}" name="body" rows="3"></textarea></div>
    <div>${submit(label, "btn", `for ${it.fundi.name}`)}</div>
  </form>`;
}

function codeForm(it, actor, labelText) {
  const j = it.job;
  const id = `code-${actor[0]}-${it.request_id}`;
  const left = j.attempts_left;
  return html`<form class="inline-form" data-action="confirm" data-job="${j.id}" data-req="${it.request_id}" data-actor="${actor}" novalidate>
    <label for="${id}">${labelText}</label>
    <input id="${id}" name="code" type="text" inputmode="numeric" autocomplete="off" maxlength="7" aria-describedby="${id}-hint">
    <p class="hint" id="${id}-hint">Six digits.${j.total_failures ? ` Wrong tries so far: ${j.total_failures} of ${S.cat.limits.wrong_before_disabled}. ${left} left before an hour's lock.` : ` ${S.cat.limits.wrong_before_lock} wrong tries lock entry for an hour.`}</p>
    <div>${submit("Confirm completion", "btn", `for ${it.fundi.name}'s job`)}</div>
  </form>`;
}

function lockNote(j) {
  return j.code_locked_until ? html`<p class="msg bad" role="status"><strong>Entry is locked until ${fmtTime(j.code_locked_until)}.</strong><span class="why">Even the right code is refused while locked. Skip the demo clock ahead an hour to lift it.</span></p>` : "";
}

function customerSide(it) {
  const j = it.job;
  const q = it.quote;
  const first = it.fundi.first_name;
  const slot = `c-${it.request_id}`;
  let body;
  if (!j) {
    if (it.request_status === "open") {
      body = html`<p>Waiting for ${first} to send a price.</p>
        <form data-action="cancel_request" data-req="${it.request_id}">${submit("Cancel request", "btn ghost", `to ${it.fundi.name}`)}</form>`;
    } else if (it.request_status === "quoted" && q && q.status === "sent") {
      body = html`<p>${first} quoted <strong class="num">${kes(q.amount)}</strong>, starting ${fmtTime(q.start)}.</p>${q.message ? html`<p class="muted">"${q.message}"</p>` : ""}
        <form data-action="accept_quote" data-quote="${q.id}" data-req="${it.request_id}">${submit("Accept and book", "btn", `quote from ${it.fundi.name}`)}</form>`;
    } else {
      body = html`<p class="status bad">${it.request_status === "declined" ? `${first} declined this request.` : "This request was cancelled."}</p>`;
    }
  } else if (j.status === "booked" || j.status === "in_progress") {
    body = html`<p>${j.status === "booked" ? `Booked for ${fmtTime(j.scheduled_for)}, ${kes(j.price)}.` : `${first} is on site.`}</p>${codeTicket(it)}
      ${j.customer_events.includes("cancel") ? html`<form data-action="job_event" data-job="${j.id}" data-req="${it.request_id}" data-actor="customer" data-event="cancel">${submit("Cancel booking", "btn ghost", `with ${it.fundi.name}`)}</form>` : ""}`;
  } else if (j.status === "awaiting_confirmation") {
    body = html`<p>${first} says the work is done. If you are happy, confirm with your code. That unlocks your review.</p>${codeTicket(it)}${lockNote(j)}
      ${j.code_invalidated ? "" : codeForm(it, "customer", "Completion code")}
      <details class="probe"><summary>Try to review before confirming</summary>
        <p class="note">The real page hides this form until the job is confirmed. Here it is shown so you can send a review to the same function and read its refusal.</p>
        ${reviewForm(it, "Publish review", "early")}</details>`;
  } else if (j.status === "completed") {
    const by = j.confirmed_by === "fundi" ? `${first} typed the code` : "You entered the code";
    body = html`<p>Confirmed on ${fmtDate(j.completed_at)}. ${by}, so it is spent.</p>
      ${j.review ? html`<p><strong>Your review:</strong> <span class="num">${j.review.rating} out of 5</span>${j.review.body ? html`. "${j.review.body}"` : ""}</p>`
        : j.can_review ? html`<h4 class="sub-h">Review ${first}</h4><p class="note">Your review is tied to this confirmed job.</p>${reviewForm(it, "Publish review", "open")}`
        : html`<p class="msg bad" role="status"><strong>${j.review_block}</strong></p>`}
      <details class="probe"><summary>Try to break the rules</summary>
        <p class="note">The real pages never offer these. Each goes to the same function the app uses and shows its answer.</p>
        <form class="inline-form" data-action="probe_reuse" data-job="${j.id}" data-req="${it.request_id}" novalidate>
          <label for="reuse-${it.request_id}">Enter the code again</label>
          <input id="reuse-${it.request_id}" name="code" type="text" inputmode="numeric" autocomplete="off" maxlength="7" aria-describedby="reuse-${it.request_id}-hint">
          <p class="hint" id="reuse-${it.request_id}-hint">Any six digits. The single-use check runs before the digits are compared. This calls <code>completion.check_code</code> directly, because the app's job page never reaches it for a finished job.</p>
          <div>${submit("Enter code", "btn ghost", `again for ${it.fundi.name}'s job`)}</div></form>
        ${j.review ? html`<p class="note mt">Post a second review for the same job:</p>${reviewForm(it, "Post another review", "again")}` : ""}
      </details>`;
  } else {
    body = html`<p class="status bad">${j.status === "cancelled" ? "Booking cancelled." : "Marked as a no-show."}</p>`;
  }
  return html`<section aria-labelledby="cs-${it.request_id}">
    <span class="who">Customer's side</span><h3 id="cs-${it.request_id}">Amina Wairimu</h3>
    <div id="m-${slot}">${slotMsg(slot)}</div>${body}
  </section>`;
}

function fundiSide(it) {
  const j = it.job;
  const q = it.quote;
  const first = it.fundi.first_name;
  const slot = `f-${it.request_id}`;
  let body;
  if (!j) {
    if (it.request_status === "open") {
      const id = `qf-${it.request_id}`;
      body = html`<p>${S.cat.customer.name.split(" ")[0]} asked for a price. Send one, or decline.</p>
        <form class="inline-form" data-action="send_quote" data-req="${it.request_id}" novalidate>
          <div class="field"><label for="${id}-amt">Price (KES)</label><input id="${id}-amt" name="amount" type="text" inputmode="numeric" autocomplete="off" aria-describedby="${id}-amt-hint"><p class="hint" id="${id}-amt-hint">Between KES 100 and KES 5,000,000.</p><p class="field-error" id="${id}-amt-err" hidden></p></div>
          <div class="field"><label for="${id}-start">Start time (East Africa Time)</label><input id="${id}-start" name="start" type="datetime-local" value="${eatInput(new Date(it.preferred_time).getTime())}"></div>
          <div class="field"><label for="${id}-msg">Note to the customer (optional)</label><input id="${id}-msg" name="message" type="text" maxlength="200"></div>
          <div class="actions">${submit("Send quote", "btn", `as ${it.fundi.name}`)}</div>
        </form>
        <form class="mt" data-action="decline_request" data-req="${it.request_id}">${submit("Decline request", "btn ghost", `as ${it.fundi.name}`)}</form>`;
    } else if (it.request_status === "quoted" && q) {
      body = html`<p>Quote sent: <strong class="num">${kes(q.amount)}</strong>, starting ${fmtTime(q.start)}. Waiting for the customer to accept.</p>`;
    } else {
      body = html`<p class="status bad">${it.request_status === "declined" ? "You declined this request." : "The customer cancelled this request."}</p>`;
    }
  } else if (j.status === "booked") {
    body = html`<p>Booked for ${fmtTime(j.scheduled_for)}, ${kes(j.price)}. You can see the job but not the completion code.</p>
      <div class="actions">
        <form data-action="job_event" data-job="${j.id}" data-req="${it.request_id}" data-actor="fundi" data-event="start">${submit("Start job", "btn", `as ${it.fundi.name}`)}</form>
        <form data-action="job_event" data-job="${j.id}" data-req="${it.request_id}" data-actor="fundi" data-event="cancel">${submit("Cancel booking", "btn ghost", `as ${it.fundi.name}`)}</form>
      </div>`;
  } else if (j.status === "in_progress") {
    body = html`<p>On site since ${fmtTime(j.started_at)}.</p>
      <div class="actions">
        <form data-action="job_event" data-job="${j.id}" data-req="${it.request_id}" data-actor="fundi" data-event="mark_complete">${submit("Mark work done", "btn", `as ${it.fundi.name}`)}</form>
        <form data-action="job_event" data-job="${j.id}" data-req="${it.request_id}" data-actor="fundi" data-event="cancel">${submit("Cancel booking", "btn ghost", `as ${it.fundi.name}`)}</form>
      </div>
      <p class="note mt">Marking the work done does not complete the job. Only the code can.</p>`;
  } else if (j.status === "awaiting_confirmation") {
    body = html`<p>You marked the work done at ${fmtTime(j.marked_complete_at)}. The code is on the customer's side only; you cannot see it. If they read it out, type it here.</p>
      ${lockNote(j)}${j.code_invalidated ? html`<p class="note">The code is disabled. See the customer's side for the admin step.</p>` : codeForm(it, "fundi", `Code ${S.cat.customer.name.split(" ")[0]} read out`)}`;
  } else if (j.status === "completed") {
    body = html`<p>Job confirmed on ${fmtDate(j.completed_at)}. It now counts toward ${first}'s record${j.review ? ", with a review" : ""}.</p>`;
  } else {
    body = html`<p class="status bad">${j.status === "cancelled" ? "Booking cancelled." : "Marked as a no-show. It counts against punctuality."}</p>`;
  }
  return html`<section aria-labelledby="fs-${it.request_id}">
    <span class="who">Fundi's side</span><h3 id="fs-${it.request_id}">${it.fundi.name}</h3>
    <div id="m-${slot}">${slotMsg(slot)}</div>${body}
  </section>`;
}

function bookingCard(it) {
  const j = it.job;
  const when = j ? j.scheduled_for : it.preferred_time;
  const sub = [it.area, fmtTime(when), j ? kes(j.price) : it.quote ? kes(it.quote.amount) : ""].filter(Boolean).join(" · ");
  const statusText = j ? { booked: "Booked", in_progress: "In progress", awaiting_confirmation: "Waiting for the code", completed: "Completed", cancelled: "Cancelled", no_show: "No-show" }[j.status] : { open: "Waiting for a quote", quoted: "Quote received", declined: "Declined", cancelled: "Cancelled" }[it.request_status];
  return html`<article class="booking" data-req="${it.request_id}" aria-labelledby="bk-${it.request_id}">
    <header>
      <h2 id="bk-${it.request_id}" tabindex="-1">${it.job_type} with <a href="#/fundi/${it.fundi.id}">${it.fundi.name}</a></h2>
      <p class="booking-sub"><span class="status">${statusText}.</span> ${sub}. ${esc(it.description)}</p>
    </header>
    ${stageList(it)}
    <div class="sides">${customerSide(it)}${fundiSide(it)}</div>
  </article>`;
}

async function viewBookings() {
  const data = await must("bookings");
  return {
    title: "My bookings",
    html: html`<div class="cols">
      <div>
        <h1>My bookings</h1>
        <p class="lede">You are Amina Wairimu, a customer. Each booking shows both sides of the job, so you can play the customer and the fundi in turn. There is no login in this demo.</p>
        <div class="clock" id="clock">
          <p id="clock-time" class="num"><strong>Demo time:</strong> ${fmtTime(data.now)} (East Africa Time)</p>
          <div class="actions">
            <button type="button" class="btn ghost" data-skip="1">Skip ahead 1 hour</button>
            <button type="button" class="btn ghost" data-skip="${24 * 31}">Skip ahead 31 days</button>
          </div>
        </div>
        <p class="note">The clock only moves when you skip it, and keeps running at normal speed otherwise. Skipping lifts a one-hour code lock, or closes the ${S.cat.limits.review_window_days}-day review window.</p>
        <div id="booking-list">${data.items.length ? data.items.map(bookingCard) : html`<p>No bookings yet. <a href="#/">Find a fundi</a> and request a quote.</p>`}</div>
      </div>
      ${tryThis()}
    </div>`,
  };
}

// ---------- how this demo works ----------

async function viewHow() {
  if (!S.manifest) {
    try {
      S.manifest = await (await fetch("python/manifest.json")).json();
    } catch {
      S.manifest = { files: [], commit: "unknown" };
    }
  }
  const m = S.manifest;
  const info = S.info || {};
  const lines = m.files.reduce((n, f) => n + f.lines, 0);
  return {
    title: "How this demo works",
    html: html`<div class="prose">
      <h1>How this demo works</h1>
      <p class="lede">This page runs Fundi Finder's real Python code inside your browser. It is the project's own marketplace logic on an in-memory database, with the web server taken away.</p>

      <h2>Approach</h2>
      <p>The real code, not a rewrite. The Flask web app and its SQLite file cannot run on GitHub Pages, which only serves files. So the page loads a self-hosted copy of Pyodide (${info.python ? `Python ${info.python}` : "CPython"} compiled to WebAssembly) and SQLAlchemy ${info.sqlalchemy || ""}, then imports ${count(m.files.length, "module")} from <code>src/fundifinder</code> (${count(lines, "line")}). They run against an in-memory SQLite database that lives in this tab.</p>

      <div class="split two">
        <div>
          <h2>Real</h2>
          <ul>
            <li><code>models</code>: the SQLAlchemy tables.</li>
            <li><code>queries</code> and <code>ranking</code>: search, filters, scores, the reserved newcomer slots and every rating shown.</li>
            <li><code>marketplace</code>: requests, quotes, booking, and the completion-code check.</li>
            <li><code>lifecycle</code>: the job state machine and who may do what.</li>
            <li><code>completion</code>: the six-digit code derived with HMAC-SHA256, constant-time comparison, a one-hour lock after ${S.cat.limits.wrong_before_lock} wrong tries, disabled after ${S.cat.limits.wrong_before_disabled}.</li>
            <li><code>reviews</code>: the review gate, one review per job, the ${S.cat.limits.review_window_days}-day window.</li>
            <li><code>badges</code>, <code>availability</code>, <code>pricing</code>.</li>
          </ul>
        </div>
        <div>
          <h2>Simulated or invented</h2>
          <ul>
            <li>All people, jobs, prices and reviews: ${S.cat.platform.fundis} fundis, ${S.cat.platform.confirmed_jobs} confirmed jobs and ${S.cat.platform.reviews} reviews, generated with a fixed random seed.</li>
            <li>The web layer: Flask routes, templates, sessions, CSRF, rate limits and login are not here. This page and a short Python wrapper (<code>demo/engine/demo_engine.py</code>) stand in for them.</li>
            <li>You play both the customer and the fundi. The app enforces who may do what; here the wrapper picks the actor for each button.</li>
            <li>The demo clock, so you can wait out a lock without waiting.</li>
            <li>The code secret is in your browser. In the app it stays on the server, so this shows how the code works, not how it is kept secret.</li>
            <li>Wording for each code result is copied from the web layer.</li>
          </ul>
        </div>
      </div>

      <h2>About "expired"</h2>
      <p>In the real app a completion code has no time limit. It stops working in four ways: it is used once, it is locked for an hour after ${S.cat.limits.wrong_before_lock} wrong tries, it is disabled after ${S.cat.limits.wrong_before_disabled} in total until an admin issues a new one, or the job is cancelled. Separately, the right to review closes ${S.cat.limits.review_window_days} days after the job is confirmed. The demo clock lets you reach each of these.</p>

      <h2>Not shown</h2>
      <p>Photo uploads, ID document review, SMS phone verification, reports and appeals, the abuse queue, no-show reports, editing and replying to reviews, and the fundi's own pages. They are in the repository and its tests.</p>

      <h2>Your data</h2>
      <p>Nothing leaves your browser. The page makes no request to any other site. ${store.works ? "Your changes are saved in this browser's local storage so a reload keeps your place." : "This browser blocked local storage, so your changes last only until you close the tab."} <button type="button" class="linkish" data-reset>Reset demo data</button> restores the starting data. After the first load the demo works offline.</p>

      <h2>Run the real thing</h2>
      <p>Needs <a href="https://docs.astral.sh/uv/">uv</a> and Python 3.12. No Docker and no outside services.</p>
<pre>git clone https://github.com/Victor-Wambua/fundi-finder.git
cd fundi-finder
uv sync
uv run flask --app fundifinder seed
uv run flask --app fundifinder run --port 5001</pre>
      <p>Then open <code>http://127.0.0.1:5001</code> and sign in with <code>customer@fundifinder.test</code> and the password <code>demo-password-123</code> (all synthetic).</p>

      <h2>What was loaded</h2>
      <p class="note">Runtime: Pyodide ${info.pyodide || ""}, Python ${info.python || ""}, SQLAlchemy ${info.sqlalchemy || ""}. Source commit <code>${(m.commit || "unknown").slice(0, 7)}</code>. The repository's tests also run these same modules under SQLAlchemy 2.1.</p>
      <div class="table-wrap"><table>
        <thead><tr><th scope="col">Module</th><th scope="col" class="r">Lines</th><th scope="col">SHA-256 (first 12)</th></tr></thead>
        <tbody>${m.files.map((f) => html`<tr><th scope="row"><code>${f.path}</code></th><td class="r">${f.lines}</td><td><code>${f.sha256.slice(0, 12)}</code></td></tr>`)}</tbody>
      </table></div>
      <p class="note">Runtime files and licences: <a href="vendor/NOTICE.md">vendor/NOTICE.md</a>.</p>
    </div>`,
  };
}

// ---------- actions ----------

function field(form, name) {
  return form.elements[name];
}

function setError(input, errorEl, text) {
  if (text) {
    errorEl.textContent = text;
    errorEl.hidden = false;
    input.setAttribute("aria-invalid", "true");
    input.setAttribute("aria-describedby", `${errorEl.id} ${input.getAttribute("aria-describedby") || ""}`.trim());
  } else {
    errorEl.hidden = true;
    input.removeAttribute("aria-invalid");
    input.setAttribute("aria-describedby", (input.getAttribute("aria-describedby") || "").replace(errorEl.id, "").trim());
  }
}

const WHY = {
  review: "reviews.create_review checks that the job is completed and its code was used, before it writes anything.",
  job_event: "lifecycle.transition only allows the moves in its table, and only for the people named there.",
  confirm: "marketplace.confirm_completion runs completion.check_code, which counts wrong tries.",
  probe_reuse: "completion.check_code looks at whether the code was already used before it compares any digits.",
};

async function refreshCard(reqId) {
  const data = await must("bookings");
  const it = data.items.find((x) => String(x.request_id) === String(reqId));
  const old = $(`article[data-req="${reqId}"]`);
  if (it && old) old.outerHTML = bookingCard(it).text;
  const clockTime = $("#clock-time");
  if (clockTime) clockTime.innerHTML = `<strong>Demo time:</strong> ${esc(fmtTime(data.now))} (East Africa Time)`;
}

const ACTIONS = {
  async request_quote(form) {
    const desc = field(form, "description");
    const err = $("#q-desc-err");
    const text = desc.value.trim();
    if (text.length < 15) {
      setError(desc, err, `Describe the job in at least 15 characters. You have ${text.length}.`);
      desc.focus();
      return;
    }
    setError(desc, err, "");
    const out = await engine.call("request_quote", {
      fundi_id: Number(form.dataset.fundi),
      job_type_id: Number(field(form, "job_type_id").value),
      area_id: Number(field(form, "area_id").value),
      description: text,
      preferred_time: field(form, "preferred_time").value ? eatToIso(field(form, "preferred_time").value) : "",
    });
    const slot = $("#quote-msg");
    if (out.ok) {
      desc.value = "";
      slot.innerHTML = html`<p class="msg ok" role="status" tabindex="-1"><strong>Request sent to ${out.data.fundi_first_name}.</strong><span class="why"><a href="#/bookings">Go to My bookings</a> to send the quote as the fundi, then accept it as the customer.</span></p>`.text;
      announce(`Request sent to ${out.data.fundi_first_name}.`);
      persist();
    } else {
      slot.innerHTML = msg("bad", out.error, "").text;
    }
    $("p", slot)?.focus();
  },

  async send_quote(form) {
    const id = form.dataset.req;
    const amt = field(form, "amount");
    const err = $(`#qf-${id}-amt-err`);
    const raw = amt.value.replace(/,/g, "").trim();
    if (!/^\d+$/.test(raw)) {
      setError(amt, err, "Enter the price as a whole number of shillings, for example 2400.");
      amt.focus();
      return;
    }
    setError(amt, err, "");
    const start = field(form, "start").value;
    const out = await engine.call("send_quote", { request_id: Number(id), amount: raw, start: start ? eatToIso(start) : "", message: field(form, "message").value });
    await finishBooking(id, "f", out, { ok: "Quote sent. Now accept it on the customer's side." });
  },

  decline_request: (form) => simple(form, "decline_request", { request_id: Number(form.dataset.req) }, "f", "Request declined."),
  cancel_request: (form) => simple(form, "cancel_request", { request_id: Number(form.dataset.req) }, "c", "Request cancelled."),
  accept_quote: (form) => simple(form, "accept_quote", { quote_id: Number(form.dataset.quote) }, "c", "Booked. Your completion code is on this side. Keep it private until the work is done."),

  async job_event(form) {
    const d = form.dataset;
    const done = { start: "Job started.", mark_complete: "Marked done. The job is not complete yet: only the customer's code can finish it.", cancel: "Booking cancelled." }[d.event];
    const out = await engine.call("job_event", { job_id: Number(d.job), actor: d.actor, event: d.event });
    await finishBooking(d.req, d.actor === "customer" ? "c" : "f", out, { ok: done, why: WHY.job_event });
  },

  async confirm(form) {
    const d = form.dataset;
    const input = field(form, "code");
    const out = await engine.call("confirm", { job_id: Number(d.job), actor: d.actor, code: input.value });
    const slot = d.actor === "customer" ? "c" : "f";
    if (out.ok) {
      const good = out.data.result === "ok";
      let text = out.data.message;
      if (out.data.result === "locked" && out.data.locked_until) text += ` Locked until ${fmtTime(out.data.locked_until)}.`;
      S.flash[`${slot}-${d.req}`] = { kind: good ? "ok" : "bad", text, why: good ? "The code is now spent, so it cannot confirm anything again." : WHY.confirm };
      persist();
      await refreshCard(d.req);
      announce(text);
      if (good) $(`#m-${slot}-${d.req} p`)?.focus();
      else ($(`#code-${slot}-${d.req}`) || $(`#m-${slot}-${d.req} p`))?.focus();
    } else {
      await finishBooking(d.req, slot, out, { why: WHY.confirm });
    }
  },

  async probe_reuse(form) {
    const d = form.dataset;
    const out = await engine.call("probe_reuse", { job_id: Number(d.job), code: field(form, "code").value });
    S.flash[`c-${d.req}`] = out.ok ? { kind: "bad", text: out.data.message, why: WHY.probe_reuse } : { kind: "bad", text: out.error };
    await refreshCard(d.req);
    announce(out.ok ? out.data.message : out.error);
    $(`#m-c-${d.req} p`)?.focus();
  },

  reissue: (form) => simple(form, "reissue", { job_id: Number(form.dataset.job) }, "c", "New code issued. The old one no longer works."),

  async review(form) {
    const d = form.dataset;
    const picked = form.querySelector('input[name="rating"]:checked');
    const out = await engine.call("review", { job_id: Number(d.job), rating: picked ? Number(picked.value) : 0, body: field(form, "body").value });
    if (out.ok) {
      const { before, after, fundi } = out.data;
      const avg = (x) => (x.rating === null ? "none" : x.rating.toFixed(2));
      const text = `Review published. ${fundi}'s confirmed reviews: ${before.review_count} to ${after.review_count}. Average ${avg(before)} to ${avg(after)}.`;
      S.flash[`c-${d.req}`] = { kind: "ok", text, why: "The count and average come from queries.load_cards, which only sees reviews that the gate let through." };
      persist();
      await refreshCard(d.req);
      announce(text);
      $(`#m-c-${d.req} p`)?.focus();
    } else {
      await finishBooking(d.req, "c", out, { why: WHY.review, refusal: "Refused" });
    }
  },
};

async function simple(form, name, args, slot, okText) {
  const out = await engine.call(name, args);
  await finishBooking(form.dataset.req, slot, out, { ok: okText });
}

async function finishBooking(reqId, slot, out, opts) {
  const key = `${slot}-${reqId}`;
  if (out.ok) {
    S.flash[key] = { kind: "ok", text: opts.ok || "Done.", why: "" };
    persist();
  } else {
    S.flash[key] = { kind: "bad", text: `${opts.refusal ? opts.refusal + ": " : ""}${out.error}`, why: out.kind === "rule" ? opts.why || "" : "This is a bug in the demo, not a rule." };
  }
  await refreshCard(reqId);
  announce(S.flash[key].text);
  $(`#m-${key} p`)?.focus();
}

async function skipClock(hours) {
  const out = await engine.call("advance_clock", { hours });
  if (!out.ok) return;
  S.now = out.data.now;
  S.flash = {};
  persist();
  await render(false);
  announce(`Demo time is now ${fmtTime(S.now)}.`);
  $("#clock-time")?.setAttribute("tabindex", "-1");
  $("#clock-time")?.focus();
}

async function resetData() {
  await engine.reset();
  store.clear();
  S.flash = {};
  S.cat = await must("catalog");
  persist();
  await render(false);
  announce("Demo data reset to the starting data.");
}

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-action]");
  if (!form) return;
  event.preventDefault();
  const buttons = form.querySelectorAll("button");
  buttons.forEach((b) => (b.disabled = true));
  try {
    await ACTIONS[form.dataset.action](form);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
});

document.addEventListener("click", (event) => {
  const skip = event.target.closest("[data-skip]");
  if (skip) skipClock(Number(skip.dataset.skip));
  if (event.target.closest("[data-reset]")) resetData();
  if (event.target.closest("#foot-reset")) resetData();
});

// ---------- routing ----------

const ROUTES = [
  { match: /^\/$/, nav: "find", run: (route) => viewFind(route) },
  { match: /^\/fundi\/(\d+)$/, nav: "find", run: (route, m) => viewProfile(Number(m[1])) },
  { match: /^\/bookings$/, nav: "bookings", run: () => viewBookings() },
  { match: /^\/how$/, nav: "how", run: () => viewHow() },
];

let renderToken = 0;
async function render(moveFocus = true) {
  const token = ++renderToken;
  const [path, query = ""] = (location.hash.slice(1) || "/").split("?");
  const route = { path, q: new URLSearchParams(query) };
  let hit = null;
  let view;
  for (const r of ROUTES) {
    const m = path.match(r.match);
    if (m) {
      hit = r;
      view = await r.run(route, m);
      break;
    }
  }
  if (token !== renderToken) return;
  if (!hit) view = { title: "Page not found", html: html`<h1>Page not found</h1><p><a href="#/">Back to the search</a>.</p>` };
  main.innerHTML = view.html.text;
  main.removeAttribute("aria-busy");
  document.title = `${view.title} · Fundi Finder demo`;
  document.querySelectorAll("[data-nav]").forEach((a) => (hit && a.dataset.nav === hit.nav ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  view.after?.();
  if (moveFocus) {
    const h1 = $("h1", main);
    if (h1) {
      h1.setAttribute("tabindex", "-1");
      h1.focus({ preventScroll: false });
    }
    window.scrollTo(0, 0);
    announce(document.title);
  }
}

window.addEventListener("hashchange", () => render(true));

// ---------- start ----------

async function start() {
  const step = $("#boot-step");
  engine.onProgress = (text) => {
    step.textContent = text;
  };
  engine.start();
  try {
    S.info = await engine.boot(store.read());
    S.cat = await must("catalog");
  } catch (error) {
    main.removeAttribute("aria-busy");
    main.innerHTML = html`<h1>The demo could not start</h1>
      <p class="msg bad" role="alert"><strong>${error.message}</strong></p>
      <p>This page needs WebAssembly and Web Workers, and about 16 MB of files from this site. Try a current version of Chrome, Firefox, Safari or Edge, then reload. The <a href="https://github.com/Victor-Wambua/fundi-finder#readme">README</a> shows how to run the real app.</p>`.text;
    return;
  }
  persist();
  const reset = $("#foot-reset");
  reset.hidden = false;
  const manifest = await fetch("python/manifest.json").then((r) => r.json()).catch(() => null);
  if (manifest) {
    S.manifest = manifest;
    $("#foot-build").textContent = `Built by Victor Wambua. MIT licence. Python source from commit ${manifest.commit.slice(0, 7)}.`;
  }
  await render(false);
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
}

start();
