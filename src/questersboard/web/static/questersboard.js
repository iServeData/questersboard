// QuestersBoard progressive enhancement. Every page works without this file.
(function () {
  "use strict";
  // Phone menu: the community stylesheet hides the menu under 760px until this button opens it.
  var menuBtn = document.querySelector(".menu-btn"), menu = document.getElementById("menu");
  if (menuBtn && menu) {
    var setMenu = function (open) {
      menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
      menuBtn.textContent = open ? "Close" : "Menu";
      menu.classList.toggle("open", open);
    };
    menuBtn.addEventListener("click", function () { setMenu(menuBtn.getAttribute("aria-expanded") !== "true"); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && menu.classList.contains("open")) { setMenu(false); menuBtn.focus(); }
    });
    menu.addEventListener("click", function (e) { if (e.target.tagName === "A") setMenu(false); });
  }
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
  // Race and class are real dropdowns that follow edition -> faction -> race. iOS Safari's datalist
  // suggestions only match what's already typed, so a filled-in field showed no list at all.
  // Each dropdown writes into the (named) text field the server reads; "Other" reveals that field
  // for anything the catalog doesn't list. Editions without a catalog show just the text field.
  var mapEl = document.getElementById("wq-race-map");
  if (mapEl) {
    var raceMap = {};
    try { raceMap = JSON.parse(mapEl.textContent); } catch (e) { raceMap = {}; }
    var form = mapEl.closest("form");
    var edSel = form.querySelector("[data-edition-select]"), facSel = form.querySelector("[data-faction-select]");
    var raceIn = form.querySelector("[data-race-input]"), classIn = form.querySelector("[data-class-input]");
    var raceSel = form.querySelector("[data-race-select]"), classSel = form.querySelector("[data-class-select]");
    var hint = form.querySelector("[data-race-hint]");
    var OTHER = "__other__";
    var norm = function (t) { return (t || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(); };
    var match = function (items, text) {         // items: [{key, label}]
      var n = norm(text);
      if (!n) return null;
      return items.filter(function (it) {
        return norm(it.key) === n || norm(it.label) === n || norm(it.label).split(" ")[0] === n; })[0] || null;
    };
    var setOptions = function (sel, prompt, items) {
      sel.textContent = "";
      var first = document.createElement("option"); first.value = ""; first.textContent = prompt;
      sel.appendChild(first);
      items.forEach(function (it) {
        var o = document.createElement("option"); o.value = it.key; o.textContent = it.label; sel.appendChild(o); });
      var other = document.createElement("option"); other.value = OTHER; other.textContent = "Other (type it)";
      sel.appendChild(other);
    };
    var show = function (el, on) { (el.closest("label") || el).hidden = !on; };
    // Point a dropdown at its text field's value; returns the matched item or null.
    var attach = function (sel, input, items) {
      if (!items.length) {                       // no catalog: plain text entry
        show(sel, false); sel.required = false; show(input, true); input.required = true;
        return null;
      }
      show(sel, true); sel.required = true;
      var hit = match(items, input.value);
      if (hit) { sel.value = hit.key; input.value = hit.key; show(input, false); input.required = false; }
      else if (input.dataset.typed === "1") { sel.value = OTHER; show(input, true); input.required = true; }
      else { sel.value = ""; input.value = ""; show(input, false); input.required = false; }
      return hit;
    };
    var state = function () {
      var m = raceMap[edSel.value] || { factions: {}, classes: {} };
      var races = ((m.factions || {})[facSel.value] || []).map(function (r) {
        return { key: r.key, label: r.label, classes: r.classes || [] }; });
      return { m: m, races: races };
    };
    // Each list only offers what fits the other: a picked class narrows the races, a picked race
    // narrows the classes. The first choice in each list clears it, which widens the other again.
    var pretty = function (k) { return k.replace(/_/g, " ").replace(/\b[a-z]/g, function (x) { return x.toUpperCase(); }); };
    var sync = function () {
      var c = state(), cls = c.m.classes || {};
      var allClasses = Object.keys(cls).map(function (k) { return { key: k, label: cls[k] }; });
      var raceTyped = raceIn.dataset.typed === "1", classTyped = classIn.dataset.typed === "1";
      var cleared = [];
      // classes some race of this faction can be (all of them when there's no race list)
      var pool = allClasses;
      if (c.races.length) {
        var any = {};
        c.races.forEach(function (r) { (r.classes.length ? r.classes : Object.keys(cls)).forEach(function (k) { any[k] = 1; }); });
        pool = allClasses.filter(function (i) { return any[i.key]; });
      }
      var curClass = classTyped ? null : match(pool, classIn.value);
      if (!classTyped && classIn.value && !curClass && c.races.length) {
        cleared.push(cls[classIn.value] || pretty(classIn.value)); classIn.value = "";
      }
      var fits = function (r) { return !curClass || !r.classes.length || r.classes.indexOf(curClass.key) >= 0; };
      var races = c.races.filter(fits);
      var race = raceTyped ? null : match(races, raceIn.value);
      if (!raceTyped && raceIn.value && !race && c.races.length) {
        var gone = match(c.races, raceIn.value);
        cleared.push(gone ? gone.label : pretty(raceIn.value)); raceIn.value = "";
      }
      setOptions(raceSel, race ? "Clear race (show all classes)" : curClass ? "Pick a race (" + curClass.label + ")" : "Pick a race", races);
      race = attach(raceSel, raceIn, races);
      var items = race && race.classes.length
        ? allClasses.filter(function (i) { return race.classes.indexOf(i.key) >= 0; }) : pool;
      setOptions(classSel, curClass ? "Clear class (show all races)" : race ? "Pick a class (" + race.label + ")" : "Pick a class", items);
      var klass = attach(classSel, classIn, items);
      if (!hint) return;
      var msg = cleared.length ? cleared.join(" and ") + (cleared.length > 1 ? " aren't" : " isn't") +
        " available for this edition and faction, so " + (cleared.length > 1 ? "they were" : "it was") + " cleared. " : "";
      var list = function (xs) { return xs.map(function (i) { return i.label; }).join(", "); };
      hint.textContent = msg + (!c.races.length ? "Type the race and class."
        : race ? race.label + " can be: " + list(items) + "."
        : klass ? "Races that can be " + klass.label + ": " + list(races) + "."
        : "Pick a race or a class first; the other list then shows only what fits.");
      hint.classList.toggle("wq-warn", !!msg);
    };
    raceSel.addEventListener("change", function () {
      if (raceSel.value === OTHER) { raceIn.value = ""; raceIn.dataset.typed = "1"; show(raceIn, true); raceIn.required = true; raceIn.focus(); }
      else { raceIn.value = raceSel.value; raceIn.dataset.typed = ""; }
      sync();
    });
    classSel.addEventListener("change", function () {
      if (classSel.value === OTHER) { classIn.value = ""; classIn.dataset.typed = "1"; show(classIn, true); classIn.required = true; classIn.focus(); }
      else { classIn.value = classSel.value; classIn.dataset.typed = ""; }
      sync();
    });
    raceIn.addEventListener("input", function () { raceIn.dataset.typed = "1"; });
    classIn.addEventListener("input", function () { classIn.dataset.typed = "1"; });
    // Restoring a form after an error sets the text fields and fires change: re-sync from them.
    var hiddenField = function (el) { return (el.closest("label") || el).hidden; };
    raceIn.addEventListener("change", function () { if (hiddenField(raceIn)) sync(); });
    classIn.addEventListener("change", function () { if (hiddenField(classIn)) { classIn.dataset.typed = ""; sync(); } });
    // A saved race or class the catalog doesn't list was typed by hand: keep it, under "Other".
    (function () {
      var c = state();
      if (raceIn.value && c.races.length && !match(c.races, raceIn.value)) raceIn.dataset.typed = "1";
      var all = Object.keys(c.m.classes || {}).map(function (k) { return { key: k, label: c.m.classes[k] }; });
      if (classIn.value && all.length && !match(all, classIn.value)) classIn.dataset.typed = "1";
    })();
    [edSel, facSel].forEach(function (el) { el.addEventListener("change", function () { sync(); }); });
    sync();
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
  // (the picker only switches when the browser's zone is in its list)
  document.querySelectorAll("select[data-tz-default]").forEach(function (sel) {
    try {
      var z = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (z && sel.querySelector('option[value="' + z.replace(/"/g, "") + '"]')) sel.value = z;
    } catch (e) {}
  });
  document.querySelectorAll("[data-confirm]").forEach(function (b) {
    b.addEventListener("click", function (e) { if (!window.confirm(b.getAttribute("data-confirm"))) e.preventDefault(); });
  });
  // Location autocomplete from the reference catalog (free text always allowed).
  document.querySelectorAll("input[data-autocomplete]").forEach(function (input) {
    var list = document.getElementById(input.getAttribute("list")), timer, found = [];
    // When the typed place matches a Blizzard dungeon/raid, mark difficulties it doesn't offer and
    // suggest its minimum level. Nothing is forced: organizers can still pick and type anything valid.
    function hint() {
      var o = found.filter(function (x) { return x.label.toLowerCase() === input.value.trim().toLowerCase(); })[0];
      input.form.querySelectorAll("select[name=difficulty]").forEach(function (sel) {
        Array.prototype.forEach.call(sel.options, function (op) {
          if (!op.value) return;
          var base = op.getAttribute("data-name") || op.textContent;
          op.setAttribute("data-name", base);
          var off = o && o.modes && o.modes.indexOf(base) < 0;
          op.textContent = off ? base + " · not offered here" : base;
        });
      });
      var lv = input.form.querySelector("[data-level-hint]"), min = input.form.querySelector("input[name=level_min]");
      if (lv) lv.textContent = o && o.minimum_level ? "Blizzard's dungeon journal suggests level " + o.minimum_level + "+." : "";
      if (o && o.minimum_level && min && !min.value) min.value = o.minimum_level;
    }
    input.addEventListener("change", hint);
    var base = (document.querySelector('link[href$="/static/questersboard.css"]') || {}).href || "";
    var api = base.replace(/\/static\/questersboard\.css.*$/, "/api/options");
    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var ed = input.form.querySelector("[data-edition-select]");
        if (!ed || input.value.length < 2) return;
        fetch(api + "?kind=location&edition=" + encodeURIComponent(ed.value) + "&q=" + encodeURIComponent(input.value),
              { headers: { Accept: "application/json" }, credentials: "same-origin" })
          .then(function (r) { return r.ok ? r.json() : { items: [] }; })
          .then(function (j) {
            list.textContent = "";
            found = j.items || [];
            found.forEach(function (o) { var op = document.createElement("option"); op.value = o.label; if (o.parent) { op.label = o.parent; } list.appendChild(op); });
            hint();
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
  // "Set all" on the import form: copy the choice to every row; changing a row afterwards clears it.
  document.querySelectorAll("select[data-share-all]").forEach(function (all) {
    var form = all.form, rows = form ? form.querySelectorAll("select[data-share-one]") : [];
    all.addEventListener("change", function () {
      if (!all.value) { return; }
      rows.forEach(function (s) { s.value = all.value; });
    });
    rows.forEach(function (s) { s.addEventListener("change", function () { all.value = ""; }); });
  });
  // Top of My characters: Save needs a level picked first.
  document.querySelectorAll("select[data-bulk-level]").forEach(function (sel) {
    var btn = sel.form && sel.form.querySelector("[data-bulk-share]");
    if (!btn) { return; }
    var sync = function () { btn.disabled = !sel.value; };
    sel.addEventListener("change", sync); sync();
  });
  // Imported portraits: if Blizzard has no picture (broken image), say so instead of an empty box.
  document.querySelectorAll("img[data-portrait]").forEach(function (img) {
    var miss = function () {
      img.hidden = true;
      var p = img.parentNode, note = p && p.querySelector("[data-portrait-missing]");
      if (note && !p.querySelector("img[data-portrait]:not([hidden])")) note.hidden = false;
    };
    if (img.complete && img.naturalWidth === 0) { miss(); } else { img.addEventListener("error", miss); }
  });
  // Gear and talent tooltips: tap (phones, iPad) opens one and closes the others; Escape or tapping
  // elsewhere closes it. Hover and keyboard focus work through CSS.
  var tips = document.querySelectorAll("[data-tip]");
  var closeAll = function (except) {
    tips.forEach(function (t) {
      if (t !== except && t.classList.contains("is-open")) {
        t.classList.remove("is-open");
        var b = t.querySelector(".wq-item-btn"); if (b) { b.setAttribute("aria-expanded", "false"); }
      }
    });
  };
  tips.forEach(function (t) {
    var b = t.querySelector(".wq-item-btn");
    if (!b) { return; }
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var open = !t.classList.contains("is-open");
      closeAll(t);
      t.classList.toggle("is-open", open);
      b.setAttribute("aria-expanded", open ? "true" : "false");
      if (!open) { b.blur(); }
    });
    // With a mouse, a clicked tooltip closes when the pointer leaves the tile (hover still shows it).
    t.addEventListener("mouseleave", function () {
      if (!window.matchMedia || !window.matchMedia("(hover: hover)").matches) { return; }
      closeAll(null);
      if (t.contains(document.activeElement)) { document.activeElement.blur(); }
    });
  });
  if (tips.length) {
    document.addEventListener("click", function (ev) { if (!ev.target.closest("[data-tip]")) { closeAll(null); } });
    document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") { closeAll(null); } });
  }
})();
