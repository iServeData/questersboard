/* WoWQuester upcoming-events widget for a static site page. No dependencies, no innerHTML.
 *
 * Markup (the children are the static fallback and stay if the request fails or nothing is scheduled):
 *   <div data-wq-upcoming data-endpoint="/wow/quester/upcoming.json" data-limit="5"
 *        data-empty="No WoWQuester events scheduled yet.">
 *     <ul class="facts"> ...static launch facts... </ul>
 *   </div>
 *   <script src="/assets/js/wow_quester.js" defer></script>
 *
 * Optional: data-keep-fallback="true" keeps the static facts and appends the events after them.
 * Renders <ul class="facts wq-upcoming"><li><span>WHEN</span><a>TITLE</a> · detail</li></ul>
 * using the host site's existing .facts styling. Times are shown in the visitor's time zone.
 */
(function () {
  "use strict";
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function when(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return "";
    try {
      return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
    } catch (e) { return d.toISOString().slice(0, 16).replace("T", " ") + " UTC"; }
  }
  function safeUrl(u) {
    try {
      var p = new URL(u, location.href);
      return (p.protocol === "https:" || p.origin === location.origin) ? p.href : null;
    } catch (e) { return null; }
  }
  function render(box, items) {
    var ul = el("ul", "facts wq-upcoming");
    items.forEach(function (it) {
      var li = el("li");
      li.appendChild(el("span", null, when(it.start)));
      var href = safeUrl(it.url);
      var title = href ? el("a", null, it.title) : el("strong", null, it.title);
      if (href) title.href = href;
      li.appendChild(title);
      var bits = [it.edition, it.ruleset, it.activity_type].filter(Boolean);
      if (it.state === "in_progress") bits.unshift("happening now");
      if (bits.length) li.appendChild(document.createTextNode(" · " + bits.join(" · ")));
      ul.appendChild(li);
    });
    if (box.getAttribute("data-keep-fallback") !== "true") {
      while (box.firstChild) box.removeChild(box.firstChild);
    }
    box.appendChild(ul);
    box.setAttribute("data-wq-state", "live");
  }
  function load(box) {
    var ep = box.getAttribute("data-endpoint") || "/wow/quester/upcoming.json";
    var n = parseInt(box.getAttribute("data-limit") || "5", 10);
    if (!(n > 0 && n <= 50)) n = 5;
    var url = ep + (ep.indexOf("?") < 0 ? "?" : "&") + "n=" + n;
    if (!window.fetch) return;
    fetch(url, { credentials: "omit", headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        var items = (data && Array.isArray(data.items)) ? data.items : [];
        if (items.length) { render(box, items); return; }
        var empty = box.getAttribute("data-empty");
        if (empty && box.getAttribute("data-keep-fallback") === "true") {
          box.appendChild(el("p", "note", empty));
        }
        box.setAttribute("data-wq-state", "empty");
      })
      .catch(function () { box.setAttribute("data-wq-state", "fallback"); });
  }
  function init() {
    var boxes = document.querySelectorAll("[data-wq-upcoming]");
    for (var i = 0; i < boxes.length; i++) load(boxes[i]);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
