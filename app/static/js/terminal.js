/* Container terminal: form helpers and the page that connects xterm.js to the terminal WebSocket. */
(function () {
  "use strict";

  var ACK_BYTES = 131072;
  var INPUT_CHUNK_BYTES = 32768;
  var FIT_DELAY_MS = 120;
  var SCROLLBACK_LINES = 5000;
  var MAX_COLUMNS = 1000;
  var MAX_ROWS = 500;
  var LINE_HEIGHT_ESTIMATE = 1.2;
  var SCROLLBAR_ESTIMATE_PX = 15;
  var NORMAL_CLOSE = 1000;
  var ACTIVITY_INTERVAL_MS = 5000;
  var LEAVE_KEY = "F6";
  var RUNNING_TEXT = "The session ended, but the command may still be running in the container.";

  var PALETTE = [
    ["black", "black"],
    ["red", "red"],
    ["green", "green"],
    ["yellow", "yellow"],
    ["blue", "blue"],
    ["magenta", "magenta"],
    ["cyan", "cyan"],
    ["white", "white"],
    ["brightBlack", "bright-black"],
    ["brightRed", "bright-red"],
    ["brightGreen", "bright-green"],
    ["brightYellow", "bright-yellow"],
    ["brightBlue", "bright-blue"],
    ["brightMagenta", "bright-magenta"],
    ["brightCyan", "bright-cyan"],
    ["brightWhite", "bright-white"]
  ];

  var END_TEXT = {
    process_exited: "The process exited.",
    detached: "The session detached from the container.",
    container_stopped: "The container stopped.",
    environment_closed: "The connection to the Docker host was closed.",
    idle_timeout: "The terminal was closed after 15 minutes without input.",
    session_expired: "Your sign-in expired.",
    max_duration: "The terminal was closed at the four hour session limit.",
    access_revoked: "Your access to this container ended, so the terminal was closed.",
    engine_error: "The Docker host reported an error and the terminal was closed.",
    protocol_error: "The terminal was closed after an unexpected message.",
    restricted_container: "Only administrators may open a terminal in this container.",
    access_denied: "You can no longer open a terminal in this container.",
    not_running: "The container is not running.",
    object_busy: "Another action on this container is in progress. Try again shortly.",
    attach_busy: "Another session is already attached to this container.",
    attach_unavailable: "This container cannot be attached to.",
    unreachable: "The Docker host could not be reached.",
    outcome_unknown: "The Docker host did not answer in time. The terminal may have started.",
    slots_user: "You have too many terminals open. Close one, then try again.",
    slots_process: "This server has reached its limit of open terminals. Try again shortly.",
    bad_ticket: "The terminal ticket is not valid or has expired.",
    invalid: "The terminal request was not valid. Reload the form and try again.",
    access_unverified: "Your access to this container could not be confirmed, so the terminal was closed."
  };

  var CLOSE_TEXT = {
    1000: "The terminal session ended.",
    1006: "The connection to dockmin was lost.",
    1008: "The connection was refused. Open a new terminal to get a fresh ticket.",
    1009: "A message was too large and the terminal was closed.",
    1011: "The server reported an error and closed the terminal.",
    4403: "Your access to this container ended, so the terminal was closed.",
    4408: "The terminal was closed after 15 minutes without input.",
    4409: "The terminal was closed at the four hour session limit.",
    4429: "No terminal is free. Close one of your open terminals, then try again."
  };

  var CALM_END = {
    process_exited: true,
    detached: true,
    container_stopped: true,
    client_closed: true
  };

  function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  function readTheme(frame) {
    var style = window.getComputedStyle(frame);
    function token(name) {
      return style.getPropertyValue("--terminal-" + name).trim();
    }
    var theme = {
      background: token("background"),
      foreground: token("foreground"),
      cursor: token("cursor"),
      cursorAccent: token("background"),
      selectionBackground: token("selection")
    };
    PALETTE.forEach(function (pair) {
      theme[pair[0]] = token(pair[1]);
    });
    return theme;
  }

  function watchTheme(callback) {
    var observer = new MutationObserver(callback);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    if (typeof window.matchMedia === "function") {
      var query = window.matchMedia("(prefers-color-scheme: dark)");
      if (typeof query.addEventListener === "function") {
        query.addEventListener("change", callback);
      }
    }
  }

  function utf8(text) {
    return new TextEncoder().encode(text);
  }

  function startTerminal(root) {
    var screen = root.querySelector("[data-terminal-screen]");
    var statusLine = root.querySelector("[data-terminal-status]");
    var actions = root.querySelector("[data-terminal-actions]");
    var frame = screen.parentElement;
    var path = root.getAttribute("data-terminal-ws");
    var ticket = root.getAttribute("data-terminal-ticket");
    root.removeAttribute("data-terminal-ticket");
    root.hidden = false;

    function setStatus(state, text) {
      statusLine.setAttribute("data-state", state);
      statusLine.textContent = text;
    }

    if (typeof window.Terminal !== "function" || !window.FitAddon || typeof WebSocket !== "function") {
      setStatus("error", "The terminal could not be loaded. Reload the page, or open a new terminal.");
      actions.hidden = false;
      return;
    }

    var style = window.getComputedStyle(frame);
    var term = new window.Terminal({
      cursorBlink: true,
      scrollback: SCROLLBACK_LINES,
      fontFamily: style.fontFamily,
      fontSize: parseFloat(style.fontSize),
      minimumContrastRatio: 4.5,
      screenReaderMode: true,
      linkHandler: { activate: function () {} },
      theme: readTheme(frame)
    });
    var fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(screen);
    fit.fit();
    watchTheme(function () {
      term.options.theme = readTheme(frame);
    });

    var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    var socket = new WebSocket(scheme + "//" + window.location.host + path);
    socket.binaryType = "arraybuffer";

    var phase = "connecting";
    var connected = false;
    var ending = null;
    var parsedSinceAck = 0;
    var fitTimer = null;
    var warned = false;
    var lastActivity = 0;

    function isOpen() {
      return socket.readyState === WebSocket.OPEN;
    }

    function sendControl(message) {
      if (isOpen()) {
        socket.send(JSON.stringify(message));
      }
    }

    function sendSize() {
      sendControl({
        t: "resize",
        cols: clamp(term.cols, 1, MAX_COLUMNS),
        rows: clamp(term.rows, 1, MAX_ROWS)
      });
    }

    function sendInput(bytes) {
      for (var offset = 0; offset < bytes.length; offset += INPUT_CHUNK_BYTES) {
        socket.send(bytes.subarray(offset, offset + INPUT_CHUNK_BYTES));
      }
    }

    function noteActivity(event) {
      if (!event.isTrusted || phase !== "open") {
        return;
      }
      var now = Date.now();
      if (warned) {
        warned = false;
        lastActivity = 0;
        setStatus("open", "Connected.");
      }
      if (now - lastActivity >= ACTIVITY_INTERVAL_MS) {
        lastActivity = now;
        sendControl({ t: "activity" });
      }
    }

    function isLeaveChord(event) {
      return event.key === LEAVE_KEY && event.ctrlKey && event.shiftKey && !event.altKey && !event.metaKey;
    }

    function leaveTerminal() {
      var back = document.querySelector("[data-terminal-back]");
      term.blur();
      if (back) {
        back.focus();
      }
    }

    term.attachCustomKeyEventHandler(function (event) {
      if (isLeaveChord(event)) {
        if (event.type === "keydown") {
          event.preventDefault();
          leaveTerminal();
        }
        return false;
      }
      if (event.type === "keydown") {
        noteActivity(event);
      }
      return true;
    });
    screen.addEventListener("paste", noteActivity, true);
    screen.addEventListener("compositionend", noteActivity, true);
    if (term.textarea) {
      term.textarea.setAttribute("aria-describedby", "terminal-help");
    }

    term.onData(function (text) {
      if (phase === "open" && isOpen()) {
        sendInput(utf8(text));
      }
    });
    term.onBinary(function (text) {
      if (phase === "open" && isOpen()) {
        var bytes = new Uint8Array(text.length);
        for (var i = 0; i < text.length; i += 1) {
          bytes[i] = text.charCodeAt(i) & 255;
        }
        sendInput(bytes);
      }
    });
    term.onResize(function () {
      if (phase === "open") {
        sendSize();
      }
    });

    function scheduleFit() {
      window.clearTimeout(fitTimer);
      fitTimer = window.setTimeout(function () {
        fit.fit();
      }, FIT_DELAY_MS);
    }
    var observer = typeof ResizeObserver === "function" ? new ResizeObserver(scheduleFit) : null;
    if (observer) {
      observer.observe(screen);
    }

    function acknowledge(length) {
      parsedSinceAck += length;
      if (parsedSinceAck >= ACK_BYTES) {
        sendControl({ t: "ack", n: parsedSinceAck });
        parsedSinceAck = 0;
      }
    }

    function writeOutput(buffer) {
      var bytes = new Uint8Array(buffer);
      term.write(bytes, function () {
        acknowledge(bytes.length);
      });
    }

    function handleControl(text) {
      var message;
      try {
        message = JSON.parse(text);
      } catch (error) {
        return;
      }
      if (!message || typeof message.t !== "string") {
        return;
      }
      if (message.t === "ready") {
        phase = "open";
        setStatus("open", "Connected.");
        sendSize();
        term.focus();
      } else if (message.t === "notice" && message.code === "idle_warning") {
        warned = true;
        setStatus("warn", "This terminal closes in one minute unless you type.");
      } else if (message.t === "end") {
        ending = message;
        if (message.reason === "process_exited" && typeof message.exit_code === "number") {
          term.write("\r\n\x1b[2m[Process exited with code " + message.exit_code + "]\x1b[0m\r\n");
        }
      }
    }

    function endText() {
      if (ending && ending.reason === "process_exited" && typeof ending.exit_code === "number") {
        return "The process exited with code " + ending.exit_code + ".";
      }
      if (ending && typeof ending.message === "string" && ending.message) {
        return ending.message;
      }
      if (ending && END_TEXT[ending.reason]) {
        return END_TEXT[ending.reason];
      }
      return null;
    }

    function finish(code) {
      if (phase === "ended") {
        return;
      }
      var opened = phase === "open";
      phase = "ended";
      window.clearTimeout(fitTimer);
      if (observer) {
        observer.disconnect();
      }
      term.options.cursorBlink = false;
      var text = endText() || CLOSE_TEXT[code] || "The terminal was closed.";
      if (!connected && !ending) {
        text = "The terminal could not connect. Open a new terminal, or sign in again if your session ended.";
      }
      var calm = (ending ? CALM_END[ending.reason] === true : false) || (!ending && opened && code === NORMAL_CLOSE);
      if (ending && ending.process_running === true) {
        setStatus("warn", RUNNING_TEXT);
      } else {
        setStatus(calm ? "ended" : "error", text);
      }
      actions.hidden = false;
      term.options.disableStdin = true;
      if (screen && screen.contains(document.activeElement)) {
        var first = actions.querySelector("a");
        if (first) {
          first.focus();
        }
      }
    }

    socket.addEventListener("open", function () {
      connected = true;
      socket.send(JSON.stringify({ t: "ticket", v: ticket }));
      ticket = null;
    });
    socket.addEventListener("message", function (event) {
      if (typeof event.data === "string") {
        handleControl(event.data);
      } else {
        writeOutput(event.data);
      }
    });
    socket.addEventListener("close", function (event) {
      finish(event.code);
    });
    window.addEventListener("pagehide", function () {
      if (phase !== "ended" && socket.readyState <= WebSocket.OPEN) {
        socket.close(NORMAL_CLOSE);
      }
    });
  }

  /* The hidden size fields get the size the terminal frame will have, estimated from the monospace font. */

  function estimateSize(probe) {
    var style = window.getComputedStyle(probe);
    var context = document.createElement("canvas").getContext("2d");
    if (!context) {
      return null;
    }
    var fontSize = parseFloat(style.fontSize);
    context.font = fontSize + "px " + style.fontFamily;
    var cell = context.measureText("W").width;
    var box = probe.getBoundingClientRect();
    var inset = parseFloat(window.getComputedStyle(document.documentElement).fontSize);
    if (!cell || !box.width || !box.height) {
      return null;
    }
    return {
      cols: clamp(Math.floor((box.width - inset - SCROLLBAR_ESTIMATE_PX) / cell), 20, MAX_COLUMNS),
      rows: clamp(Math.floor((box.height - inset) / (fontSize * LINE_HEIGHT_ESTIMATE)), 5, MAX_ROWS)
    };
  }

  function initForm(form) {
    var command = form.querySelector("#field-command");
    var quick = form.querySelector("[data-quick-commands]");
    if (command && quick) {
      quick.hidden = false;
      quick.addEventListener("click", function (event) {
        var target = event.target;
        if (target instanceof Element) {
          var button = target.closest("[data-command-fill]");
          if (button) {
            command.value = button.getAttribute("data-command-fill");
            command.focus();
          }
        }
      });
    }
    var probe = document.querySelector("[data-terminal-probe]");
    var rows = form.querySelector("[data-terminal-rows]");
    var cols = form.querySelector("[data-terminal-cols]");
    if (probe && rows && cols) {
      var size = estimateSize(probe);
      if (size) {
        rows.value = String(size.rows);
        cols.value = String(size.cols);
      }
    }
  }

  function init() {
    var root = document.querySelector("[data-terminal]");
    if (root) {
      startTerminal(root);
    }
    var form = document.querySelector("[data-terminal-form]");
    if (form) {
      initForm(form);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
