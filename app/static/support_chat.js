/*
 * AI Support Chat widget: a floating button that opens a chat panel backed
 * by POST /api/ai-support/ (send) and GET /api/ai-support/history/ (load).
 *
 * Add it to any page with a single tag (it loads support_chat.css itself):
 *   <script src="/static/support_chat.js" defer></script>
 *
 * It reuses the access token the page already stored after login -- by
 * default the dashboard's "dashboard_access_token" localStorage key;
 * override with data-token-key="..." on the script tag.
 */
(function () {
  "use strict";

  if (window.__supportChatLoaded) return;
  window.__supportChatLoaded = true;

  var script = document.currentScript;
  var TOKEN_KEY = (script && script.getAttribute("data-token-key")) || "dashboard_access_token";
  var CSS_URL = script && script.src ? script.src.replace(/\.js(\?.*)?$/, ".css") : "/static/support_chat.css";
  var MAX_LENGTH = 2000;

  // Must match the full-screen @media query in support_chat.css.
  var FULLSCREEN_QUERY = window.matchMedia("(max-width: 600px), (max-height: 500px)");
  // On touch screens, focusing the input opens the keyboard, which would hide
  // the conversation the moment the chat opens -- so only focus it on request there.
  var IS_TOUCH = window.matchMedia("(pointer: coarse)").matches;

  var root, launcher, panel, closeBtn, messagesEl, form, input, sendBtn;
  var loadedForToken = null;
  var busy = false;

  // History paging (GET /api/ai-support/history/ returns newest first).
  var HISTORY_PAGE_SIZE = 20;
  var historyPage = 0;          // last page loaded
  var historyTotalPages = 0;
  var shownIds = {};            // ids already on screen, so a page shifted by new messages never shows duplicates
  var earlierWrap = null;       // the "Show earlier messages" row; older exchanges are inserted right after it

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY); } catch (e) { return null; }
  }

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function formatTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function scrollToBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }

  function isNearBottom() {
    return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 40;
  }

  function focusInputIfNoKeyboard() {
    if (!IS_TOUCH) input.focus();
  }

  // --- Rendering ----------------------------------------------------------

  function buildMessage(role, text, meta) {
    var wrap = make("div", "sc-msg " + (role === "user" ? "sc-msg-user" : "sc-msg-bot"));
    // textContent (never innerHTML): replies are shown exactly as plain text.
    wrap.appendChild(make("div", "sc-bubble", text));
    if (meta) wrap.appendChild(make("div", "sc-meta", meta));
    return wrap;
  }

  function addMessage(role, text, meta) {
    var wrap = buildMessage(role, text, meta);
    messagesEl.appendChild(wrap);
    scrollToBottom();
    return wrap;
  }

  function addNotice(text, isError) {
    var node = make("div", "sc-notice" + (isError ? " sc-error" : ""), text);
    messagesEl.appendChild(node);
    scrollToBottom();
    return node;
  }

  // Builds the given history items (newest first, as the API returns them)
  // as nodes in chronological order, skipping any already on screen.
  function buildExchanges(items) {
    var fragment = document.createDocumentFragment();
    items.slice().reverse().forEach(function (item) {
      if (shownIds[item.id]) return;
      shownIds[item.id] = true;
      fragment.appendChild(buildMessage("user", item.question, formatTime(item.created_at)));
      fragment.appendChild(buildMessage("bot", item.ai_response, formatTime(item.created_at)));
    });
    return fragment;
  }

  function showWelcome() {
    addMessage("bot", "Hi! I'm the support assistant. Ask me about plans, billing, posts, comments, " +
      "notifications or your account.");
  }

  // Send is only enabled when there's something to send.
  function updateSendState() {
    sendBtn.disabled = busy || input.disabled || !input.value.trim();
  }

  function setInputEnabled(enabled) {
    input.disabled = !enabled;
    updateSendState();
  }

  function showLoggedOut() {
    messagesEl.textContent = "";
    loadedForToken = null;
    addNotice("Please log in to chat with AI Support.");
    setInputEnabled(false);
  }

  // --- API ------------------------------------------------------------------

  var REQUEST_TIMEOUT_MS = 75000; // above the server's own AI timeout, so its FAQ fallback can answer first

  // Every failure becomes an Error with .kind ("auth", "network", "timeout",
  // "validation", "server", "empty") and a user-friendly .message.
  function apiError(kind, message) {
    var err = new Error(message);
    err.kind = kind;
    return err;
  }

  function validationMessage(detail) {
    // FastAPI 422 bodies: {"detail": [{"msg": "Value error, message cannot be blank", ...}]}
    var first = Array.isArray(detail) && detail[0];
    if (first && typeof first.msg === "string") {
      var msg = first.msg.replace(/^Value error, /, "");
      if (first.type === "string_too_long") msg = "Your message is too long (maximum " + MAX_LENGTH + " characters).";
      return msg.charAt(0).toUpperCase() + msg.slice(1) + (/[.!?]$/.test(msg) ? "" : ".");
    }
    return typeof detail === "string" ? detail : "That message couldn't be sent. Please check it and try again.";
  }

  async function api(path, options) {
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timer = controller && setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS);
    var res;
    try {
      res = await fetch(path, Object.assign({}, options, {
        signal: controller ? controller.signal : undefined,
        headers: Object.assign({ Authorization: "Bearer " + getToken() }, (options && options.headers) || {}),
      }));
    } catch (e) {
      if (e && e.name === "AbortError") {
        throw apiError("timeout", "AI Support is taking too long to answer. Please try again.");
      }
      throw apiError("network", "Couldn't reach AI Support. Check your connection and try again.");
    } finally {
      if (timer) clearTimeout(timer);
    }

    if (res.status === 401) throw apiError("auth", "Your session has expired. Please log in again.");

    var body = null;
    try { body = await res.json(); } catch (e) { /* not JSON */ }

    if (res.status === 422) throw apiError("validation", validationMessage(body && body.detail));
    if (!res.ok) {
      throw apiError("server", "AI Support is having trouble right now. Please try again in a moment.");
    }
    if (body === null) throw apiError("empty", "AI Support sent an unreadable reply. Please try again.");
    return body;
  }

  function resetHistoryState() {
    historyPage = 0;
    historyTotalPages = 0;
    shownIds = {};
    earlierWrap = null;
  }

  function renderEarlierButton() {
    if (historyPage >= historyTotalPages) {
      if (earlierWrap) earlierWrap.remove();
      earlierWrap = null;
      return;
    }
    if (!earlierWrap) {
      earlierWrap = make("div", "sc-earlier");
      var btn = make("button", "sc-earlier-btn", "Show earlier messages");
      btn.type = "button";
      btn.addEventListener("click", loadEarlier);
      earlierWrap.appendChild(btn);
    }
    var button = earlierWrap.firstChild;
    button.disabled = false;
    button.textContent = "Show earlier messages";
  }

  async function loadHistory() {
    var token = getToken();
    if (!token) { showLoggedOut(); return; }
    if (loadedForToken === token) return; // already showing this user's history

    messagesEl.textContent = "";
    resetHistoryState();
    setInputEnabled(false);
    var loading = addNotice("Loading your conversation…");
    loading.classList.add("sc-loading");
    try {
      var data = await api("/api/ai-support/history/?page=1&limit=" + HISTORY_PAGE_SIZE);
      if (getToken() !== token) return; // logged out/in as someone else meanwhile; that load wins
      messagesEl.textContent = "";
      showWelcome();
      var items = data.messages || [];
      historyPage = 1;
      historyTotalPages = data.total_pages || 0;
      renderEarlierButton();
      if (earlierWrap) messagesEl.appendChild(earlierWrap);
      if (items.length) {
        messagesEl.appendChild(buildExchanges(items));
      } else {
        addNotice("No previous conversations yet. Ask your first question below.").classList.add("sc-empty");
      }
      loadedForToken = token;
      scrollToBottom();
      setInputEnabled(true);
      focusInputIfNoKeyboard();
    } catch (e) {
      loading.remove();
      if (e.kind === "auth") { showLoggedOut(); return; }
      // History is optional: say so, offer a retry, and let the user keep chatting.
      if (!messagesEl.querySelector(".sc-msg")) showWelcome();
      var notice = addNotice("Couldn't load your earlier messages. ", true);
      var retry = make("button", "sc-link-btn", "Retry");
      retry.type = "button";
      retry.addEventListener("click", function () {
        loadedForToken = null;
        loadHistory();
      });
      notice.appendChild(retry);
      setInputEnabled(true);
      focusInputIfNoKeyboard();
    }
  }

  async function loadEarlier() {
    if (!earlierWrap) return;
    var button = earlierWrap.firstChild;
    button.disabled = true;
    button.textContent = "Loading…";
    try {
      var data = await api("/api/ai-support/history/?page=" + (historyPage + 1) + "&limit=" + HISTORY_PAGE_SIZE);
      historyPage += 1;
      historyTotalPages = data.total_pages || historyTotalPages;
      // Insert above what's on screen without moving what the user is looking at.
      var before = messagesEl.scrollHeight;
      messagesEl.insertBefore(buildExchanges(data.messages || []), earlierWrap.nextSibling);
      messagesEl.scrollTop += messagesEl.scrollHeight - before;
      renderEarlierButton();
    } catch (e) {
      if (e.kind === "auth") { showLoggedOut(); addNotice(e.message, true); return; }
      button.disabled = false;
      button.textContent = "Couldn't load. Try again";
    }
  }

  async function sendMessage(message) {
    var emptyNote = messagesEl.querySelector(".sc-empty");
    if (emptyNote) emptyNote.remove();
    busy = true;
    setInputEnabled(false);
    var sent = addMessage("user", message, "Sending…");
    var typing = make("div", "sc-msg sc-msg-bot");
    var dots = make("div", "sc-bubble sc-typing");
    dots.setAttribute("aria-label", "AI Support is typing");
    dots.innerHTML = "<span></span><span></span><span></span>";
    typing.appendChild(dots);
    messagesEl.appendChild(typing);
    scrollToBottom();

    try {
      var data = await api("/api/ai-support/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: message }),
      });
      var reply = data && typeof data.response === "string" ? data.response.trim() : "";
      if (!reply) throw apiError("empty", "AI Support didn't return an answer. Please try again.");

      typing.remove();
      sent.lastChild.textContent = formatTime(data.timestamp) || "Just now";
      addMessage("bot", data.response, formatTime(data.timestamp));
      busy = false;
      setInputEnabled(true);
      input.focus();
    } catch (e) {
      typing.remove();
      sent.lastChild.textContent = "Not sent";
      sent.classList.add("sc-msg-failed");
      busy = false; // before setInputEnabled, which re-checks it
      if (e.kind === "auth") {
        showLoggedOut();
        addNotice(e.message, true);
      } else {
        addNotice(e.message || "Couldn't send your message. Please try again.", true);
        // Put the message back so it can be retried without retyping.
        if (!input.value) input.value = message;
        setInputEnabled(true);
        input.focus();
      }
    } finally {
      updateSendState();
    }
  }

  // --- Open / close ---------------------------------------------------------

  // Full-screen mode only: size the panel to the visible viewport (which
  // excludes the on-screen keyboard) and stop the page behind it scrolling.
  function syncViewport() {
    var fullscreen = !panel.hidden && FULLSCREEN_QUERY.matches;
    document.documentElement.classList.toggle("sc-scroll-lock", fullscreen);
    var vv = window.visualViewport;
    if (fullscreen && vv) {
      var keepAtBottom = isNearBottom();
      root.style.setProperty("--sc-viewport-height", vv.height + "px");
      root.style.setProperty("--sc-viewport-top", vv.offsetTop + "px");
      if (keepAtBottom) scrollToBottom();
    } else {
      root.style.removeProperty("--sc-viewport-height");
      root.style.removeProperty("--sc-viewport-top");
    }
  }

  function open() {
    panel.hidden = false;
    root.classList.add("sc-open");
    launcher.setAttribute("aria-expanded", "true");
    syncViewport();
    closeBtn.focus(); // moved to the input once history has loaded (not on touch screens)
    loadHistory();
  }

  function close() {
    panel.hidden = true;
    root.classList.remove("sc-open");
    launcher.setAttribute("aria-expanded", "false");
    syncViewport();
    launcher.focus();
  }

  function build() {
    var link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = CSS_URL;
    document.head.appendChild(link);

    root = make("div", "sc-root");

    launcher = make("button", "sc-launcher");
    launcher.type = "button";
    launcher.setAttribute("aria-label", "Open AI Support chat");
    launcher.setAttribute("aria-haspopup", "dialog");
    launcher.setAttribute("aria-expanded", "false");
    launcher.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 ' +
      '4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';

    panel = make("div", "sc-panel");
    panel.hidden = true;
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "AI Support chat");

    var header = make("div", "sc-header");
    var headerText = make("div", "sc-header-text");
    headerText.appendChild(make("p", "sc-title", "AI Support"));
    headerText.appendChild(make("p", "sc-subtitle", "Ask about posts, plans, billing and more"));
    closeBtn = make("button", "sc-close", "×");
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "Close AI Support chat");
    header.appendChild(headerText);
    header.appendChild(closeBtn);

    messagesEl = make("div", "sc-messages");
    messagesEl.setAttribute("aria-live", "polite");

    form = make("form", "sc-form");
    input = make("textarea", "sc-input");
    input.rows = 1;
    input.maxLength = MAX_LENGTH;
    input.placeholder = "Type your question…";
    input.setAttribute("aria-label", "Your question");
    sendBtn = make("button", "sc-send", "Send");
    sendBtn.type = "submit";
    form.appendChild(input);
    form.appendChild(sendBtn);

    panel.appendChild(header);
    panel.appendChild(messagesEl);
    panel.appendChild(form);
    root.appendChild(panel);
    root.appendChild(launcher);
    document.body.appendChild(root);

    launcher.addEventListener("click", function () { panel.hidden ? open() : close(); });
    closeBtn.addEventListener("click", close);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !panel.hidden) close();
    });

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var message = input.value.trim();
      if (!message || busy) return; // blank input is never sent (Send is disabled too)
      if (!getToken()) { showLoggedOut(); return; }
      input.value = "";
      input.style.height = "";
      sendMessage(message);
    });

    // Enter sends; Shift+Enter adds a new line.
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        form.requestSubmit();
      }
    });
    input.addEventListener("input", function () {
      updateSendState();
      input.style.height = "";
      input.style.height = Math.min(input.scrollHeight, 120) + "px";
    });

    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", syncViewport);
      window.visualViewport.addEventListener("scroll", syncViewport);
    }
    if (FULLSCREEN_QUERY.addEventListener) FULLSCREEN_QUERY.addEventListener("change", syncViewport);

    // Another tab logging in or out changes who the chat belongs to.
    window.addEventListener("storage", function (e) {
      if (e.key === TOKEN_KEY && !panel.hidden) { loadedForToken = null; loadHistory(); }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
