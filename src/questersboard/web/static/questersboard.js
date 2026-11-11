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
  // Submenu dropdowns: tap or click opens a section; outside click, Esc or picking a link closes it.
  // Without this file the lists still open on hover and keyboard focus.
  document.querySelectorAll("[data-subnav]").forEach(function (dd) {
    dd.classList.add("is-js");
    var btns = dd.querySelectorAll(".wq-dd-btn");
    var closeAll = function (except) {
      btns.forEach(function (b) { if (b !== except) b.setAttribute("aria-expanded", "false"); });
    };
    var mouse = window.matchMedia && window.matchMedia("(hover: hover) and (pointer: fine)").matches;
    btns.forEach(function (b) {
      if (mouse) b.parentNode.addEventListener("mouseenter", function () { closeAll(b); });
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        var open = b.getAttribute("aria-expanded") !== "true";
        closeAll(b);
        b.setAttribute("aria-expanded", open ? "true" : "false");
        if (open && menuBtn && menu && menu.classList.contains("open")) menuBtn.click();   // one menu at a time
      });
    });
    document.addEventListener("click", function (e) { if (!dd.contains(e.target)) closeAll(null); });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      var open = dd.querySelector('.wq-dd-btn[aria-expanded="true"]');
      if (open) { closeAll(null); open.focus(); }
    });
  });
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
  // Back to top on long pages, like the rest of the site: shown after scrolling more than a screen
  // and kept there down to the very bottom (this footer has nothing in the corner for it to cover);
  // instant when reduced motion is on.
  if (!document.querySelector(".to-top")) {
    var top = document.createElement("a");
    top.className = "to-top"; top.href = "#top"; top.setAttribute("aria-label", "Back to top");
    top.innerHTML = '<span aria-hidden="true">&uarr;</span> Top';
    document.body.appendChild(top);
    var topTick = false;
    var showTop = function () {
      topTick = false;
      var on = (window.scrollY || document.documentElement.scrollTop || 0) > window.innerHeight * 1.2;
      top.classList.toggle("is-shown", on);
      document.body.classList.toggle("has-to-top", on);   // lets a floating button step above it
    };
    window.addEventListener("scroll", function () { if (!topTick) { topTick = true; requestAnimationFrame(showTop); } }, { passive: true });
    window.addEventListener("resize", showTop);
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
  // Pet pictures: if Blizzard doesn't send one, drop the empty box and keep the text.
  document.querySelectorAll("img[data-hide-broken]").forEach(function (img) {
    var gone = function () {
      var fig = img.closest(".wq-figure");
      if (fig) { fig.remove(); } else { img.remove(); }   // no empty stand for a missing picture
    };
    if (img.complete && img.naturalWidth === 0 && img.getAttribute("src")) { gone(); }
    img.addEventListener("error", gone);
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
      var fig = img.closest(".wq-figure");
      if (fig) fig.hidden = true;                         // no stand without a figure on it
      var p = img.closest(".wq-gear-center") || img.parentNode;
      if (p && !p.querySelector("img[data-portrait]:not([hidden])")) {
        p.querySelectorAll("[data-portrait-missing]").forEach(function (n) { n.hidden = false; });
      }
    };
    if (img.complete && img.naturalWidth === 0) { miss(); } else { img.addEventListener("error", miss); }
  });
  // My characters: the add/edit form is a dialog. Without this script it shows in the page when the
  // address asks for it (?add=1 or ?edit=N), and the buttons are plain links to those addresses.
  var dlg = document.querySelector("[data-character-dialog]");
  if (dlg && typeof dlg.showModal === "function") {
    var openDlg = function () {
      if (!dlg.open) dlg.showModal();
      var first = dlg.querySelector("input:not([type=hidden]):not([disabled]), select");
      if (first) first.focus();
    };
    if (dlg.hasAttribute("open")) { dlg.removeAttribute("open"); dlg.classList.add("is-modal"); openDlg(); }
    document.querySelectorAll("[data-open-character]").forEach(function (a) {
      a.addEventListener("click", function (e) {
        if (dlg.getAttribute("data-mode") !== "add") return;   // editing one: load a blank form instead
        e.preventDefault();
        dlg.classList.add("is-modal");
        openDlg();
      });
    });
    dlg.querySelectorAll("[data-close-dialog]").forEach(function (b) {
      b.addEventListener("click", function (e) { e.preventDefault(); dlg.close(); });
    });
    dlg.addEventListener("close", function () {
      if (/[?&](edit|add)=/.test(location.search) && history.replaceState) {
        history.replaceState(null, "", location.pathname);
      }
    });
  }
  // Pill tabs over a character's details: show one tab's sections at a time. Without this script the
  // tab bar stays hidden and every section shows. On a single character's page the tab follows #hash.
  var tabBars = document.querySelectorAll("[data-tabs]");
  tabBars.forEach(function (bar) {
    var box = bar.closest(".wq-details"), btns = bar.querySelectorAll("[data-tab]");
    if (!box) return;
    var show = function (key, opening) {
      btns.forEach(function (b) {
        var on = b.getAttribute("data-tab") === key;
        b.setAttribute("aria-selected", on ? "true" : "false");
        b.tabIndex = on ? 0 : -1;
      });
      box.querySelectorAll("[data-tab-panel]").forEach(function (d) {
        var on = d.getAttribute("data-tab-panel") === key;
        d.hidden = !on;
        if (on && opening) d.open = true;
      });
    };
    var keys = Array.prototype.map.call(btns, function (b) { return b.getAttribute("data-tab"); });
    var start = tabBars.length === 1 && keys.indexOf(location.hash.slice(1)) >= 0 ? location.hash.slice(1) : keys[0];
    show(start, start !== keys[0]);
    bar.hidden = false;
    btns.forEach(function (b, i) {
      b.addEventListener("click", function () {
        show(keys[i], true);
        if (tabBars.length === 1 && history.replaceState) history.replaceState(null, "", "#" + keys[i]);
      });
      b.addEventListener("keydown", function (e) {
        var j = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : -2;
        if (j === -2) return;
        e.preventDefault();
        j = (j + btns.length) % btns.length;
        btns[j].focus(); btns[j].click();
      });
    });
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
  // Long tables: the filter bar and headings stay in view (CSS sticky). The headings sit just under the
  // bar, so its height is measured into --wq-bar-h. Headings sort in the browser; the links still work
  // without this file, and the phone "Sort by" picker drives the same sort.
  document.querySelectorAll("[data-sticky-bar]").forEach(function (bar) {
    var root = document.documentElement;
    var setH = function () { root.style.setProperty("--wq-bar-h", bar.offsetHeight + "px"); };
    setH();
    if (window.ResizeObserver) { new ResizeObserver(setH).observe(bar); } else { window.addEventListener("resize", setH); }
  });
  var narrow = window.matchMedia ? window.matchMedia("(max-width: 700px)") : null;
  document.querySelectorAll("[data-filter-box]").forEach(function (box) {
    var fit = function () { box.open = !(narrow && narrow.matches); };
    fit();
    if (narrow && narrow.addEventListener) { narrow.addEventListener("change", fit); }
  });
  document.querySelectorAll("table[data-sortable]").forEach(function (table) {
    var body = table.tBodies[0], heads = table.querySelectorAll("th[data-sort-key]");
    var pick = document.querySelector("[data-sort-select]");
    if (!body || !heads.length) { return; }
    var cmp = function (a, b, num) {
      if (a === "" || b === "") { return a === b ? 0 : (a === "" ? 1 : -1); }
      if (num) { return parseFloat(a) - parseFloat(b); }
      return a.localeCompare(b, undefined, { sensitivity: "base", numeric: true });
    };
    var sortBy = function (key, desc) {
      var th = table.querySelector('th[data-sort-key="' + key + '"]');
      if (!th) { return; }
      var col = Array.prototype.indexOf.call(th.parentNode.children, th), num = th.hasAttribute("data-sort-num");
      var rows = Array.prototype.slice.call(body.rows).map(function (tr, i) {
        var td = tr.cells[col], v = td ? (td.getAttribute("data-v") || "").trim() : "";
        var name = tr.cells[0] ? (tr.cells[0].getAttribute("data-v") || "") : "";
        return { tr: tr, v: v, name: name, i: i };
      });
      rows.sort(function (a, b) {
        var c = cmp(a.v, b.v, num);
        if (c !== 0 && a.v !== "" && b.v !== "" && desc) { c = -c; }
        return c || a.name.localeCompare(b.name, undefined, { sensitivity: "base" }) || a.i - b.i;
      });
      var frag = document.createDocumentFragment();
      rows.forEach(function (r) { frag.appendChild(r.tr); });
      body.appendChild(frag);
      heads.forEach(function (h) {
        h.setAttribute("aria-sort", h === th ? (desc ? "descending" : "ascending") : "none");
      });
      if (pick) { pick.value = key + (desc ? ":desc" : ""); }
      if (window.history && history.replaceState && window.URLSearchParams) {
        var q = new URLSearchParams(location.search);
        q.set("sort", key);
        if (desc) { q.set("dir", "desc"); } else { q.delete("dir"); }
        history.replaceState(null, "", location.pathname + "?" + q.toString() + location.hash);
      }
    };
    heads.forEach(function (th) {
      var a = th.querySelector("[data-sort-link]");
      if (!a) { return; }
      a.setAttribute("role", "button");
      a.addEventListener("click", function (e) {
        e.preventDefault();
        sortBy(th.getAttribute("data-sort-key"), th.getAttribute("aria-sort") === "ascending");
      });
    });
    if (pick) {
      pick.addEventListener("change", function () {
        var p = pick.value.split(":");
        sortBy(p[0], p[1] === "desc");
      });
    }
  });
})();

