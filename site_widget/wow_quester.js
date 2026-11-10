/* QuestersBoard widgets for a static site page. No dependencies, no innerHTML (textContent only).
 *
 * 1. Upcoming events list (reads <member_path>/upcoming.json once):
 *   <div data-wq-upcoming data-endpoint="/wow/quester/upcoming.json" data-limit="5"
 *        data-empty="No QuestersBoard events scheduled yet.">
 *     <ul class="facts"> ...static facts... </ul>
 *   </div>
 *   Optional: data-keep-fallback="true" keeps the static facts and appends the events after them.
 *   Renders <ul class="facts wq-upcoming"><li><span>WHEN</span><a>TITLE</a> · detail</li></ul>.
 *
 * 2. Community status card (reads <member_path>/status.json, polled every 60 s):
 *   <div class="panel status-card" data-wq-status data-endpoint="/wow/quester/status.json">
 *     <dl class="st-list">
 *       <div class="st-row" data-component="events|event_board|bot|discord|worker" data-tone="unknown">
 *         <dt>Label</dt><dd><span class="st-dot"></span><span class="st-state">Unknown</span></dd>
 *         <dd class="st-meta"><span class="st-age">checking…</span></dd></div> ...
 *     </dl><p class="st-foot"></p>
 *   </div>
 *   Every row starts as Unknown; a missing, old or failed report never shows green.
 *
 * 3. Next event card (same status.json):
 *   <div class="panel sched-card" data-wq-next data-endpoint="/wow/quester/status.json">
 *     <div class="card-head"><h3>Next event</h3><span class="live-chip" data-wq-chip hidden>Live now</span></div>
 *     <div data-wq-event hidden></div>
 *     <p class="note" data-wq-empty hidden>No events scheduled yet.</p>
 *     <p class="note" data-wq-down hidden>Events can't be loaded right now.</p>
 *   </div>
 *   "Live now" appears only when an organiser has launched the event (phase live / running_over).
 *
 * Any element with data-wq-until="<ISO instant>" is removed once that instant has passed
 * (e.g. a launch-day fallback inside the Next event card). Without JavaScript it stays visible.
 * Times are shown in the visitor's time zone.
 */
