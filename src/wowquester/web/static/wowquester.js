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
})();