/* Account menu, ⋯ menus, My characters list/detail, sharing that saves as you click, Battle.net panel. */
(function () {
  document.documentElement.classList.add("wq-js");
  var menus = Array.prototype.slice.call(document.querySelectorAll("details.wq-acct, details.wq-more"));
  document.addEventListener("click", function (e) {
    menus.forEach(function (d) { if (d.open && !d.contains(e.target)) { d.open = false; } });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") { return; }
    menus.forEach(function (d) {
      if (d.open) { d.open = false; var s = d.querySelector("summary"); if (s) { s.focus(); } }
    });
    var sheet = document.querySelector("[data-sheet]");
    if (sheet) { var x = sheet.querySelector(".wq-dialog-x"); if (x) { location.href = x.href; } }
  });

  var search = document.querySelector("[data-char-search]");
  if (search) {
    var items = Array.prototype.slice.call(document.querySelectorAll("[data-char-cards] > li"));
    var none = document.querySelector("[data-char-none]");
    search.addEventListener("input", function () {
      var q = search.value.trim().toLowerCase(), shown = 0;
      items.forEach(function (li) {
        var hit = !q || li.getAttribute("data-name").indexOf(q) !== -1;
        li.hidden = !hit;
        if (hit) { shown += 1; }
      });
      if (none) { none.hidden = shown !== 0; }
    });
  }

  document.querySelectorAll("[data-open-details]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      var d = document.getElementById(a.getAttribute("data-open-details"));
      if (!d) { return; }
      e.preventDefault();
      d.open = true;
      var m = a.closest("details.wq-more");
      if (m) { m.open = false; }
      d.scrollIntoView({ block: "start" });
      var s = d.querySelector("summary");
      if (s) { s.focus({ preventScroll: true }); }
    });
  });

  var refused = document.querySelector("[data-share-refused]");
  if (refused) {
    var dismiss = refused.querySelector("[data-dismiss]");
    if (dismiss) {
      dismiss.addEventListener("click", function (e) { e.preventDefault(); refused.hidden = true; });
    }
  }
  document.querySelectorAll("form[data-autosave]").forEach(function (form) {
    var radios = Array.prototype.slice.call(form.querySelectorAll("input[type=radio]"));
    var before = {};
    radios.forEach(function (r) { if (r.checked) { before[r.name] = r; } });
    form.addEventListener("change", function (e) {
      var r = e.target;
      if (!r || r.type !== "radio") { return; }
      if (r.hasAttribute("data-locked")) {
        r.checked = false;
        if (before[r.name]) { before[r.name].checked = true; }
        if (refused) { refused.hidden = false; refused.scrollIntoView({ block: "nearest" }); }
        return;
      }
      if (r.name === "share_all") {
        radios.forEach(function (o) { if (o.name !== "share_all" && o.value === r.value) { o.checked = true; } });
      } else {
        radios.forEach(function (o) { if (o.name === "share_all") { o.checked = false; } });
      }
      form.submit();
    });
  });

  var picker = document.querySelector("form[data-picker]");
  if (picker) {
    var btn = picker.querySelector("[data-pick-count]");
    var count = function () {
      var n = picker.querySelectorAll("input[data-pick]:checked").length;
      btn.textContent = "Add " + n + " character" + (n === 1 ? "" : "s");
      btn.disabled = n === 0;
    };
    picker.addEventListener("change", count);
    count();
  }
})();
