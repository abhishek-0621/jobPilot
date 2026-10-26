// Row selection for bulk actions: select-all checkboxes and the sticky action bar.
// Works with checkboxes swapped in by HTMX because it listens on the document.
(function () {
  "use strict";
  function boxes(name) {
    return Array.prototype.slice.call(
      document.querySelectorAll('input[type="checkbox"][name="' + name + '"]:not([disabled])'));
  }
  function refresh(name) {
    var all = boxes(name);
    var n = all.filter(function (b) { return b.checked; }).length;
    document.querySelectorAll('[data-action-bar="' + name + '"]').forEach(function (bar) {
      bar.hidden = n === 0;
      var c = bar.querySelector("[data-count]");
      if (c) { c.textContent = String(n); }
    });
    document.querySelectorAll('[data-select-all="' + name + '"]').forEach(function (sa) {
      sa.checked = all.length > 0 && n === all.length;
      sa.indeterminate = n > 0 && n < all.length;
    });
  }
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (!(t instanceof HTMLInputElement) || t.type !== "checkbox") { return; }
    var group = t.getAttribute("data-select-all");
    if (group) {
      boxes(group).forEach(function (b) { b.checked = t.checked; });
      refresh(group);
    } else if (t.name) {
      refresh(t.name);
    }
  });
  function refreshAll() {
    document.querySelectorAll("[data-action-bar]").forEach(function (bar) {
      refresh(bar.getAttribute("data-action-bar"));
    });
  }
  document.addEventListener("DOMContentLoaded", refreshAll);
  document.addEventListener("htmx:afterSwap", refreshAll);
}());