(function () {
  "use strict";
  var POLL_MS = 60000, TICK_MS = 15000, TIMEOUT_MS = 8000;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function t(iso) { if (typeof iso !== "string") return null; var v = Date.parse(iso); return isNaN(v) ? null : v; }
  function when(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return "";
    try {
      return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
    } catch (e) { return d.toISOString().slice(0, 16).replace("T", " ") + " UTC"; }
  }
  function clock(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return "";
    try { return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); }
    catch (e) { return d.toISOString().slice(11, 16) + " UTC"; }
  }
  function age(ms) {
    if (ms == null) return null;
    var s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 172800) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }
  function safeUrl(u) {
    try {
      var p = new URL(u, location.href);
      return (p.protocol === "https:" || p.origin === location.origin) ? p.href : null;
    } catch (e) { return null; }
  }
  function getJson(url) {
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = ctl ? setTimeout(function () { ctl.abort(); }, TIMEOUT_MS) : null;
    return fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" },
                        signal: ctl ? ctl.signal : undefined })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) { if (timer) clearTimeout(timer); return j; },
            function (e) { if (timer) clearTimeout(timer); throw e; });
  }

  // ---------------------------------------------------------------- data-wq-until
  function expire() {
    var els = document.querySelectorAll("[data-wq-until]");
    for (var i = 0; i < els.length; i++) {
      var u = t(els[i].getAttribute("data-wq-until"));
      if (u != null && Date.now() >= u && els[i].parentNode) els[i].parentNode.removeChild(els[i]);
    }
  }

  // ---------------------------------------------------------------- 1. upcoming list
  function renderUpcoming(box, items) {
    var ul = el("ul", "facts wq-upcoming");
    items.forEach(function (it) {
      var li = el("li");
      li.appendChild(el("span", null, when(it.start)));
      var href = safeUrl(it.url);
      var title = href ? el("a", null, it.title) : el("strong", null, it.title);
      if (href) title.href = href;
      li.appendChild(title);
      var bits = [it.edition, it.ruleset, it.activity_type].filter(Boolean);
      if (it.state === "in_progress") bits.unshift("live now");
      if (bits.length) li.appendChild(document.createTextNode(" · " + bits.join(" · ")));
      ul.appendChild(li);
    });
    if (box.getAttribute("data-keep-fallback") !== "true") {
      while (box.firstChild) box.removeChild(box.firstChild);
    }
    box.appendChild(ul);
    box.setAttribute("data-wq-state", "live");
  }
  function loadUpcoming(box) {
    var ep = box.getAttribute("data-endpoint") || "/wow/quester/upcoming.json";
    var n = parseInt(box.getAttribute("data-limit") || "5", 10);
    if (!(n > 0 && n <= 50)) n = 5;
    getJson(ep + (ep.indexOf("?") < 0 ? "?" : "&") + "n=" + n)
      .then(function (data) {
        var items = (data && Array.isArray(data.items)) ? data.items : [];
        if (items.length) { renderUpcoming(box, items); return; }
        var empty = box.getAttribute("data-empty");
        if (empty && box.getAttribute("data-keep-fallback") === "true") box.appendChild(el("p", "note", empty));
        box.setAttribute("data-wq-state", "empty");
      })
      .catch(function () { box.setAttribute("data-wq-state", "fallback"); });
  }

  // ---------------------------------------------------------------- shared status.json
  var STATUS = { data: null, failed: false, cards: [], nexts: [], endpoint: null };
  var SVC = {
    event_board: { responding: ["Responding", "ok"] },
    bot: { running: ["Running", "ok"], stale: ["Not checking in", "bad"], unknown: ["Unknown", "unknown"] },
    discord: { connected: ["Connected", "ok"], disconnected: ["Disconnected", "bad"], unknown: ["Unknown", "unknown"] },
    worker: { running: ["Running", "ok"], stale: ["Not checking in", "bad"], unknown: ["Unknown", "unknown"] }
  };
  function validStatus(d) {
    if (!d || typeof d !== "object" || d.schema_version !== 1 || t(d.generated_at) == null) return false;
    if (!d.services || typeof d.services !== "object") return false;
    for (var k in SVC) {
      if (!d.services[k] || !SVC[k].hasOwnProperty(d.services[k].state)) return false;
    }
    return true;
  }
  function fresh(d) {
    var maxAge = ((d.expiry && d.expiry.public_json_seconds) || 180) * 1000, a = Date.now() - t(d.generated_at);
    return a <= maxAge && a > -60000;
  }
  function row(card, name, label, tone, meta) {
    var r = card.querySelector('[data-component="' + name + '"]');
    if (!r) return;
    r.setAttribute("data-tone", tone);
    var s = r.querySelector(".st-state"), m = r.querySelector(".st-age");
    if (s) s.textContent = label;
    if (m) m.textContent = meta;
  }
  function foot(card, text) { var f = card.querySelector(".st-foot"); if (f) f.textContent = text; }

  function renderStatusCard(card) {
    var d = STATUS.data;
    if (!d || !fresh(d)) {
      var why = !d ? (STATUS.failed ? "no response" : "checking…") : "last report " + age(t(d.generated_at));
      row(card, "event_board", STATUS.failed && !d ? "Unreachable" : "Unknown", STATUS.failed && !d ? "bad" : "unknown", why);
      ["events", "bot", "discord", "worker"].forEach(function (k) { row(card, k, "Unknown", "unknown", why); });
      foot(card, STATUS.failed ? "Event board unavailable." : "Checking…");
      return;
    }
    var sv = d.services;
    Object.keys(SVC).forEach(function (k) {
      var x = sv[k], L = SVC[k][x.state];
      var obs = t(x.observed_at);
      row(card, k, L[0], L[1], obs != null ? (k === "event_board" ? "checked " : "last check-in ") + age(obs)
                                           : "no check-in yet");
    });
    var e = d.event, live = d.live_count || 0;
    if (live > 0) row(card, "events", live === 1 ? "Live now" : live + " live now", "ok",
                      e ? e.title : "");
    else if (e) row(card, "events", "None live", "unknown", "next: " + when(e.start));
    else row(card, "events", "None scheduled", "unknown", "organisers post events in Discord");
    foot(card, "Updated " + age(t(d.generated_at)) + ".");
  }

  var PHASE = {
    live: ["Live now", "Launched by the organiser"],
    running_over: ["Live · running over", "Running past its planned end"],
    starting_soon: ["Starting soon", null],
    waiting: ["Start time passed", "Waiting for the organiser to launch"],
    scheduled: [null, null]
  };
  function renderNext(card) {
    var box = card.querySelector("[data-wq-event]"), chip = card.querySelector("[data-wq-chip]"),
        empty = card.querySelector("[data-wq-empty]"), down = card.querySelector("[data-wq-down]"),
        d = STATUS.data;
    if (!box) return;
    var e = d && fresh(d) ? d.event : null;
    while (box.firstChild) box.removeChild(box.firstChild);
    if (!e || !PHASE.hasOwnProperty(e.phase)) {
      box.hidden = true;
      if (chip) chip.hidden = true;
      // Only say "nothing scheduled" when the board answered and the launch fallback is gone.
      var fallback = !!card.querySelector("[data-wq-until]"), answered = !!(d && fresh(d));
      if (empty) empty.hidden = !answered || fallback;
      if (down) down.hidden = answered || fallback || !STATUS.failed;
      return;
    }
    if (empty) empty.hidden = true;
    if (down) down.hidden = true;
    var ph = PHASE[e.phase], isLive = e.phase === "live" || e.phase === "running_over";
    if (chip) {
      chip.hidden = !isLive;
      chip.textContent = ph[0] || "Live now";
    }
    if (!isLive && ph[0]) box.appendChild(el("p", "wq-phase", "")).appendChild(el("span", "status-chip", ph[0]));
    box.appendChild(el("p", "big-when", isLive ? "Now · until " + clock(e.end) : when(e.start)));
    var href = safeUrl(e.url), tp = el("p", "wq-title");
    var a = href ? el("a", null, e.title) : el("strong", null, e.title);
    if (href) a.href = href;
    tp.appendChild(a);
    box.appendChild(tp);
    var bits = [e.edition, e.ruleset, e.activity_type].filter(Boolean);
    if (bits.length) box.appendChild(el("p", "note", bits.join(" · ")));
    if (ph[1]) box.appendChild(el("p", "note", ph[1]));
    var roster = "Signed up " + (e.signed_up || 0) + (typeof e.capacity === "number" ? "/" + e.capacity : "");
    if (Array.isArray(e.needs) && e.needs.length) {
      roster += " · Need " + e.needs.map(function (n) { return n.missing + " " + n.label; }).join(", ");
    }
    box.appendChild(el("p", "sched-foot", roster));
    if (isLive && e.voice && typeof e.voice.in_voice === "number") {
      box.appendChild(el("p", "sched-foot", e.voice.in_voice + " in voice · checked " + age(t(e.voice.observed_at))));
    }
    if ((d.live_count || 0) > 1) box.appendChild(el("p", "note", "+" + (d.live_count - 1) + " more live"));
    if (href) {
      var b = el("a", "btn block", isLive ? "Open event" : "Sign up / details");
      b.href = href;
      box.appendChild(b);
    }
    box.hidden = false;
  }
  function renderAll() {
    expire();
    STATUS.cards.forEach(renderStatusCard);
    STATUS.nexts.forEach(renderNext);
  }
  function poll() {
    getJson(STATUS.endpoint)
      .then(function (d) {
        if (!validStatus(d)) throw new Error("invalid");
        STATUS.data = d; STATUS.failed = false;
      })
      .catch(function () { STATUS.failed = true; if (STATUS.data && !fresh(STATUS.data)) STATUS.data = null; })
      .then(renderAll);
  }

  function init() {
    expire();
    var boxes = document.querySelectorAll("[data-wq-upcoming]");
    if (window.fetch) for (var i = 0; i < boxes.length; i++) loadUpcoming(boxes[i]);
    STATUS.cards = [].slice.call(document.querySelectorAll("[data-wq-status]"));
    STATUS.nexts = [].slice.call(document.querySelectorAll("[data-wq-next]"));
    var first = STATUS.cards[0] || STATUS.nexts[0];
    if (!first || !window.fetch) return;
    STATUS.endpoint = first.getAttribute("data-endpoint") || "/wow/quester/status.json";
    renderAll();
    poll();
    setInterval(function () { if (!document.hidden) poll(); }, POLL_MS);
    setInterval(renderAll, TICK_MS);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
