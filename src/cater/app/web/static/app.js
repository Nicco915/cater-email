// Local, CSP-self script for the CATER admin UI. No inline handlers, no
// external requests. The only behaviour is a "select all" checkbox that
// toggles the row checkboxes belonging to the SAME form (whether they are DOM
// descendants or associated via the HTML5 `form="id"` attribute).
(function () {
  "use strict";
  var NAMES = [
    "task_ids",
    "country_alpha3",
    "machine_key",
    "country_alias_key",
    "machine_alias_key"
  ];
  document.addEventListener("DOMContentLoaded", function () {
    var selector = NAMES.map(function (n) {
      return 'input[type="checkbox"][name="' + n + '"]';
    }).join(",");
    var masters = document.querySelectorAll("[data-select-all]");
    masters.forEach(function (master) {
      master.addEventListener("click", function () {
        var form = master.closest("form");
        if (!form) return;
        document.querySelectorAll(selector).forEach(function (box) {
          // box.form is the real form owner (descendant OR form= association).
          if (box.form !== form) return;
          if (!box.disabled) box.checked = master.checked;
        });
      });
    });
  });
})();