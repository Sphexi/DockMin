/* Sidebar and top-bar menus; every control works without this file. */
(function () {
  "use strict";

  var STORE_KEY = "dockmin.nav.open";
  var root = document.documentElement;

  function readOpen() {
    try {
      return JSON.parse(window.localStorage.getItem(STORE_KEY) || "{}");
    } catch (error) {
      return {};
    }
  }

  function writeOpen(state) {
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify(state));
    } catch (error) {
      // Storage can be unavailable; the server-rendered open state still applies.
    }
  }

  function initGroups() {
    var state = readOpen();
    document.querySelectorAll("details.nav__group[data-nav-group]").forEach(function (group) {
      var key = group.getAttribute("data-nav-group");
      // The group holding the current page stays open whatever was remembered.
      var holdsCurrent = group.querySelector('[aria-current="page"]') !== null;
      if (!holdsCurrent && Object.prototype.hasOwnProperty.call(state, key)) {
        group.open = state[key] === true;
      }
      group.addEventListener("toggle", function () {
        var next = readOpen();
        next[key] = group.open;
        writeOpen(next);
      });
    });
  }

  function initDrawer() {
    var button = document.querySelector("[data-nav-toggle]");
    var nav = document.getElementById("sidenav");
    if (!button || !nav) {
      return;
    }
    button.hidden = false;

    function setOpen(open) {
      if (open) {
        root.setAttribute("data-nav-open", "");
      } else {
        root.removeAttribute("data-nav-open");
      }
      button.setAttribute("aria-expanded", open ? "true" : "false");
    }

    button.addEventListener("click", function () {
      setOpen(!root.hasAttribute("data-nav-open"));
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && root.hasAttribute("data-nav-open")) {
        setOpen(false);
        button.focus();
      }
    });

    document.addEventListener("click", function (event) {
      if (!root.hasAttribute("data-nav-open")) {
        return;
      }
      if (nav.contains(event.target) || button.contains(event.target)) {
        return;
      }
      setOpen(false);
    });
  }

  function initEnvironmentFilter() {
    var switcher = document.querySelector("details.env-switch");
    if (!switcher) {
      return;
    }
    var filter = switcher.querySelector("[data-env-filter]");
    var options = switcher.querySelectorAll(".env-switch__list > li");
    if (!filter) {
      return;
    }
    filter.hidden = false;

    switcher.addEventListener("toggle", function () {
      if (switcher.open) {
        filter.focus();
      }
    });

    filter.addEventListener("input", function () {
      var needle = filter.value.trim().toLowerCase();
      options.forEach(function (item) {
        item.hidden = needle !== "" && item.textContent.toLowerCase().indexOf(needle) === -1;
      });
    });
  }

  function initMenus() {
    var menus = document.querySelectorAll("details[data-menu]");
    if (!menus.length) {
      return;
    }

    document.addEventListener("click", function (event) {
      menus.forEach(function (menu) {
        if (menu.open && !menu.contains(event.target)) {
          menu.open = false;
        }
      });
    });

    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") {
        return;
      }
      menus.forEach(function (menu) {
        if (menu.open) {
          menu.open = false;
          menu.querySelector("summary").focus();
        }
      });
    });
  }

  function init() {
    initGroups();
    initDrawer();
    initEnvironmentFilter();
    initMenus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
