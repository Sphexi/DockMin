/* Progressive enhancement for the server-rendered UI; every page works without this file. */
(function () {
  "use strict";

  var THEME_COOKIE = "dockmin_theme";
  var ONE_YEAR_SECONDS = 31536000;
  var TOAST_LIFETIME_MS = 20000;
  var TOAST_ANNOUNCE_DELAY_MS = 100;
  var TOAST_CLEARANCE_PROPERTY = "--toast-clearance";

  /* The server writes data-theme from the cookie, so this handles later changes only. */

  function effectiveTheme() {
    var set = document.documentElement.getAttribute("data-theme");
    if (set === "light" || set === "dark") {
      return set;
    }
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function storeTheme(theme) {
    var cookie = THEME_COOKIE + "=" + theme + "; Path=/; Max-Age=" + ONE_YEAR_SECONDS + "; SameSite=Lax";
    if (window.location.protocol === "https:") {
      cookie += "; Secure";
    }
    document.cookie = cookie;
  }

  function syncThemeToggle(button) {
    button.setAttribute("aria-pressed", effectiveTheme() === "dark" ? "true" : "false");
  }

  function initThemeToggle() {
    var button = document.querySelector("[data-theme-toggle]");
    if (!button) {
      return;
    }
    button.hidden = false;
    syncThemeToggle(button);
    button.addEventListener("click", function () {
      var next = effectiveTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      storeTheme(next);
      syncThemeToggle(button);
    });
  }

  /* An inline dialog has no native Escape, so Escape and Cancel remove it and focus returns to the element that asked for it. */

  var modalOpeners = new WeakMap();
  var inlineReturns = new WeakMap();
  var inlineDialog = null;

  function isModal(dialog) {
    try {
      return dialog.matches(":modal");
    } catch (error) {
      return modalOpeners.has(dialog);
    }
  }

  function focusAfterDialog(element) {
    var target = element && element.isConnected ? element : document.getElementById("main");
    if (target instanceof HTMLElement) {
      target.focus();
    }
  }

  function returnRecord(element) {
    return element instanceof Element ? { element: element, id: element.id } : null;
  }

  function resolveReturn(record) {
    if (!record) {
      return null;
    }
    if (record.element.isConnected) {
      return record.element;
    }
    return record.id ? document.getElementById(record.id) : null;
  }

  function returnTargetOf(dialog) {
    var id = dialog.getAttribute("data-dialog-return");
    var named = id ? document.getElementById(id) : null;
    return named || resolveReturn(inlineReturns.get(dialog));
  }

  function closeInlineDialog(dialog) {
    var target = returnTargetOf(dialog);
    var parent = dialog.parentElement;
    dialog.remove();
    if (parent && parent.children.length === 0) {
      parent.textContent = "";
    }
    if (inlineDialog && inlineDialog.dialog === dialog) {
      inlineDialog = null;
    }
    focusAfterDialog(target);
  }

  function closeDialog(dialog) {
    if (isModal(dialog)) {
      dialog.close();
    } else {
      closeInlineDialog(dialog);
    }
  }

  function trackInlineDialog(event) {
    var found = null;
    document.querySelectorAll("dialog[data-confirm-dialog][open]").forEach(function (dialog) {
      if (!isModal(dialog)) {
        found = dialog;
      }
    });
    if (found && (!inlineDialog || inlineDialog.dialog !== found)) {
      var issuer = event.detail && event.detail.requestConfig ? event.detail.requestConfig.elt : null;
      var previous = issuer instanceof Element ? issuer.closest("dialog[data-confirm-dialog]") : null;
      var carried = previous ? inlineReturns.get(previous) : null;
      inlineReturns.set(found, carried || (previous ? null : returnRecord(issuer)));
      inlineDialog = { dialog: found };
    } else if (!found && inlineDialog) {
      var gone = inlineDialog.dialog;
      inlineDialog = null;
      focusAfterDialog(returnTargetOf(gone));
    }
  }

  function initDialogs() {
    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!(target instanceof Element)) {
        return;
      }
      var opener = target.closest("[data-dialog-open]");
      if (opener) {
        var dialog = document.getElementById(opener.getAttribute("data-dialog-open"));
        if (dialog && typeof dialog.showModal === "function") {
          event.preventDefault();
          if (!dialog.open) {
            modalOpeners.set(dialog, opener);
            dialog.showModal();
          }
        }
        return;
      }
      var closer = target.closest("[data-dialog-close]");
      if (closer) {
        var owner = closer.closest("dialog");
        if (owner) {
          event.preventDefault();
          closeDialog(owner);
        }
        return;
      }
      if (target instanceof HTMLDialogElement && target.hasAttribute("data-confirm-dialog") && isModal(target)) {
        target.close();
      }
    });
    document.addEventListener("close", function (event) {
      var dialog = event.target;
      if (dialog instanceof HTMLDialogElement && modalOpeners.has(dialog)) {
        var opener = modalOpeners.get(dialog);
        modalOpeners.delete(dialog);
        focusAfterDialog(opener);
      }
    }, true);
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" || !(event.target instanceof Element)) {
        return;
      }
      var dialog = event.target.closest("dialog[data-confirm-dialog][open]");
      if (dialog && !isModal(dialog)) {
        event.preventDefault();
        closeInlineDialog(dialog);
      }
    });
    document.addEventListener("htmx:afterSettle", trackInlineDialog);
    document.addEventListener("htmx:afterRequest", function (event) {
      var form = event.target;
      if (form instanceof Element && event.detail.successful) {
        var dialog = form.closest("dialog[data-confirm-dialog]");
        if (dialog && dialog.isConnected && dialog.open) {
          closeDialog(dialog);
        }
      }
    });
  }

  /* A page restored from the back-forward cache gets its submit buttons re-enabled. */

  function initSubmitOnce() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-submit-once")) {
        return;
      }
      window.setTimeout(function () {
        form.querySelectorAll("[type=submit]").forEach(function (button) {
          button.disabled = true;
        });
      }, 0);
    });
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        document.querySelectorAll("[data-submit-once] [type=submit]").forEach(function (button) {
          button.disabled = false;
        });
      }
    });
  }

  var INDEX_TOKEN = "__INDEX__";
  var NUMBER_TOKEN = "__NUMBER__";

  function fillRowTokens(root, index, number) {
    var replace = function (text) {
      return text.split(INDEX_TOKEN).join(index).split(NUMBER_TOKEN).join(number);
    };
    var elements = [root].concat(Array.prototype.slice.call(root.querySelectorAll("*")));
    elements.forEach(function (element) {
      Array.prototype.slice.call(element.attributes).forEach(function (attribute) {
        if (attribute.value.indexOf("__") !== -1) {
          element.setAttribute(attribute.name, replace(attribute.value));
        }
      });
    });
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      walker.currentNode.nodeValue = replace(walker.currentNode.nodeValue);
    }
  }

  function addRow(group) {
    var rows = group.querySelector("[data-repeat-rows]");
    var template = group.querySelector("template[data-repeat-template]");
    var cap = parseInt(group.getAttribute("data-repeat-cap"), 10);
    var existing = rows.querySelectorAll("[data-repeat-row]");
    var next = 0;
    existing.forEach(function (row) {
      next = Math.max(next, parseInt(row.getAttribute("data-repeat-row"), 10) + 1);
    });
    if (next >= cap) {
      return false;
    }
    var row = template.content.firstElementChild.cloneNode(true);
    fillRowTokens(row, next, existing.length + 1);
    rows.appendChild(row);
    initCodeEditors(row);
    if (window.htmx) {
      window.htmx.process(row);
    }
    var field = row.querySelector("input, select");
    if (field) {
      field.focus();
    }
    return next + 1 < cap;
  }

  function initRepeatRows() {
    document.querySelectorAll("[data-repeat]").forEach(function (group) {
      var add = group.querySelector("[data-repeat-add]");
      var more = group.querySelector("[data-repeat-more]");
      if (!add || !group.querySelector("template[data-repeat-template]")) {
        return;
      }
      add.hidden = false;
      if (more) {
        more.hidden = true;
      }
      add.addEventListener("click", function () {
        if (!addRow(group)) {
          add.hidden = true;
        }
      });
    });
  }

  /* Lines do not wrap, so one gutter number is one line. */

  function lineCount(text) {
    var count = 1;
    var at = text.indexOf("\n");
    while (at !== -1) {
      count += 1;
      at = text.indexOf("\n", at + 1);
    }
    return count;
  }

  function gutterText(count) {
    var numbers = new Array(count);
    for (var i = 0; i < count; i += 1) {
      numbers[i] = String(i + 1);
    }
    return numbers.join("\n");
  }

  function initCodeEditor(area) {
    if (area.hasAttribute("data-code-editor-ready") || !area.parentNode) {
      return;
    }
    area.setAttribute("data-code-editor-ready", "");
    area.setAttribute("wrap", "off");
    var wrapper = document.createElement("div");
    wrapper.className = "code-editor";
    var gutter = document.createElement("div");
    gutter.className = "code-editor__gutter";
    gutter.setAttribute("aria-hidden", "true");
    var numbers = document.createTextNode("1");
    gutter.appendChild(numbers);
    var status = document.createElement("p");
    status.className = "code-editor__status";
    status.setAttribute("data-code-editor-status", "");
    area.parentNode.insertBefore(wrapper, area);
    wrapper.appendChild(gutter);
    wrapper.appendChild(area);
    wrapper.insertAdjacentElement("afterend", status);

    var shown = 0;
    var scheduled = false;

    function draw() {
      scheduled = false;
      var count = lineCount(area.value);
      if (count !== shown) {
        shown = count;
        numbers.nodeValue = gutterText(count);
      }
      gutter.scrollTop = area.scrollTop;
    }

    function schedule() {
      if (!scheduled) {
        scheduled = true;
        window.requestAnimationFrame(draw);
      }
    }

    function locate() {
      var caret = area.selectionStart;
      var before = area.value.slice(0, caret);
      var column = caret - (before.lastIndexOf("\n") + 1) + 1;
      status.textContent = "Line " + lineCount(before) + ", column " + column;
    }

    area.addEventListener("input", function () {
      schedule();
      locate();
    });
    area.addEventListener("scroll", function () {
      gutter.scrollTop = area.scrollTop;
    });
    ["keyup", "mouseup", "focus", "select"].forEach(function (name) {
      area.addEventListener(name, locate);
    });
    if (area.form) {
      area.form.addEventListener("reset", function () {
        window.setTimeout(schedule, 0);
      });
    }
    window.addEventListener("pageshow", schedule);
    draw();
    locate();
  }

  function initCodeEditors(root) {
    root.querySelectorAll("textarea[data-code-editor]").forEach(initCodeEditor);
  }

  function goToLine(link) {
    var id = (link.getAttribute("href") || "").slice(1);
    var area = id ? document.getElementById(id) : null;
    var line = parseInt(link.getAttribute("data-goto-line"), 10);
    if (!(area instanceof HTMLTextAreaElement) || !(line >= 1)) {
      return false;
    }
    var text = area.value;
    var offset = 0;
    for (var i = 1; i < line; i += 1) {
      var next = text.indexOf("\n", offset);
      if (next === -1) {
        offset = text.length;
        break;
      }
      offset = next + 1;
    }
    area.focus();
    area.setSelectionRange(offset, offset);
    var height = parseFloat(window.getComputedStyle(area).lineHeight);
    if (height > 0) {
      area.scrollTop = Math.max(0, (line - 1) * height - area.clientHeight / 3);
    }
    area.dispatchEvent(new Event("select"));
    return true;
  }

  function initGotoLine() {
    document.addEventListener("click", function (event) {
      var link = event.target instanceof Element ? event.target.closest("a[data-goto-line]") : null;
      if (link && goToLine(link)) {
        event.preventDefault();
      }
    });
  }

  /* Fields the chosen kind does not use are hidden and disabled, so they are neither shown nor posted. */

  function initTargetKind() {
    document.querySelectorAll("[data-target-form]").forEach(function (form) {
      var select = form.querySelector("select[name=target_kind]");
      if (!select) {
        return;
      }
      var apply = function () {
        form.querySelectorAll("[data-target-section]").forEach(function (section) {
          var uses = section.getAttribute("data-target-section").split(" ").indexOf(select.value) !== -1;
          section.hidden = !uses;
          section.querySelectorAll("input, textarea, button").forEach(function (control) {
            control.disabled = !uses;
          });
        });
      };
      select.addEventListener("change", apply);
      apply();
    });
  }

  /* A server-rendered toast is re-inserted after load so its live role is announced. */

  var toastTimers = new WeakMap();

  function dismissToast(toast) {
    window.clearTimeout(toastTimers.get(toast));
    toastTimers.delete(toast);
    if (toast.parentNode) {
      toast.parentNode.removeChild(toast);
    }
  }

  function startToastTimer(toast) {
    window.clearTimeout(toastTimers.get(toast));
    toastTimers.set(toast, window.setTimeout(function () {
      dismissToast(toast);
    }, TOAST_LIFETIME_MS));
  }

  function pauseToastTimer(toast) {
    window.clearTimeout(toastTimers.get(toast));
  }

  function armToast(toast) {
    if (toast.hasAttribute("data-armed")) {
      return;
    }
    toast.setAttribute("data-armed", "");
    var level = toast.getAttribute("data-level");
    if (level !== "info" && level !== "ok") {
      return;
    }
    startToastTimer(toast);
    toast.addEventListener("mouseenter", function () {
      pauseToastTimer(toast);
    });
    toast.addEventListener("mouseleave", function () {
      if (!toast.contains(document.activeElement)) {
        startToastTimer(toast);
      }
    });
    toast.addEventListener("focusin", function () {
      pauseToastTimer(toast);
    });
    toast.addEventListener("focusout", function () {
      startToastTimer(toast);
    });
  }

  function armToasts(root) {
    var toasts = root.querySelectorAll("[data-toast]");
    for (var i = 0; i < toasts.length; i += 1) {
      armToast(toasts[i]);
    }
  }

  function showToast(level, message) {
    var template = document.getElementById("toast-template");
    var region = document.getElementById("toasts");
    if (!template || !region) {
      return;
    }
    var toast = template.content.firstElementChild.cloneNode(true);
    toast.setAttribute("data-level", level);
    toast.setAttribute("role", level === "warn" || level === "danger" ? "alert" : "status");
    toast.querySelector("[data-toast-message]").textContent = message;
    region.appendChild(toast);
    armToast(toast);
  }

  function announceServerToasts(region) {
    var present = Array.prototype.slice.call(region.querySelectorAll("[data-toast]"));
    present.forEach(function (toast) {
      region.removeChild(toast);
    });
    window.setTimeout(function () {
      present.forEach(function (toast) {
        region.appendChild(toast);
      });
    }, TOAST_ANNOUNCE_DELAY_MS);
  }

  function trackToastClearance(region) {
    if (typeof ResizeObserver !== "function") {
      return;
    }
    var root = document.documentElement;
    var update = function () {
      var box = region.getBoundingClientRect();
      var clearance = box.height > 0 ? Math.max(0, window.innerHeight - box.top) : 0;
      root.style.setProperty(TOAST_CLEARANCE_PROPERTY, clearance + "px");
    };
    new ResizeObserver(update).observe(region);
    window.addEventListener("resize", update);
    update();
  }

  function initToasts() {
    var region = document.getElementById("toasts");
    document.querySelectorAll("[data-toast]").forEach(function (toast) {
      toast.setAttribute("data-armed", "");
    });
    document.addEventListener("click", function (event) {
      var target = event.target;
      if (target instanceof Element) {
        var close = target.closest("[data-toast-close]");
        if (close) {
          dismissToast(close.closest("[data-toast]"));
        }
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" || event.defaultPrevented) {
        return;
      }
      if (event.target instanceof Element && event.target.closest("dialog")) {
        return;
      }
      document.querySelectorAll("#toasts [data-toast]").forEach(dismissToast);
    });
    if (region) {
      announceServerToasts(region);
      trackToastClearance(region);
    }
  }

  /* A poll is left alone so the loading line does not flicker, as is a request that targets the whole page. */

  var busyTargets = new WeakMap();

  function isPoll(element) {
    var trigger = element && element.getAttribute ? element.getAttribute("hx-trigger") : null;
    return !!trigger && /(^|[\s,])every\s/.test(trigger);
  }

  function markBusy(event) {
    var target = event.detail.target;
    if (!(target instanceof Element) || target === document.body || isPoll(event.detail.elt)) {
      return;
    }
    var entry = busyTargets.get(target);
    if (entry) {
      entry.count += 1;
      return;
    }
    busyTargets.set(target, { count: 1, previous: target.getAttribute("aria-busy") });
    target.setAttribute("aria-busy", "true");
  }

  function clearBusy(event) {
    var target = event.detail.target;
    var entry = target instanceof Element ? busyTargets.get(target) : null;
    if (!entry) {
      return;
    }
    entry.count -= 1;
    if (entry.count > 0) {
      return;
    }
    busyTargets.delete(target);
    if (entry.previous === null) {
      target.removeAttribute("aria-busy");
    } else {
      target.setAttribute("aria-busy", entry.previous);
    }
  }

  function initBusy() {
    document.addEventListener("htmx:beforeRequest", markBusy);
    document.addEventListener("htmx:afterRequest", clearBusy);
  }

  function isHtmlResponse(xhr) {
    var type = xhr.getResponseHeader("Content-Type") || "";
    return type.indexOf("text/html") === 0;
  }

  /* The restart and update pages poll a server that is expected to be down; their failures are
     not errors. */
  function isQuietRequest(event) {
    return (
      event.target instanceof Element &&
      event.target.closest("[data-restart-poll], [data-update-poll]") !== null
    );
  }

  var focusHeldBy = null;

  function restoreFocus() {
    var id = focusHeldBy;
    focusHeldBy = null;
    var replacement = id === null ? null : document.getElementById(id);
    if (!replacement || replacement.contains(document.activeElement)) {
      return;
    }
    var control = replacement.querySelector("[data-autofocus], [data-focus-return]");
    if (control instanceof HTMLElement) {
      control.focus();
      return;
    }
    replacement.setAttribute("tabindex", "-1");
    replacement.focus();
  }

  /* htmx reports a request it aborts itself (hx-sync replace) to the console as an error. */
  function initHtmx() {
    /* An error status that carries an HTML fragment renders in place; htmx would otherwise discard it. */
    document.addEventListener("htmx:beforeSwap", function (event) {
      var detail = event.detail;
      if (detail.xhr.status >= 400 && isHtmlResponse(detail.xhr) && detail.xhr.responseText) {
        detail.shouldSwap = true;
        detail.isError = false;
      }
    });

    /* No fragment came back, so the page would otherwise fail silently. */
    document.addEventListener("htmx:responseError", function (event) {
      var xhr = event.detail.xhr;
      if (isQuietRequest(event)) {
        return;
      }
      if (!isHtmlResponse(xhr) || !xhr.responseText) {
        showToast("danger", "The request failed (" + xhr.status + "). Try again.");
      }
    });
    document.addEventListener("htmx:sendError", function (event) {
      if (!isQuietRequest(event)) {
        showToast("danger", "The server could not be reached. Check the connection and try again.");
      }
    });
    document.addEventListener("htmx:timeout", function (event) {
      if (!isQuietRequest(event)) {
        showToast("warn", "The request timed out. Try again.");
      }
    });

    /* The swap target can be the element that was replaced, so the whole document is searched. */
    document.addEventListener("htmx:afterSettle", function (event) {
      armToasts(document);
      initCodeEditors(document);
      var root = event.target;
      if (root instanceof Element) {
        var focusTarget = root.matches("[data-autofocus]") ? root : root.querySelector("[data-autofocus]");
        if (focusTarget instanceof HTMLElement) {
          focusTarget.focus();
        }
      }
      restoreFocus();
    });

    /* When a swap replaces the focused element and nothing in the replacement takes focus, focus would drop to the page. */
    document.addEventListener("htmx:beforeSwap", function (event) {
      var target = event.detail.target;
      var active = document.activeElement;
      focusHeldBy =
        event.detail.shouldSwap &&
        target instanceof Element &&
        target.id &&
        active !== document.body &&
        target.contains(active)
          ? target.id
          : null;
    });
  }

  /* Cumulative network and block counters are turned into bytes per second. */

  var CHART_SAMPLES = 90;
  var CHART_GAP_MS = 10000;
  var CHART_WIDTH = 300;
  var CHART_HEIGHT = 100;
  var CHART_PADDING = 2;
  var CPU_FLOOR_PERCENT = 10;
  var BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  var SVG_NS = "http://www.w3.org/2000/svg";
  var chartStates = new WeakMap();

  function formatPercent(value) {
    return value.toFixed(1) + "%";
  }

  function formatBytes(value) {
    var unit = 0;
    var size = value;
    while (size >= 1024 && unit < BYTE_UNITS.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return (unit === 0 ? String(Math.round(size)) : size.toFixed(1)) + " " + BYTE_UNITS[unit];
  }

  function formatRate(value) {
    return formatBytes(value) + "/s";
  }

  function decimalCeiling(value) {
    if (!(value > 0)) {
      return 1;
    }
    var magnitude = Math.pow(10, Math.floor(Math.log10(value)));
    var lead = value / magnitude;
    return (lead <= 1 ? 1 : lead <= 2 ? 2 : lead <= 5 ? 5 : 10) * magnitude;
  }

  function byteCeiling(value) {
    var unit = 0;
    var size = value;
    while (size >= 1024 && unit < BYTE_UNITS.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return decimalCeiling(size) * Math.pow(1024, unit);
  }

  function limitOf(samples) {
    return samples.length ? samples[samples.length - 1].memLimit : 0;
  }

  var CHARTS = [
    {
      key: "cpu",
      name: "CPU",
      format: formatPercent,
      scale: function (value) { return Math.round(value) + "%"; },
      ceiling: function (max) {
        return Math.max(decimalCeiling(max), CPU_FLOOR_PERCENT);
      },
      series: [
        { id: "cpu", label: "CPU", slot: "stats-slot--1", area: true, value: function (s) { return s.cpu; } }
      ]
    },
    {
      key: "mem",
      name: "Memory",
      format: formatBytes,
      ceiling: function (max, samples) {
        return limitOf(samples) > 0 ? max : byteCeiling(max);
      },
      series: [
        { id: "mem", label: "Used", slot: "stats-slot--2", area: true, value: function (s) { return s.memUsed; } },
        {
          id: "limit", label: "Limit", slot: "stats-slot--0", dashed: true, empty: "none",
          value: function (s) { return s.memLimit > 0 ? s.memLimit : null; }
        }
      ]
    },
    {
      key: "net",
      name: "Network",
      format: formatRate,
      ceiling: function (max) { return byteCeiling(max); },
      series: [
        { id: "rx", label: "Received", slot: "stats-slot--3", value: function (s) { return s.rates ? s.rates.rx : null; } },
        { id: "tx", label: "Sent", slot: "stats-slot--4", dashed: true, value: function (s) { return s.rates ? s.rates.tx : null; } }
      ]
    },
    {
      key: "blk",
      name: "Block I/O",
      format: formatRate,
      ceiling: function (max) { return byteCeiling(max); },
      series: [
        { id: "read", label: "Read", slot: "stats-slot--3", value: function (s) { return s.rates ? s.rates.read : null; } },
        { id: "write", label: "Written", slot: "stats-slot--4", dashed: true, value: function (s) { return s.rates ? s.rates.write : null; } }
      ]
    }
  ];

  function svgElement(name, attributes) {
    var element = document.createElementNS(SVG_NS, name);
    Object.keys(attributes).forEach(function (key) {
      element.setAttribute(key, attributes[key]);
    });
    return element;
  }

  function buildChart(root, definition) {
    var figure = root.querySelector('[data-chart="' + definition.key + '"]');
    var plot = figure ? figure.querySelector("[data-chart-plot]") : null;
    if (!figure || !plot) {
      return null;
    }
    var svg = svgElement("svg", {
      "class": "stats-chart__svg",
      viewBox: "0 0 " + CHART_WIDTH + " " + CHART_HEIGHT,
      preserveAspectRatio: "none",
      "aria-hidden": "true",
      focusable: "false"
    });
    var middle = CHART_HEIGHT / 2;
    var bottom = CHART_HEIGHT - CHART_PADDING;
    svg.appendChild(svgElement("path", {
      "class": "stats-grid",
      d: "M0 " + CHART_PADDING + "H" + CHART_WIDTH + "M0 " + middle + "H" + CHART_WIDTH + "M0 " + bottom + "H" + CHART_WIDTH
    }));
    var paths = {};
    definition.series.forEach(function (series) {
      var slot = series.slot;
      var entry = { area: null, line: null, value: figure.querySelector('[data-chart-value="' + series.id + '"]') };
      if (series.area) {
        entry.area = svgElement("path", { "class": "stats-area " + slot });
        svg.appendChild(entry.area);
      }
      entry.line = svgElement("path", { "class": "stats-line " + slot + (series.dashed ? " stats-line--dashed" : "") });
      svg.appendChild(entry.line);
      paths[series.id] = entry;
    });
    plot.appendChild(svg);
    return { definition: definition, plot: plot, scale: figure.querySelector("[data-chart-scale]"), paths: paths };
  }

  function chartState(panel) {
    var existing = chartStates.get(panel);
    if (existing) {
      return existing;
    }
    var root = panel.querySelector("[data-stats-charts]");
    if (!root) {
      return null;
    }
    var views = CHARTS.map(function (definition) {
      return buildChart(root, definition);
    }).filter(Boolean);
    var state = { samples: [], views: views };
    root.hidden = false;
    chartStates.set(panel, state);
    return state;
  }

  function numberAttribute(element, name) {
    var text = element.getAttribute(name);
    if (text === null || text === "") {
      return null;
    }
    var value = Number(text);
    return isFinite(value) ? value : null;
  }

  function readSample(body) {
    var element = body ? body.querySelector("[data-stats-sample]") : null;
    if (!element) {
      return null;
    }
    var stamp = element.querySelector("time[datetime]");
    var readAt = stamp ? Date.parse(stamp.getAttribute("datetime")) : NaN;
    var micro = numberAttribute(element, "data-read-microsecond");
    if (isFinite(readAt) && micro !== null) {
      readAt += micro / 1000;
    }
    var counters = {
      rx: numberAttribute(element, "data-net-rx"),
      tx: numberAttribute(element, "data-net-tx"),
      read: numberAttribute(element, "data-blk-read"),
      write: numberAttribute(element, "data-blk-write")
    };
    return {
      t: isFinite(readAt) ? readAt : Date.now(),
      cpu: numberAttribute(element, "data-cpu"),
      memUsed: numberAttribute(element, "data-mem-used"),
      memLimit: numberAttribute(element, "data-mem-limit") || 0,
      counters: counters,
      rates: null,
      gap: false
    };
  }

  /* A counter that goes backwards was reset, so that interval has no rate. */
  function ratesBetween(previous, current, seconds) {
    var rates = {};
    Object.keys(current).forEach(function (name) {
      var change = current[name] === null || previous[name] === null ? -1 : current[name] - previous[name];
      rates[name] = change >= 0 ? change / seconds : null;
    });
    return rates;
  }

  function recordSample(state, body) {
    var sample = readSample(body);
    if (!sample) {
      return;
    }
    var last = state.samples[state.samples.length - 1];
    if (last) {
      var elapsed = sample.t - last.t;
      if (elapsed <= 0) {
        return;
      }
      if (elapsed > CHART_GAP_MS) {
        sample.gap = true;
      } else {
        sample.rates = ratesBetween(last.counters, sample.counters, elapsed / 1000);
      }
    }
    state.samples.push(sample);
    if (state.samples.length > CHART_SAMPLES) {
      state.samples.shift();
    }
    state.views.forEach(function (view) {
      drawChart(view, state.samples);
    });
  }

  function roundCoordinate(value) {
    return Math.round(value * 10) / 10;
  }

  /* Contiguous runs of points; a missing value or a gap in time ends a run. */
  function segmentsOf(series, samples, ceiling) {
    var segments = [];
    var run = null;
    var offset = CHART_SAMPLES - samples.length;
    var span = CHART_HEIGHT - 2 * CHART_PADDING;
    samples.forEach(function (sample, index) {
      var value = series.value(sample);
      if (sample.gap || value === null) {
        run = null;
      }
      if (value === null) {
        return;
      }
      if (run === null) {
        run = [];
        segments.push(run);
      }
      run.push({
        x: roundCoordinate((offset + index) / (CHART_SAMPLES - 1) * CHART_WIDTH),
        y: roundCoordinate(CHART_HEIGHT - CHART_PADDING - Math.min(value / ceiling, 1) * span)
      });
    });
    return segments;
  }

  function lineOf(segments) {
    return segments.map(function (points) {
      var path = points.map(function (point, index) {
        return (index === 0 ? "M" : "L") + point.x + " " + point.y;
      }).join("");
      return points.length === 1 ? path + "L" + points[0].x + " " + points[0].y : path;
    }).join("");
  }

  function areaOf(segments) {
    var floor = CHART_HEIGHT - CHART_PADDING;
    return segments.filter(function (points) {
      return points.length > 1;
    }).map(function (points) {
      var first = points[0];
      var last = points[points.length - 1];
      return lineOf([points]) + "L" + last.x + " " + floor + "L" + first.x + " " + floor + "Z";
    }).join("");
  }

  function drawChart(view, samples) {
    var definition = view.definition;
    var max = 0;
    definition.series.forEach(function (series) {
      samples.forEach(function (sample) {
        var value = series.value(sample);
        if (value !== null && value > max) {
          max = value;
        }
      });
    });
    var ceiling = definition.ceiling(max, samples);
    var latest = samples[samples.length - 1];
    var parts = [];
    definition.series.forEach(function (series) {
      var segments = segmentsOf(series, samples, ceiling);
      var entry = view.paths[series.id];
      entry.line.setAttribute("d", lineOf(segments));
      if (entry.area) {
        entry.area.setAttribute("d", areaOf(segments));
      }
      var value = latest ? series.value(latest) : null;
      var text = value === null ? (series.empty || "—") : definition.format(value);
      if (entry.value) {
        entry.value.textContent = text;
      }
      parts.push(series.label + " " + text);
    });
    if (view.scale) {
      view.scale.textContent = "max " + (definition.scale || definition.format)(ceiling);
    }
    view.plot.setAttribute("aria-label", definition.name + ": " + parts.join(", "));
  }

  var statsSource = null;
  var statsPanel = null;
  var statsFailed = null;

  function closeStats() {
    if (statsSource) {
      statsSource.close();
    }
    statsSource = null;
    statsPanel = null;
  }

  function openStats(panel) {
    var body = panel.querySelector("[data-stats-body]");
    var status = panel.querySelector("[data-stats-status]");
    var refresh = panel.querySelector("[data-stats-refresh]");
    var idle = status ? status.textContent : "";
    var charts = chartState(panel);
    var source = new EventSource(panel.getAttribute("data-stats-stream"));
    if (charts) {
      recordSample(charts, body);
    }
    if (refresh) {
      refresh.hidden = true;
    }
    source.addEventListener("stats", function (event) {
      if (body) {
        body.innerHTML = event.data;
      }
      if (charts) {
        recordSample(charts, body);
      }
      if (status) {
        status.textContent = idle;
      }
    });
    source.addEventListener("error", function () {
      if (source.readyState === EventSource.CLOSED) {
        statsFailed = panel;
        closeStats();
        if (status) {
          status.textContent = "Stats are no longer available.";
        }
        if (refresh) {
          refresh.hidden = false;
        }
      } else if (status) {
        status.textContent = "Reconnecting.";
      }
    });
    statsSource = source;
    statsPanel = panel;
  }

  function syncStats() {
    var panel = document.querySelector("[data-stats-stream]");
    var wanted = null;
    if (panel && panel !== statsFailed && !document.hidden && typeof EventSource === "function") {
      wanted = panel;
    }
    if (statsSource && statsPanel !== wanted) {
      closeStats();
    }
    if (wanted && !statsSource) {
      openStats(wanted);
    }
  }

  function initStats() {
    document.addEventListener("htmx:beforeCleanupElement", function (event) {
      if (statsPanel && event.target instanceof Element && event.target.contains(statsPanel)) {
        closeStats();
      }
    });
    document.addEventListener("htmx:afterSettle", syncStats);
    document.addEventListener("htmx:historyRestore", syncStats);
    document.addEventListener("visibilitychange", syncStats);
    window.addEventListener("pageshow", syncStats);
    window.addEventListener("pagehide", closeStats);
    syncStats();
  }

  /* A finished job announces counts-changed so the sidebar counts reload. */

  var jobSource = null;
  var jobPanel = null;
  var jobFailed = null;

  function closeJob() {
    if (jobSource) {
      jobSource.close();
    }
    jobSource = null;
    jobPanel = null;
  }

  function finishJob(panel) {
    panel.removeAttribute("data-job-stream");
    panel.querySelectorAll("[data-job-cancel], [data-job-refresh], [data-job-status]").forEach(function (element) {
      element.hidden = true;
    });
    closeJob();
    document.body.dispatchEvent(new CustomEvent("counts-changed"));
  }

  function openJob(panel) {
    var body = panel.querySelector("[data-job-body]");
    var status = panel.querySelector("[data-job-status]");
    var refresh = panel.querySelector("[data-job-refresh]");
    var source = new EventSource(panel.getAttribute("data-job-stream"));
    if (refresh) {
      refresh.hidden = true;
    }
    if (status) {
      status.textContent = "Progress updates live while this page is open.";
    }
    source.addEventListener("progress", function (event) {
      if (body) {
        body.innerHTML = event.data;
      }
    });
    source.addEventListener("done", function (event) {
      if (body) {
        body.innerHTML = event.data;
      }
      finishJob(panel);
    });
    source.addEventListener("error", function () {
      if (source.readyState === EventSource.CLOSED) {
        jobFailed = panel;
        closeJob();
        if (status) {
          status.textContent = "Live updates stopped. Use Refresh to see the current state.";
        }
        if (refresh) {
          refresh.hidden = false;
        }
      } else if (status) {
        status.textContent = "Reconnecting.";
      }
    });
    jobSource = source;
    jobPanel = panel;
  }

  function syncJob() {
    var panel = document.querySelector("[data-job-stream]");
    var wanted = null;
    if (panel && panel !== jobFailed && !document.hidden && typeof EventSource === "function") {
      wanted = panel;
    }
    if (jobSource && jobPanel !== wanted) {
      closeJob();
    }
    if (wanted && !jobSource) {
      openJob(wanted);
    }
  }

  function initJobs() {
    document.addEventListener("htmx:beforeCleanupElement", function (event) {
      if (jobPanel && event.target instanceof Element && event.target.contains(jobPanel)) {
        closeJob();
      }
    });
    document.addEventListener("htmx:afterSettle", syncJob);
    document.addEventListener("htmx:historyRestore", syncJob);
    document.addEventListener("visibilitychange", syncJob);
    window.addEventListener("pageshow", syncJob);
    window.addEventListener("pagehide", closeJob);
    syncJob();
  }

  var LOG_STAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z$/;
  var LOG_FILTER_DELAY_MS = 120;
  var LOG_BOTTOM_SLACK_PX = 8;
  var LOG_FAILURE_TEXT = "Logs are no longer available.";
  var logs = null;

  function logShortStamp(stamp) {
    var match = LOG_STAMP.exec(stamp);
    if (!match) {
      return stamp;
    }
    return match[1] + "-" + match[2] + "-" + match[3] + " " + match[4] + "." + ((match[5] || "") + "000").slice(0, 3);
  }

  function logChecked(form, name) {
    var box = form.querySelector('input[type="checkbox"][name="' + name + '"]');
    return !!box && box.checked;
  }

  function logValue(form, name) {
    var input = form.elements.namedItem(name);
    return input && typeof input.value === "string" ? input.value.trim() : "";
  }

  function logServices(form) {
    var boxes = form.querySelectorAll('input[type="checkbox"][name="services"]');
    var chosen = [];
    boxes.forEach(function (box) {
      if (box.checked) {
        chosen.push(box.value);
      }
    });
    return chosen.length === boxes.length ? [] : chosen;
  }

  function showLogNotice(state, source, text) {
    if (state.source !== source || !text) {
      return;
    }
    if (state.notices.indexOf(text) === -1) {
      state.notices.push(text);
    }
    if (state.notice) {
      state.notice.textContent = state.notices.join(" ");
      state.notice.hidden = false;
    }
    if (state.announce) {
      state.announce.textContent = text;
    }
  }

  function setLogStatus(state, tone, text) {
    state.status.setAttribute("data-state", tone);
    state.status.textContent = text;
    state.status.hidden = false;
  }

  function logCountText(state) {
    var text = state.matching === state.retained
      ? state.retained.toLocaleString() + " lines"
      : state.matching.toLocaleString() + " of " + state.retained.toLocaleString() + " lines contain the filter";
    if (state.trimmed > 0) {
      text += ". The oldest " + state.trimmed.toLocaleString() + " were discarded to stay within the line budget";
    }
    return text + ".";
  }

  function showLogCount(state) {
    state.count.textContent = logCountText(state);
    state.count.hidden = false;
  }

  function announceLog(state) {
    if (state.announce && state.retained > 0) {
      state.announce.textContent = logCountText(state);
    }
  }

  function logAtBottom(output) {
    return output.scrollHeight - output.scrollTop - output.clientHeight <= LOG_BOTTOM_SLACK_PX;
  }

  function logMatches(filter, text) {
    return !filter || text.toLowerCase().indexOf(filter) !== -1;
  }

  function buildLogLine(stamp, text, isError, filter, labelled) {
    var line = document.createElement("div");
    line.className = isError ? "log-line log-line--err" : "log-line";
    if (stamp !== "-") {
      var time = document.createElement("span");
      time.className = "log-line__time";
      time.textContent = logShortStamp(stamp) + " ";
      line.appendChild(time);
    }
    if (isError) {
      var label = document.createElement("span");
      label.className = "visually-hidden";
      label.textContent = "stderr ";
      line.appendChild(label);
    }
    if (labelled) {
      var gap = text.indexOf(" ");
      var task = document.createElement("span");
      task.className = "log-line__task";
      task.textContent = gap === -1 ? text : text.slice(0, gap);
      text = gap === -1 ? "" : text.slice(gap + 1);
      line.appendChild(task);
      line.appendChild(document.createTextNode(" "));
    }
    var body = document.createElement("span");
    body.className = "log-line__text";
    body.textContent = text;
    line.appendChild(body);
    line.hidden = !logMatches(filter, text);
    return line;
  }

  function closeLogSource(state) {
    if (state.source) {
      state.source.close();
      state.source = null;
    }
  }

  function trimLog(state) {
    var output = state.output;
    while (state.retained > state.budget && output.firstChild) {
      if (!output.firstChild.hidden) {
        state.matching -= 1;
      }
      output.removeChild(output.firstChild);
      state.retained -= 1;
      state.trimmed += 1;
    }
  }

  function addLogEvent(state, source, event, isError) {
    if (state.source !== source) {
      return;
    }
    if (event.lastEventId) {
      state.lastEventId = event.lastEventId;
    }
    var fragment = document.createDocumentFragment();
    var added = 0;
    var entries = event.data.split("\n");
    for (var i = 0; i < entries.length; i += 1) {
      var space = entries[i].indexOf(" ");
      var stamp = space === -1 ? entries[i] : entries[i].slice(0, space);
      var text = space === -1 ? "" : entries[i].slice(space + 1);
      var line = buildLogLine(stamp, text, isError, state.filter, state.labelled);
      if (!line.hidden) {
        state.matching += 1;
      }
      fragment.appendChild(line);
      added += 1;
    }
    if (added === 0) {
      return;
    }
    state.output.appendChild(fragment);
    state.retained += added;
    trimLog(state);
    if (state.stick) {
      state.output.scrollTop = state.output.scrollHeight;
    }
    showLogCount(state);
  }

  function logUrl(state, resume) {
    var form = state.form;
    var params = new URLSearchParams();
    if (resume && state.lastEventId) {
      params.set("after", state.lastEventId);
    } else {
      params.set("tail", logValue(form, "tail"));
      if (logValue(form, "since")) {
        params.set("since", logValue(form, "since"));
      }
    }
    if (logValue(form, "until")) {
      params.set("until", logValue(form, "until"));
    }
    params.set("stdout", logChecked(form, "stdout") ? "1" : "0");
    params.set("stderr", logChecked(form, "stderr") ? "1" : "0");
    params.set("follow", state.follow ? "1" : "0");
    logServices(form).forEach(function (name) {
      params.append("services", name);
    });
    return state.root.getAttribute("data-log-stream") + "?" + params.toString();
  }

  function updateLogDownload(state) {
    if (!state.download) {
      return;
    }
    var form = state.form;
    var params = new URLSearchParams();
    params.set("tail", "all");
    ["since", "until"].forEach(function (name) {
      if (logValue(form, name)) {
        params.set(name, logValue(form, name));
      }
    });
    params.set("stdout", logChecked(form, "stdout") ? "1" : "0");
    params.set("stderr", logChecked(form, "stderr") ? "1" : "0");
    params.set("timestamps", logChecked(form, "timestamps") ? "1" : "0");
    if (logValue(form, "q")) {
      params.set("q", logValue(form, "q"));
    }
    logServices(form).forEach(function (name) {
      params.append("services", name);
    });
    var base = state.root.getAttribute("data-log-download") + "?" + params.toString();
    state.download.setAttribute("href", base + "&format=text");
    if (state.downloadNdjson) {
      state.downloadNdjson.setAttribute("href", base + "&format=ndjson");
    }
  }

  function messageOfLogRefusal(response, body) {
    var type = response.headers.get("Content-Type") || "";
    if (type.indexOf("text/html") === 0) {
      var parsed = new DOMParser().parseFromString(body, "text/html");
      var node = parsed.querySelector(".state__body");
      return node ? node.textContent.trim() : LOG_FAILURE_TEXT;
    }
    return body.trim().slice(0, 300) || LOG_FAILURE_TEXT;
  }

  function explainLogFailure(state, url) {
    var controller = new AbortController();
    fetch(url, { credentials: "same-origin", headers: { Accept: "text/html, text/plain" }, signal: controller.signal })
      .then(function (response) {
        if (response.ok) {
          controller.abort();
          return LOG_FAILURE_TEXT;
        }
        return response.text().then(function (body) {
          return messageOfLogRefusal(response, body);
        });
      })
      .catch(function () {
        return LOG_FAILURE_TEXT;
      })
      .then(function (message) {
        if (logs === state && !state.source) {
          setLogStatus(state, "error", message);
        }
      });
  }

  function syncLogFollow(state) {
    var blocked = !state.running || logValue(state.form, "until") !== "";
    state.followBox.disabled = blocked;
    if (blocked) {
      state.followBox.checked = false;
    }
  }

  function finishLog(state, source, reason) {
    if (state.source !== source) {
      return;
    }
    closeLogSource(state);
    state.ended = true;
    announceLog(state);
    if (reason === "unavailable") {
      setLogStatus(state, "error", LOG_FAILURE_TEXT);
      return;
    }
    if (state.follow && state.running) {
      state.running = false;
      syncLogFollow(state);
      state.follow = false;
      setLogStatus(state, "idle", "The " + state.subject + " stopped.");
    } else if (logValue(state.form, "until")) {
      setLogStatus(state, "idle", "Reached the end of the selected range.");
    } else {
      setLogStatus(state, "idle", "End of the log.");
    }
  }

  function connectLog(state, resume) {
    closeLogSource(state);
    state.ended = false;
    state.suspended = false;
    var url = logUrl(state, resume);
    var source = new EventSource(url);
    state.source = source;
    setLogStatus(state, "idle", resume ? "Resuming." : "Connecting.");
    source.addEventListener("open", function () {
      if (state.source === source) {
        setLogStatus(state, "idle", state.follow ? "Following new output." : "Loading output.");
      }
    });
    source.addEventListener("out", function (event) {
      addLogEvent(state, source, event, false);
    });
    source.addEventListener("err", function (event) {
      addLogEvent(state, source, event, true);
    });
    source.addEventListener("notice", function (event) {
      showLogNotice(state, source, event.data);
    });
    source.addEventListener("end", function (event) {
      finishLog(state, source, event.data);
    });
    source.addEventListener("error", function () {
      if (state.source !== source) {
        return;
      }
      if (source.readyState === EventSource.CLOSED) {
        closeLogSource(state);
        state.ended = true;
        setLogStatus(state, "error", "Checking why the logs stopped.");
        explainLogFailure(state, url);
      } else {
        setLogStatus(state, "idle", "Reconnecting.");
      }
    });
  }

  function clearLog(state) {
    state.output.textContent = "";
    state.retained = 0;
    state.matching = 0;
    state.trimmed = 0;
    state.lastEventId = null;
    state.stick = true;
    state.jump.hidden = true;
    state.count.hidden = true;
    state.notices = [];
    if (state.notice) {
      state.notice.textContent = "";
      state.notice.hidden = true;
    }
  }

  function applyLogForm(state) {
    var form = state.form;
    if (!logChecked(form, "stdout") && !logChecked(form, "stderr")) {
      closeLogSource(state);
      setLogStatus(state, "error", "Select at least one of stdout and stderr.");
      return;
    }
    syncLogFollow(state);
    state.follow = state.followBox.checked;
    clearLog(state);
    connectLog(state, false);
  }

  function filterLog(state) {
    state.filter = state.filterBox.value.toLowerCase();
    state.matching = 0;
    var lines = state.output.children;
    for (var i = 0; i < lines.length; i += 1) {
      var hidden = !logMatches(state.filter, lines[i].lastChild.textContent);
      lines[i].hidden = hidden;
      if (!hidden) {
        state.matching += 1;
      }
    }
    if (state.retained > 0) {
      showLogCount(state);
      announceLog(state);
    }
  }

  function setLogFollow(state, on) {
    state.follow = on;
    if (!on) {
      closeLogSource(state);
      state.suspended = false;
      setLogStatus(state, "idle", "Not following. Turn on Follow new output to continue.");
      return;
    }
    connectLog(state, state.lastEventId !== null);
  }

  function suspendLog(state) {
    if (state.source && state.follow && !state.ended) {
      closeLogSource(state);
      state.suspended = true;
      setLogStatus(state, "idle", "Paused while this tab is not visible.");
    }
  }

  function resumeLog(state) {
    if (state.suspended) {
      connectLog(state, state.lastEventId !== null);
    }
  }

  function destroyLogs() {
    if (logs) {
      closeLogSource(logs);
      window.clearTimeout(logs.filterTimer);
    }
    logs = null;
  }

  function bindLogs(root) {
    var state = {
      root: root,
      form: root.querySelector("[data-log-form]"),
      output: root.querySelector("[data-log-output]"),
      status: root.querySelector("[data-log-status]"),
      count: root.querySelector("[data-log-count]"),
      announce: root.querySelector("[data-log-announce]"),
      notice: root.querySelector("[data-log-notice]"),
      notices: [],
      controls: root.querySelector("[data-log-controls]"),
      followBox: root.querySelector("[data-log-follow]"),
      budgetBox: root.querySelector("[data-log-budget]"),
      jump: root.querySelector("[data-log-jump]"),
      download: root.querySelector("[data-log-download-link]"),
      downloadNdjson: root.querySelector("[data-log-download-ndjson]"),
      running: root.getAttribute("data-log-running") === "true",
      labelled: root.hasAttribute("data-log-labelled"),
      subject: root.getAttribute("data-log-subject") || "container",
      source: null,
      lastEventId: null,
      follow: false,
      ended: false,
      suspended: false,
      stick: true,
      retained: 0,
      matching: 0,
      trimmed: 0,
      filter: "",
      filterTimer: null
    };
    if (!state.form || !state.output || !state.status || !state.count || !state.controls ||
        !state.followBox || !state.budgetBox || !state.jump) {
      return;
    }
    state.filterBox = state.form.elements.namedItem("q");
    state.budget = parseInt(state.budgetBox.value, 10);
    state.filter = state.filterBox.value.toLowerCase();
    root.querySelectorAll("[data-log-static]").forEach(function (section) {
      section.hidden = true;
    });
    state.output.hidden = false;
    state.controls.hidden = false;
    syncLogFollow(state);
    state.follow = state.followBox.checked;

    state.output.addEventListener("scroll", function () {
      state.stick = logAtBottom(state.output);
      state.jump.hidden = state.stick;
    });
    state.jump.addEventListener("click", function () {
      state.output.scrollTop = state.output.scrollHeight;
      state.output.focus();
    });
    state.followBox.addEventListener("change", function () {
      setLogFollow(state, state.followBox.checked);
    });
    state.budgetBox.addEventListener("change", function () {
      state.budget = parseInt(state.budgetBox.value, 10);
      trimLog(state);
      if (state.retained > 0) {
        showLogCount(state);
      }
    });
    state.form.addEventListener("input", function (event) {
      var target = event.target;
      if (target === state.filterBox) {
        window.clearTimeout(state.filterTimer);
        state.filterTimer = window.setTimeout(function () {
          filterLog(state);
        }, LOG_FILTER_DELAY_MS);
      } else if (target === state.form.elements.namedItem("until")) {
        syncLogFollow(state);
      }
      updateLogDownload(state);
    });
    state.form.addEventListener("change", function (event) {
      var target = event.target;
      if (target instanceof HTMLInputElement && target.name === "timestamps") {
        state.output.setAttribute("data-timestamps", target.checked ? "on" : "off");
      }
      updateLogDownload(state);
    });
    state.form.addEventListener("submit", function (event) {
      event.preventDefault();
      applyLogForm(state);
    });
    logs = state;
    updateLogDownload(state);
    if (root.hasAttribute("data-log-invalid")) {
      setLogStatus(state, "error", "Correct the highlighted fields, then apply.");
    } else {
      connectLog(state, false);
    }
  }

  function syncLogs() {
    var root = document.querySelector("[data-log-view]");
    if (logs && logs.root !== root) {
      destroyLogs();
    }
    if (root && !logs && typeof EventSource === "function") {
      bindLogs(root);
    }
    if (logs) {
      if (document.hidden) {
        suspendLog(logs);
      } else {
        resumeLog(logs);
      }
    }
  }

  function initLogs() {
    document.addEventListener("htmx:beforeCleanupElement", function (event) {
      if (logs && event.target instanceof Element && event.target.contains(logs.root)) {
        destroyLogs();
      }
    });
    document.addEventListener("htmx:afterSettle", syncLogs);
    document.addEventListener("htmx:historyRestore", syncLogs);
    document.addEventListener("visibilitychange", syncLogs);
    window.addEventListener("pageshow", syncLogs);
    window.addEventListener("pagehide", function () {
      if (logs) {
        suspendLog(logs);
      }
    });
    syncLogs();
  }

  function initRestart() {
    var panel = document.querySelector("[data-restart-poll]");
    if (!panel) {
      return;
    }
    var original = panel.getAttribute("data-boot-id");
    var giveUp = Number(panel.getAttribute("data-give-up")) * 1000;
    var message = panel.querySelector("[data-restart-message]");
    var done = panel.querySelector("[data-restart-done]");
    var failed = panel.querySelector("[data-restart-failed]");

    function finish(outcome) {
      panel.setAttribute("data-restart-state", outcome);
      if (message) {
        message.hidden = true;
      }
      var shown = outcome === "done" ? done : failed;
      if (shown) {
        shown.hidden = false;
      }
    }

    panel.addEventListener("htmx:beforeRequest", function (event) {
      if (panel.hasAttribute("data-restart-state")) {
        event.preventDefault();
      }
    });
    panel.addEventListener("htmx:afterSwap", function () {
      var current = panel.querySelector("#boot-id");
      var id = current ? current.textContent.trim() : "";
      if (!panel.hasAttribute("data-restart-state") && id !== "" && id !== original) {
        finish("done");
      }
    });
    if (giveUp > 0) {
      window.setTimeout(function () {
        if (!panel.hasAttribute("data-restart-state")) {
          finish("failed");
        }
      }, giveUp);
    }
  }

  /* The give-up clock starts when the status first reaches the phase named in data-give-up-phase. */

  function initUpdate() {
    var panel = document.querySelector("[data-update-poll]");
    if (!panel) {
      return;
    }
    var giveUp = Number(panel.getAttribute("data-give-up")) * 1000;
    var giveUpPhase = panel.getAttribute("data-give-up-phase");
    var failed = panel.querySelector("[data-update-failed]");
    var timer = null;

    function settleRestartPoll() {
      var boot = document.querySelector("[data-restart-poll]");
      if (!boot) {
        return;
      }
      boot.setAttribute("data-restart-state", "settled");
      var message = boot.querySelector("[data-restart-message]");
      if (message) {
        message.hidden = true;
      }
    }

    function sync() {
      var status = panel.querySelector("[data-phase]");
      if (!status) {
        return;
      }
      if (status.hasAttribute("data-terminal")) {
        panel.setAttribute("data-update-state", "terminal");
        if (timer !== null) {
          window.clearTimeout(timer);
          timer = null;
        }
        if (failed) {
          failed.hidden = true;
        }
        settleRestartPoll();
        return;
      }
      if (timer === null && giveUp > 0 && status.getAttribute("data-phase") === giveUpPhase) {
        timer = window.setTimeout(function () {
          if (!panel.hasAttribute("data-update-state") && failed) {
            panel.setAttribute("data-update-state", "failed");
            failed.hidden = false;
          }
        }, giveUp);
      }
    }

    document.addEventListener("htmx:afterSwap", sync);
    document.addEventListener("htmx:afterSettle", sync);
    sync();
  }

  var MASKED_REVEAL = "\u2022".repeat(16);
  var REVEAL_VISIBLE_MS = 60000;
  var armedReveals = new WeakSet();

  function maskReveals() {
    document.querySelectorAll("[data-revealed]").forEach(function (element) {
      element.textContent = MASKED_REVEAL;
    });
  }

  function armReveals() {
    document.querySelectorAll("[data-revealed]").forEach(function (element) {
      if (armedReveals.has(element)) {
        return;
      }
      armedReveals.add(element);
      window.setTimeout(function () {
        element.textContent = MASKED_REVEAL;
      }, REVEAL_VISIBLE_MS);
    });
  }

  function initRevealedValues() {
    window.addEventListener("pagehide", maskReveals);
    document.addEventListener("htmx:afterSettle", armReveals);
    armReveals();
  }

  function init() {
    initThemeToggle();
    initDialogs();
    initSubmitOnce();
    initRepeatRows();
    initCodeEditors(document);
    initGotoLine();
    initTargetKind();
    initToasts();
    initBusy();
    initHtmx();
    if (window.htmx) {
      document.documentElement.setAttribute("data-enhanced", "");
    }
    initStats();
    initJobs();
    initLogs();
    initRestart();
    initUpdate();
    initRevealedValues();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
