/* RefundClaimers — shared front-end helpers.
 *
 * Security note: nothing here is a security control. Every permission is
 * enforced by the server. Dynamic content is inserted with textContent / DOM
 * construction only (never innerHTML).
 */
(function () {
  "use strict";

  const MAX_AMOUNT_CENTS = 100000000000; // mirrors backend/money.py

  // ---- DOM helpers ----------------------------------------------------------

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [key, value] of Object.entries(props)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "dataset") Object.assign(node.dataset, value);
        else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
        else if (value === true) node.setAttribute(key, "");
        else node.setAttribute(key, String(value));
      }
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $all(selector, root) { return Array.from((root || document).querySelectorAll(selector)); }

  // ---- money ------------------------------------------------------------------

  function formatCents(cents, opts) {
    const signed = opts && opts.signed;
    const n = Number(cents);
    const abs = Math.abs(n);
    const dollars = Math.floor(abs / 100);
    const rem = abs % 100;
    const body = "$" + dollars.toLocaleString("en-US") + "." + String(rem).padStart(2, "0");
    if (n < 0) return "−" + body; // true minus sign
    if (signed && n > 0) return "+" + body;
    return body;
  }

  const AMOUNT_RE = /^([+-])?\$?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?$/;

  /** Parse "1,250.50" / "-25" into integer cents using string arithmetic only. */
  function parseAmountToCents(raw, opts) {
    const allowSign = !opts || opts.allowSign !== false;
    const text = String(raw || "").trim().replace(/^−/, "-");
    if (!text) return { ok: false, error: "Enter an amount." };
    const m = AMOUNT_RE.exec(text);
    if (!m) {
      if (/^[+-]?\$?[\d,]+\.\d{3,}$/.test(text)) return { ok: false, error: "Use at most two decimal places." };
      return { ok: false, error: "Enter a valid amount, for example 125.50." };
    }
    const [, sign, whole, frac] = m;
    if (sign && !allowSign) return { ok: false, error: "Enter the amount without a sign." };
    const digits = whole.replace(/,/g, "");
    if (digits.length > 12) return { ok: false, error: "Amount is too large." };
    let cents = Number(digits) * 100 + Number((frac || "0").padEnd(2, "0"));
    if (sign === "-") cents = -cents;
    if (Math.abs(cents) > MAX_AMOUNT_CENTS) {
      return { ok: false, error: "Amounts must not exceed " + formatCents(MAX_AMOUNT_CENTS) + "." };
    }
    return { ok: true, cents };
  }

  // ---- dates ------------------------------------------------------------------

  const dateTimeFmt = new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
  const dateFmt = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" });

  function formatDateTime(iso) {
    const d = new Date(iso);
    return isNaN(d) ? "" : dateTimeFmt.format(d);
  }
  function formatDate(iso) {
    const d = new Date(iso);
    return isNaN(d) ? "" : dateFmt.format(d);
  }
  function timeEl(iso, kind) {
    return el("time", { datetime: iso, title: iso.replace("T", " ").replace("Z", " UTC") },
      kind === "date" ? formatDate(iso) : formatDateTime(iso));
  }
  function hydrateTimes(root) {
    $all("time[data-ts]", root).forEach((node) => {
      const iso = node.dataset.ts;
      if (!iso) return;
      node.setAttribute("datetime", iso);
      node.title = iso.replace("T", " ").replace("Z", " UTC");
      node.textContent = node.dataset.tsFormat === "date" ? formatDate(iso) : formatDateTime(iso);
    });
  }

  // ---- API client -------------------------------------------------------------

  class ApiError extends Error {
    constructor(status, code, message, details) {
      super(message);
      this.status = status;
      this.code = code;
      this.details = details || [];
    }
  }

  function csrfToken() {
    const meta = $('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  const FRIENDLY = {
    403: "You don’t have permission to do that.",
    404: "That record could not be found.",
    429: "Too many requests. Please wait a moment and try again.",
    500: "Something went wrong on the server. No changes were made.",
  };

  async function api(path, options) {
    const opts = options || {};
    const method = opts.method || "GET";
    const headers = { Accept: "application/json", ...(opts.headers || {}) };
    if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
    let body;
    if (opts.body !== undefined) {
      if (opts.body instanceof FormData) {
        body = opts.body;
      } else {
        headers["Content-Type"] = "application/json";
        body = JSON.stringify(opts.body);
      }
    }
    let response;
    try {
      response = await fetch(path, { method, headers, body, credentials: "same-origin", cache: "no-store" });
    } catch (_) {
      throw new ApiError(0, "network", "Unable to reach the server. Check that it is running and try again.");
    }
    let data = null;
    const type = response.headers.get("Content-Type") || "";
    if (type.includes("application/json")) {
      try { data = await response.json(); } catch (_) { data = null; }
    }
    if (response.ok) return data;

    const err = (data && data.error) || {};
    if (response.status === 401) {
      const next = encodeURIComponent(location.pathname + location.search);
      location.assign("/login?expired=1&next=" + next);
      throw new ApiError(401, "not_authenticated", "Your session has ended. Redirecting to sign in…");
    }
    const message = err.message || FRIENDLY[response.status] || FRIENDLY[500];
    throw new ApiError(response.status, err.code || "error", message, err.details);
  }

  // ---- UI feedback ------------------------------------------------------------

  function toast(message, type) {
    const region = $("[data-toast-region]");
    if (!region) return;
    const node = el("div", { class: "toast toast--" + (type || "info"), role: type === "error" ? "alert" : "status" },
      el("span", { class: "toast__text", text: message }),
      el("button", { type: "button", class: "toast__close", "aria-label": "Dismiss", onclick: () => node.remove() }, "×"));
    region.append(node);
    setTimeout(() => node.remove(), type === "error" ? 9000 : 5000);
  }

  function setBusy(button, busy, label) {
    if (!button) return;
    button.disabled = !!busy;
    button.classList.toggle("is-busy", !!busy);
    button.setAttribute("aria-busy", busy ? "true" : "false");
    const labelNode = button.querySelector("[data-label]");
    if (labelNode && label) labelNode.textContent = label;
  }

  function showError(node, message) {
    if (!node) return;
    node.textContent = message || "";
    node.hidden = !message;
  }

  function fieldErrors(form, errors) {
    $all("[data-error-for]", form).forEach((p) => {
      const msg = errors && errors[p.dataset.errorFor];
      p.textContent = msg || "";
      p.hidden = !msg;
      const input = form.querySelector('[name="' + p.dataset.errorFor + '"]');
      if (input) {
        input.setAttribute("aria-invalid", msg ? "true" : "false");
        if (msg) {
          if (!p.id) p.id = "err-" + p.dataset.errorFor + "-" + Math.random().toString(36).slice(2, 8);
          input.setAttribute("aria-errormessage", p.id);
        }
      }
    });
  }

  // ---- dialogs ----------------------------------------------------------------

  function openDialog(dialog) {
    if (!dialog) return;
    dialog.__returnFocus = document.activeElement;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    const first = dialog.querySelector("input:not([type=hidden]):not([disabled]), select, textarea, button:not([data-dialog-close])");
    if (first) first.focus();
  }

  function closeDialog(dialog) {
    if (!dialog || !dialog.open) return;
    if (dialog.dataset.locked === "1") return; // request in flight
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  document.addEventListener("click", (event) => {
    const closer = event.target.closest("[data-dialog-close]");
    if (closer) closeDialog(closer.closest("dialog"));
  });
  document.addEventListener("cancel", (event) => {
    if (event.target.tagName === "DIALOG" && event.target.dataset.locked === "1") event.preventDefault();
  }, true);
  document.addEventListener("close", (event) => {
    const dialog = event.target;
    if (dialog.tagName === "DIALOG" && dialog.__returnFocus && document.contains(dialog.__returnFocus)) {
      dialog.__returnFocus.focus();
    }
  }, true);

  /** Generic yes/no dialog. `action` is an async function; errors are shown inline. */
  function confirmAction({ title, message, confirmLabel, danger, action }) {
    const dialog = $("#confirm-dialog");
    $("[data-confirm-title]", dialog).textContent = title;
    $("[data-confirm-message]", dialog).textContent = message;
    const ok = $("[data-confirm-ok]", dialog);
    const errorBox = $("[data-dialog-error]", dialog);
    ok.className = "btn " + (danger ? "btn--danger" : "btn--primary");
    $("[data-label]", ok).textContent = confirmLabel || "Confirm";
    showError(errorBox, "");
    setBusy(ok, false);
    ok.onclick = async () => {
      setBusy(ok, true);
      dialog.dataset.locked = "1";
      showError(errorBox, "");
      try {
        await action();
        dialog.dataset.locked = "0";
        closeDialog(dialog);
      } catch (err) {
        showError(errorBox, err.message);
      } finally {
        dialog.dataset.locked = "0";
        setBusy(ok, false);
      }
    };
    openDialog(dialog);
  }

  // ---- shared widgets -----------------------------------------------------------

  function stateBlock(kind, title, text, action) {
    return el("div", { class: "state state--" + kind, role: kind === "error" ? "alert" : null },
      kind === "loading" ? el("span", { class: "spinner spinner--lg", "aria-hidden": "true" }) : null,
      el("p", { class: "state__title", text: title }),
      text ? el("p", { class: "state__text", text }) : null,
      action || null);
  }

  function pagination(meta, onPage, label) {
    if (!meta || meta.pages <= 1) {
      return meta && meta.total ? el("p", { class: "pager__summary", text: meta.total + (meta.total === 1 ? " record" : " records") }) : null;
    }
    const start = (meta.page - 1) * meta.page_size + 1;
    const end = Math.min(meta.total, meta.page * meta.page_size);
    return el("nav", { class: "pager", "aria-label": label || "Pagination" },
      el("p", { class: "pager__summary", text: start + "–" + end + " of " + meta.total }),
      el("div", { class: "pager__buttons" },
        el("button", { type: "button", class: "btn btn--secondary btn--sm", disabled: meta.page <= 1, onclick: () => onPage(meta.page - 1) }, "Previous"),
        el("span", { class: "pager__page", text: "Page " + meta.page + " of " + meta.pages }),
        el("button", { type: "button", class: "btn btn--secondary btn--sm", disabled: meta.page >= meta.pages, onclick: () => onPage(meta.page + 1) }, "Next")));
  }

  function amountCell(cents) {
    const kind = cents > 0 ? "pos" : cents < 0 ? "neg" : "zero";
    const label = cents > 0 ? "Increase of " : cents < 0 ? "Decrease of " : "";
    return el("span", { class: "amount amount--" + kind },
      el("span", { class: "visually-hidden", text: label }),
      el("span", { "aria-hidden": "true", class: "amount__icon", text: cents > 0 ? "▲" : cents < 0 ? "▼" : "•" }),
      formatCents(cents, { signed: true }));
  }

  const TYPE_LABEL = { set: "Balance set", adjust: "Adjustment" };

  /** Activity table bound to a container with data-endpoint / data-variant. */
  function qs(params) {
    const parts = [];
    for (const [k, v] of Object.entries(params || {})) {
      if (v === undefined || v === null || v === "") continue;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    }
    return parts.length ? "?" + parts.join("&") : "";
  }

  function ActivityTable(container, options) {
    const endpoint = container.dataset.endpoint;
    const isAdmin = container.dataset.variant === "admin";
    const pageSize = Number(container.dataset.pageSize) || 20;
    const noPager = container.dataset.noPager === "1";
    const opts = options || {};
    let filters = {};
    let page = 1;
    let seq = 0;

    async function load(p) {
      page = p || page;
      const mySeq = ++seq;
      container.setAttribute("aria-busy", "true");
      container.replaceChildren(stateBlock("loading", "Loading activity…"));
      try {
        const data = await api(endpoint + qs({ ...filters, page, page_size: pageSize }));
        if (opts.onLoad) opts.onLoad(data);
        if (mySeq !== seq) return;
        if (data.page !== page) page = data.page;
        render(data);
      } catch (err) {
        if (mySeq !== seq) return;
        container.replaceChildren(stateBlock("error", "Couldn’t load activity", err.message,
          el("button", { type: "button", class: "btn btn--secondary btn--sm", onclick: () => load(page) }, "Try again")));
      } finally {
        if (mySeq === seq) container.setAttribute("aria-busy", "false");
      }
    }

    function render(data) {
      if (!data.items.length) {
        container.replaceChildren(stateBlock("empty", container.dataset.emptyTitle || "No activity", container.dataset.emptyText));
        return;
      }
      const showAccount = !!opts.showAccount;
      const head = ["Date"];
      if (showAccount) head.push("Account");
      head.push("Type", "Previous", "Change", "Resulting", "Reason");
      if (isAdmin) head.push("Recorded by");
      const rows = data.items.map((a) => el("tr", null,
        el("td", { "data-label": "Date" }, timeEl(a.created_at)),
        showAccount ? el("td", { "data-label": "Account" },
          el("a", { href: "/admin/users/" + a.user_id, class: "strong-link", text: a.username || ("#" + a.user_id) })) : null,
        el("td", { "data-label": "Type" }, el("span", { class: "chip chip--" + a.change_type, text: TYPE_LABEL[a.change_type] || a.change_type })),
        el("td", { "data-label": "Previous", class: "num" }, formatCents(a.previous_cents)),
        el("td", { "data-label": "Change", class: "num" }, amountCell(a.amount_cents)),
        el("td", { "data-label": "Resulting", class: "num strong" }, formatCents(a.resulting_cents)),
        el("td", { "data-label": "Reason", class: "reason" },
          a.reason ? a.reason : el("span", { class: "muted", text: "—" }),
          a.claim_id ? el("a", { class: "claim-link", href: (isAdmin ? "/admin/claims?claim=" : "/dashboard/claims#claim-") + a.claim_id,
            text: "Claim #" + a.claim_id + (a.claim_merchant && !isAdmin ? " · " + a.claim_merchant : "") }) : null),
        isAdmin ? el("td", { "data-label": "Recorded by" }, a.admin_username || ("#" + a.admin_id),
          a.approved_by_username ? el("span", { class: "muted small approved-by", text: "approved by " + a.approved_by_username }) : null) : null));
      const table = el("div", { class: "table-wrap" },
        el("table", { class: "table table--stack" },
          el("caption", { class: "visually-hidden", text: "Balance activity" }),
          el("thead", null, el("tr", null, head.map((h) => el("th", { scope: "col", class: ["Previous", "Change", "Resulting"].includes(h) ? "num" : null, text: h })))),
          el("tbody", null, rows)));
      container.replaceChildren(table, (noPager ? null : pagination(data, load, "Activity pages")) || "");
    }

    return {
      load, reload: () => load(page), reset: () => load(1),
      setFilters(next) { filters = { ...next }; return load(1); },
    };
  }

  // ---- misc helpers -------------------------------------------------------------

  async function copyText(text, okMessage) {
    try {
      await navigator.clipboard.writeText(text);
      toast(okMessage || "Copied to clipboard.", "success");
    } catch (_) {
      window.prompt("Copy this:", text);
    }
  }

  const rtf = typeof Intl.RelativeTimeFormat === "function" ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }) : null;
  function relativeTime(iso) {
    const diff = (new Date(iso).getTime() - Date.now()) / 1000;
    if (!rtf || isNaN(diff)) return formatDateTime(iso);
    const units = [["year", 31536000], ["month", 2592000], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60]];
    for (const [unit, secs] of units) {
      if (Math.abs(diff) >= secs) return rtf.format(Math.round(diff / secs), unit);
    }
    return "just now";
  }

  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  function formValues(form) {
    const out = {};
    for (const node of form.elements) {
      if (!node.name || node.disabled) continue;
      if (node.type === "checkbox") out[node.name] = node.checked;
      else if (node.type === "radio") { if (node.checked) out[node.name] = node.value; }
      else out[node.name] = node.value;
    }
    return out;
  }

  // ---- global behaviours --------------------------------------------------------

  document.addEventListener("click", (event) => {
    const copyLink = event.target.closest("[data-copy-link]");
    if (copyLink) { copyText(location.origin + location.pathname, "Link to this page copied."); return; }
    const copyPath = event.target.closest("[data-copy-path]");
    if (copyPath) { copyText(location.origin + copyPath.dataset.copyPath, "Link copied."); return; }
    const copyTarget = event.target.closest("[data-copy-target]");
    if (copyTarget) {
      const node = document.querySelector(copyTarget.dataset.copyTarget);
      if (node) copyText(node.textContent.trim());
      return;
    }
    const toggle = event.target.closest("[data-toggle-password]");
    if (toggle) {
      const input = document.getElementById(toggle.dataset.togglePassword);
      if (!input) return;
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      toggle.textContent = show ? "Hide" : "Show";
      toggle.setAttribute("aria-pressed", show ? "true" : "false");
      return;
    }
    const navToggle = event.target.closest("[data-nav-toggle]");
    if (navToggle) {
      const expanded = navToggle.getAttribute("aria-expanded") === "true";
      navToggle.setAttribute("aria-expanded", expanded ? "false" : "true");
      document.getElementById(navToggle.getAttribute("aria-controls")).classList.toggle("is-open", !expanded);
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    hydrateTimes(document);
    $all("[data-counter]").forEach((field) => {
      const out = document.getElementById(field.dataset.counter);
      const update = () => { if (out) out.textContent = String(field.value.length); };
      field.addEventListener("input", update);
      update();
    });
    $all("[data-share-path]").forEach((n) => { n.textContent = location.origin + n.dataset.sharePath; });
  });

  function randomKey() {
    if (window.crypto && typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  window.Ledger = {
    el, $, $all, api, ApiError, toast, setBusy, showError, fieldErrors,
    openDialog, closeDialog, confirmAction, stateBlock, pagination, amountCell,
    formatCents, parseAmountToCents, formatDateTime, formatDate, timeEl, hydrateTimes,
    ActivityTable, randomKey, MAX_AMOUNT_CENTS, qs, copyText, relativeTime, debounce, formValues,
  };
})();
