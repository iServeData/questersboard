// WoWQuester progressive enhancement. Every page works without this file.
(function () {
  "use strict";
  // Realm vs ruleset: show the realm field only for editions with realms, ruleset only for realmless ones.
  function syncEdition(sel) {
    var form = sel.form, opt = sel.options[sel.selectedIndex];
    var usesRealms = opt && opt.getAttribute("data-uses-realms") === "1";
    form.querySelectorAll("[data-realm-field]").forEach(function (el) {
      el.hidden = !usesRealms;
      el.querySelectorAll("input").forEach(function (i) { i.disabled = !usesRealms; i.required = usesRealms; });
    });
    form.querySelectorAll("[data-for-edition]").forEach(function (el) {
      var on = el.getAttribute("data-for-edition") === sel.value;
      el.hidden = !on;
      el.querySelectorAll("select,input").forEach(function (i) { i.disabled = !on; });
    });
    form.querySelectorAll("[data-list-prefix]").forEach(function (i) {
      i.setAttribute("list", i.getAttribute("data-list-prefix") + sel.value);
    });
  }
  document.querySelectorAll("[data-edition-select]").forEach(function (sel) {
    sel.addEventListener("change", function () { syncEdition(sel); });
    syncEdition(sel);
  });
  // Race/class suggestions follow edition -> faction -> race. Typing anything else is still allowed;
  // the server only refuses pairings the catalog rules out.
  var mapEl = document.getElementById("wq-race-map");
  if (mapEl) {
    var raceMap = {};
    try { raceMap = JSON.parse(mapEl.textContent); } catch (e) { raceMap = {}; }
    var form = mapEl.closest("form");
    var edSel = form.querySelector("[data-edition-select]"), facSel = form.querySelector("[data-faction-select]");
    var raceIn = form.querySelector("[data-race-input]"), classIn = form.querySelector("[data-class-input]");
    var raceList = document.getElementById("wq-race-list"), classList = document.getElementById("wq-class-list");
    var norm = function (t) { return (t || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(); };
    var fill = function (list, labels) {
      list.textContent = "";
      labels.forEach(function (l) { var o = document.createElement("option"); o.value = l; list.appendChild(o); });
    };
    var current = function () {
      var m = raceMap[edSel.value] || { factions: {}, classes: {} };
      var races = (m.factions || {})[facSel.value] || [];
      var typed = norm(raceIn.value);
      var race = races.filter(function (r) {
        return norm(r.label) === typed || norm(r.key) === typed || norm(r.label).split(" ")[0] === typed;
      })[0];
      return { m: m, races: races, race: race };
    };
    var syncRace = function () {
      var c = current();
      fill(raceList, c.races.map(function (r) { return r.label; }));
      var keys = c.race && c.race.classes.length ? c.race.classes : Object.keys(c.m.classes || {});
      fill(classList, keys.map(function (k) { return (c.m.classes || {})[k] || k; }));
      var hint = form.querySelector("[data-race-hint]");
      if (hint) {
        hint.textContent = c.race ? c.race.label + " can be: " + keys.map(function (k) {
          return (c.m.classes || {})[k] || k; }).join(", ") + "."
          : "Pick from the list or type your own.";
      }
    };
    [edSel, facSel].forEach(function (el) { el.addEventListener("change", syncRace); });
    raceIn.addEventListener("input", syncRace);
    raceIn.addEventListener("change", syncRace);
    syncRace();
  }
  // Show times in the viewer's own zone as well.
  document.querySelectorAll("time[data-local]").forEach(function (t) {
    var d = new Date(t.getAttribute("datetime"));
    if (!isNaN(d)) {
      t.title = t.textContent;
      t.textContent = d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
    }
  });
  // Default the time zone field to the browser's zone for new events.
  document.querySelectorAll("input[data-tz-default]").forEach(function (i) {
    try { if (!i.form.action.match(/edit$/)) i.value = Intl.DateTimeFormat().resolvedOptions().timeZone || i.value; } catch (e) {}
  });
  document.querySelectorAll("[data-confirm]").forEach(function (b) {
    b.addEventListener("click", function (e) { if (!window.confirm(b.getAttribute("data-confirm"))) e.preventDefault(); });
  });
  // Location autocomplete from the reference catalog (free text always allowed).
  document.querySelectorAll("input[data-autocomplete]").forEach(function (input) {
    var list = document.getElementById(input.getAttribute("list")), timer;
    var base = (document.querySelector('link[href$="/static/wowquester.css"]') || {}).href || "";
    var api = base.replace(/\/static\/wowquester\.css.*$/, "/api/options");
    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var ed = input.form.querySelector("[data-edition-select]");
        if (!ed || input.value.length < 2) return;
        fetch(api + "?kind=zone&edition=" + encodeURIComponent(ed.value) + "&q=" + encodeURIComponent(input.value),
              { headers: { Accept: "application/json" }, credentials: "same-origin" })
          .then(function (r) { return r.ok ? r.json() : { items: [] }; })
          .then(function (j) {
            list.textContent = "";
            (j.items || []).forEach(function (o) { var op = document.createElement("option"); op.value = o.label; list.appendChild(op); });
          }).catch(function () {});
      }, 250);
    });
  });

  // Group roles: show the filled rows plus one blank row, and an "Add more..." button for the rest.
  // Without this script every row is shown, so the form still works.
  document.querySelectorAll("[data-role-rows]").forEach(function (table) {
    var form = table.closest("form"), btn = form && form.querySelector("[data-add-role]");
    var extras = function () { return table.querySelectorAll("tr[data-role-extra]"); };
    var refresh = function () { if (btn) btn.hidden = extras().length === 0; };
    var reveal = function (tr) { tr.hidden = false; tr.removeAttribute("data-role-extra"); };
    extras().forEach(function (tr) { tr.hidden = true; });
    refresh();
    if (btn) btn.addEventListener("click", function () {
      var next = extras()[0]; if (!next) return;
      reveal(next); refresh(); next.querySelector("input").focus();
    });
    // A row filled in by "entries filled back in" must be visible.
    table.addEventListener("input", function (ev) {
      var tr = ev.target.closest("tr[data-role-extra]");
      if (tr && ev.target.value) { reveal(tr); refresh(); }
    });
  });

  // Keep what a player typed when the server refuses a form. On submit the fields are copied
  // into this tab's sessionStorage; the error page marks which form to restore; the form page
  // puts the values back and shows the message above the form. A page reached any other way
  // clears the copy. File inputs and the CSRF token are never stored.
  var KEY = "wq-form:", FLAG = "wq-restore";
  var store = window.sessionStorage;
  var actionOf = function (f) { try { return new URL(f.action, location.href).pathname; } catch (e) { return ""; } };
  document.addEventListener("submit", function (ev) {
    var f = ev.target;
    if (!f || (f.method || "").toLowerCase() !== "post" || !store) return;
    var data = {};
    Array.prototype.forEach.call(f.elements, function (el) {
      if (!el.name || el.name === "csrf" || el.type === "file" || el.type === "submit" || el.type === "button"
          || el.type === "hidden" || el.disabled) return;
      if (el.type === "checkbox" || el.type === "radio") {
        data[el.name] = data[el.name] || [];
        if (el.checked) data[el.name].push(el.value);
      } else { data[el.name] = el.value; }
    });
    try { store.setItem(KEY + actionOf(f), JSON.stringify(data)); } catch (e) { /* storage full or off */ }
  }, true);
  var restoreHere = function () {
    if (!store) return;
    var msgPage = document.querySelector("[data-restore-action]");
    if (msgPage) {
      store.setItem(FLAG, JSON.stringify({ action: msgPage.getAttribute("data-restore-action"),
                                           message: msgPage.getAttribute("data-restore-message") || "" }));
      return;
    }
    var flag = null;
    try { flag = JSON.parse(store.getItem(FLAG) || "null"); } catch (e) { flag = null; }
    store.removeItem(FLAG);
    var form = flag && Array.prototype.filter.call(document.forms, function (f) {
      return actionOf(f) === flag.action; })[0];
    var saved = null;
    if (form) { try { saved = JSON.parse(store.getItem(KEY + flag.action) || "null"); } catch (e) { saved = null; } }
    Object.keys(store).forEach(function (k) { if (k.indexOf(KEY) === 0) store.removeItem(k); });
    if (!form || !saved) return;
    var order = ["edition", "faction", "race_key"];          // parents first so dependent lists refill
    var names = Object.keys(saved).sort(function (a, b) {
      var ia = order.indexOf(a), ib = order.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib); });
    names.forEach(function (name) {
      var v = saved[name];
      Array.prototype.forEach.call(form.elements, function (el) {
        if (el.name !== name || el.type === "file" || el.type === "hidden") return;
        if (el.type === "checkbox" || el.type === "radio") { el.checked = Array.isArray(v) && v.indexOf(el.value) >= 0; }
        else if (typeof v === "string") { el.value = v; }
        else { return; }
        el.dispatchEvent(new Event("change", { bubbles: true }));
        if (el.tagName === "INPUT") el.dispatchEvent(new Event("input", { bubbles: true }));
      });
    });
    var box = document.createElement("p");
    box.className = "wq-form-error"; box.setAttribute("role", "alert");
    box.textContent = flag.message + " Your entries are filled back in.";
    form.insertBefore(box, form.firstChild);
    if (form.open === false) form.open = true;
    var det = form.closest("details"); if (det) det.open = true;
    box.scrollIntoView({ block: "center" });
  };
  window.addEventListener("pageshow", restoreHere);
  document.addEventListener("click", function (ev) {
    var a = ev.target.closest && ev.target.closest("a[data-back]");
    if (!a) return;
    // Prefer the real history entry (the browser may still hold the form as typed); fall back to the link.
    if (document.referrer && new URL(document.referrer).origin === location.origin && history.length > 1) {
      ev.preventDefault(); history.back();
    }
  });
  // Privacy / terms table of contents: mark the section being read. The last heading above the upper
  // third of the screen wins; at the very bottom, the last section does.
  var toc = document.querySelector("[data-toc]");
  if (toc) {
    var links = Array.prototype.slice.call(toc.querySelectorAll('a[href^="#"]'));
    var targets = links.map(function (a) { return document.getElementById(a.getAttribute("href").slice(1)); });
    // A clicked entry (or one in the address) stays marked until the reader scrolls on their own:
    // sections near the end of the page can't reach the top of the screen.
    var spyTick = false, pinned = -1;
    var pinTo = function (hash) {
      pinned = links.findIndex(function (a) { return a.getAttribute("href") === hash; });
    };
    links.forEach(function (a) { a.addEventListener("click", function () { pinTo(a.getAttribute("href")); spy(); }); });
    if (location.hash) { pinTo(location.hash); }
    ["wheel", "touchmove", "keydown"].forEach(function (ev) {
      window.addEventListener(ev, function () { pinned = -1; }, { passive: true });
    });
    var spy = function () {
      spyTick = false;
      var line = window.innerHeight / 3, cur = -1;
      targets.forEach(function (el, i) { if (el && el.getBoundingClientRect().top <= line) { cur = i; } });
      if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) { cur = targets.length - 1; }
      if (pinned >= 0) { cur = pinned; }
      links.forEach(function (a, i) {
        if (i === cur) { a.setAttribute("aria-current", "true"); } else { a.removeAttribute("aria-current"); }
      });
    };
    window.addEventListener("scroll", function () { if (!spyTick) { spyTick = true; requestAnimationFrame(spy); } }, { passive: true });
    window.addEventListener("resize", spy);
    spy();
  }
  // Back to top on long pages, like the rest of the site: shown after scrolling more than a screen,
  // hidden over the footer, instant when reduced motion is on.
  if (!document.querySelector(".to-top")) {
    var top = document.createElement("a");
    top.className = "to-top"; top.href = "#top"; top.setAttribute("aria-label", "Back to top");
    top.innerHTML = '<span aria-hidden="true">&uarr;</span> Top';
    document.body.appendChild(top);
    var nearEnd = false, topTick = false;
    var showTop = function () {
      topTick = false;
      top.classList.toggle("is-shown", window.scrollY > window.innerHeight * 1.2 && !nearEnd);
    };
    window.addEventListener("scroll", function () { if (!topTick) { topTick = true; requestAnimationFrame(showTop); } }, { passive: true });
    window.addEventListener("resize", showTop);
    var foot = document.querySelector("body > footer");
    if (foot && "IntersectionObserver" in window) {
      new IntersectionObserver(function (es) { nearEnd = es[0].isIntersecting; showTop(); }).observe(foot);
    }
    top.addEventListener("click", function (ev) {
      ev.preventDefault();
      var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      window.scrollTo({ top: 0, behavior: reduce ? "auto" : "smooth" });
      if (history.replaceState && location.hash) { history.replaceState(null, "", location.pathname + location.search); }
      var first = document.querySelector(".nav a");
      if (first) { first.focus({ preventScroll: true }); }
    });
    showTop();
  }
})();
