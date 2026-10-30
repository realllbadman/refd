/* Administrator UI: statistics, account list, account creation, balance and status changes.
 * All rules are re-validated by the server; nothing here grants permission. */
(function () {
  "use strict";
  const L = window.Ledger;
  const { el, $, $all, api, toast, setBusy, showError, fieldErrors, openDialog, closeDialog, confirmAction,
    stateBlock, pagination, formatCents, parseAmountToCents, timeEl, hydrateTimes, ActivityTable } = L;

  let config = {};

  // ---- balance dialog (shared by overview and detail pages) ----------------------

  const BalanceDialog = (function () {
    let dialog, form, state;

    function init() {
      dialog = $("#balance-dialog");
      if (!dialog) return;
      form = $("[data-balance-form]", dialog);
      form.addEventListener("submit", onSubmit);
      form.addEventListener("change", (e) => { if (e.target.name === "mode") syncMode(); });
      form.addEventListener("input", () => { fieldErrors(form, {}); showError($("[data-dialog-error]", dialog), ""); });
      $("[data-back]", dialog).addEventListener("click", () => showStep("edit"));
    }

    function syncMode() {
      const mode = form.mode.value;
      $("[data-amount-label]", form).textContent = mode === "set" ? "Target balance (USD)" : "Adjustment amount (USD)";
      $("[data-amount-hint]", form).textContent = mode === "set"
        ? "The balance the account should have. Up to two decimal places."
        : "Use a minus sign to decrease, e.g. -25.00. Up to two decimal places.";
      form.amount.placeholder = mode === "set" ? "0.00" : "+0.00 or -0.00";
      $("#balance-dialog-title").textContent = (mode === "set" ? "Set" : "Adjust") + " balance";
    }

    function showStep(step) {
      state.step = step;
      $("[data-step=edit]", dialog).hidden = step !== "edit";
      $("[data-step=confirm]", dialog).hidden = step !== "confirm";
      $("[data-back]", dialog).hidden = step !== "confirm";
      $("[data-step-edit-only]", dialog).hidden = step === "confirm";
      $("[data-label]", $("[data-submit]", dialog)).textContent = step === "edit" ? "Review change" : "Confirm change";
      showError($("[data-dialog-error]", dialog), "");
      if (step === "edit") { state.idempotencyKey = null; form.amount.focus(); }
      else $("[data-submit]", dialog).focus();
    }

    /** Opens the dialog for a user, always fetching the latest confirmed balance first. */
    async function open(userId, mode, onDone) {
      if (!dialog) return;
      let user;
      try {
        user = (await api("/api/admin/users/" + userId + "/?page_size=1")).user;
      } catch (err) {
        toast(err.message, "error");
        return;
      }
      if (user.role !== "user") { toast("Balances can only be managed for account holders.", "error"); return; }
      state = { user, onDone, step: "edit", idempotencyKey: null, pending: null };
      form.reset();
      form.mode.value = mode || "set";
      syncMode();
      fieldErrors(form, {});
      $("[data-bal-account]", dialog).textContent = user.username + " (#" + user.id + ")";
      $("[data-bal-current]", dialog).textContent = formatCents(user.balance_cents);
      showStep("edit");
      openDialog(dialog);
      form.amount.focus();
    }

    function review() {
      const mode = form.mode.value;
      const parsed = parseAmountToCents(form.amount.value);
      const errors = {};
      if (!parsed.ok) errors.amount = parsed.error;
      const reason = form.reason.value.trim().replace(/\s+/g, " ");
      if (reason.length > 200) errors.reason = "Reason must be at most 200 characters.";
      let resulting = null;
      if (parsed.ok) {
        const previous = state.user.balance_cents;
        if (mode === "set") {
          resulting = parsed.cents;
          if (resulting === previous) errors.amount = "The balance is already " + formatCents(previous) + ".";
        } else {
          if (parsed.cents === 0) errors.amount = "Adjustment amount must not be zero.";
          resulting = previous + parsed.cents;
        }
        if (!errors.amount && Math.abs(resulting) > config.maxCents) errors.amount = "Resulting balance is out of range.";
        if (!errors.amount && resulting < 0 && !config.allowNegative) {
          errors.amount = "This would make the balance " + formatCents(resulting) + ". Negative balances are not permitted.";
        }
      }
      fieldErrors(form, errors);
      if (Object.keys(errors).length) { (errors.amount ? form.amount : form.reason).focus(); return; }

      state.pending = { mode, cents: parsed.cents, reason: reason || null, resulting };
      state.idempotencyKey = L.randomKey();
      const previous = state.user.balance_cents;
      $("[data-review-account]", dialog).textContent = state.user.username + " (#" + state.user.id + ")";
      $("[data-review-mode]", dialog).textContent = mode === "set" ? "Set balance" : "Adjust balance";
      $("[data-review-previous]", dialog).textContent = formatCents(previous);
      $("[data-review-requested-label]", dialog).textContent = mode === "set" ? "New target balance" : "Adjustment";
      $("[data-review-requested]", dialog).textContent = mode === "set"
        ? formatCents(parsed.cents) + "  (" + formatCents(parsed.cents - previous, { signed: true }) + ")"
        : formatCents(parsed.cents, { signed: true });
      $("[data-review-result]", dialog).textContent = formatCents(resulting);
      $("[data-review-reason]", dialog).textContent = reason || "—";
      showStep("confirm");
    }

    async function submit() {
      const button = $("[data-submit]", dialog);
      const errorBox = $("[data-dialog-error]", dialog);
      const p = state.pending;
      const body = p.mode === "set"
        ? { mode: "set", target_cents: p.cents, reason: p.reason }
        : { mode: "adjust", amount_cents: p.cents, reason: p.reason };
      setBusy(button, true, "Saving…");
      $("[data-back]", dialog).disabled = true;
      dialog.dataset.locked = "1";
      showError(errorBox, "");
      try {
        const result = await api("/api/admin/users/" + state.user.id + "/balance/", {
          method: "POST", body, headers: { "Idempotency-Key": state.idempotencyKey },
        });
        dialog.dataset.locked = "0";
        closeDialog(dialog);
        if (result.pending) {
          // At or above the approval threshold: nothing changed yet.
          toast("This change needs a second administrator. It is waiting on the Approvals page.", "success");
          return;
        }
        toast("Balance for " + result.user.username + " is now " + formatCents(result.resulting_cents) +
          " (" + formatCents(result.amount_cents, { signed: true }) + ").", "success");
        if (state.onDone) state.onDone(result);
      } catch (err) {
        // Only a definite rejection (4xx) gets a fresh key. Network / server failures keep the
        // same key, so retrying cannot apply the change twice if it did commit.
        if (err.status >= 400 && err.status < 500) state.idempotencyKey = L.randomKey();
        showError(errorBox, err.status === 409 || err.status === 422
          ? err.message + " Go back to edit the change."
          : err.message);
      } finally {
        dialog.dataset.locked = "0";
        $("[data-back]", dialog).disabled = false;
        setBusy(button, false, "Confirm change");
      }
    }

    function onSubmit(event) {
      event.preventDefault();
      if ($("[data-submit]", dialog).disabled) return;
      if (state.step === "edit") review();
      else submit();
    }

    return { init, open };
  })();

  function changeStatus(user, status, onDone) {
    const deactivate = status === "inactive";
    confirmAction({
      title: deactivate ? "Deactivate " + user.username + "?" : "Reactivate " + user.username + "?",
      message: deactivate
        ? "The account holder will be signed out and unable to sign in. Their balance (" + formatCents(user.balance_cents) +
          ") and activity history are kept and the account can be reactivated later."
        : "The account holder will be able to sign in again.",
      confirmLabel: deactivate ? "Deactivate" : "Reactivate",
      danger: deactivate,
      action: async () => {
        const updated = await api("/api/admin/users/" + user.id + "/status/", { method: "POST", body: { status } });
        toast(updated.username + " is now " + updated.status + ".", "success");
        onDone(updated);
      },
    });
  }

  // ---- overview page ---------------------------------------------------------------

  function initOverview() {
    const statsBox = $("[data-stats]");
    const tableBox = $("[data-users-table]");
    const searchInput = $("#user-search");
    let query = "";
    let page = 1;
    let seq = 0;

    async function loadStats() {
      statsBox.setAttribute("aria-busy", "true");
      try {
        const s = await api("/api/admin/stats/");
        $("[data-stat=total_users]").textContent = s.total_users.toLocaleString("en-US");
        $("[data-stat=active_users]").textContent = s.active_users.toLocaleString("en-US");
        $("[data-stat-meta=inactive_users]").textContent = s.inactive_users.toLocaleString("en-US") + " inactive";
        $("[data-stat=total_balance_display]").textContent = formatCents(s.total_balance_cents);
        $("[data-stat=recent_activity_count]").textContent = s.recent_activity_count.toLocaleString("en-US");
        $("[data-stat-meta=recent_window]").textContent = "Balance changes, last " + s.recent_activity_window_days + " days";
        showError($("[data-stats-error]"), "");
      } catch (err) {
        $all("[data-stat]").forEach((n) => { n.textContent = "—"; });
        showError($("[data-stats-error]"), "Statistics unavailable: " + err.message);
      } finally {
        statsBox.setAttribute("aria-busy", "false");
      }
    }

    const filterForm = $("[data-search-form]");
    const urlParams = new URLSearchParams(location.search);
    if (urlParams.get("sort")) filterForm.sort.value = urlParams.get("sort");
    const selected = new Set();
    const bulkBar = $("[data-bulk-bar]");

    function currentFilters() {
      return { q: query, status: filterForm.status.value, role: filterForm.role.value, sort: filterForm.sort.value };
    }

    function syncExport() {
      const link = $("[data-export-link]");
      const f = currentFilters();
      if (link) link.href = "/api/admin/users/export.csv" + L.qs(f);
    }

    function syncBulk() {
      bulkBar.hidden = selected.size === 0;
      $("[data-bulk-count]").textContent = String(selected.size);
      $all("[data-select-row]", tableBox).forEach((cb) => { cb.checked = selected.has(Number(cb.value)); });
      const all = $("[data-select-all]", tableBox);
      if (all) {
        const boxes = $all("[data-select-row]", tableBox);
        all.checked = boxes.length > 0 && boxes.every((b) => b.checked);
        all.indeterminate = !all.checked && boxes.some((b) => b.checked);
      }
    }

    async function loadUsers(p) {
      page = p || page;
      const mySeq = ++seq;
      syncExport();
      tableBox.setAttribute("aria-busy", "true");
      if (!tableBox.querySelector("table")) tableBox.replaceChildren(stateBlock("loading", "Loading accounts…"));
      else tableBox.classList.add("is-loading");
      try {
        const data = await api("/api/admin/users/" + L.qs({ ...currentFilters(), page, page_size: 20 }));
        if (mySeq !== seq) return;
        renderUsers(data);
      } catch (err) {
        if (mySeq !== seq) return;
        tableBox.replaceChildren(stateBlock("error", "Couldn’t load accounts", err.message,
          el("button", { type: "button", class: "btn btn--secondary btn--sm", onclick: () => loadUsers(page) }, "Try again")));
      } finally {
        if (mySeq === seq) { tableBox.setAttribute("aria-busy", "false"); tableBox.classList.remove("is-loading"); }
      }
    }

    function refreshAll() { loadStats(); loadUsers(page); }

    function actionsFor(u) {
      const isSelf = String(u.id) === String(config.currentAdminId);
      const buttons = [el("a", { class: "btn btn--secondary btn--xs", href: "/admin/users/" + u.id }, "View",
        el("span", { class: "visually-hidden", text: " " + u.username }))];
      if (u.role === "user") {
        buttons.push(
          el("button", { type: "button", class: "btn btn--secondary btn--xs", onclick: () => BalanceDialog.open(u.id, "set", refreshAll) }, "Set"),
          el("button", { type: "button", class: "btn btn--secondary btn--xs", onclick: () => BalanceDialog.open(u.id, "adjust", refreshAll) }, "Adjust"));
      }
      if (!isSelf) {
        buttons.push(u.status === "active"
          ? el("button", { type: "button", class: "btn btn--danger-outline btn--xs", onclick: () => changeStatus(u, "inactive", refreshAll) }, "Deactivate")
          : el("button", { type: "button", class: "btn btn--secondary btn--xs", onclick: () => changeStatus(u, "active", refreshAll) }, "Activate"));
      }
      return el("div", { class: "row-actions" }, buttons);
    }

    function renderUsers(data) {
      const f = currentFilters();
      if (!data.items.length) {
        const filtered = f.q || f.status || f.role;
        tableBox.replaceChildren(filtered
          ? stateBlock("empty", "No matching accounts", "Nothing matches these filters. Try a different search or clear the filters.")
          : stateBlock("empty", "No accounts yet", "Share the sign-up link or create the first account with “New account”."));
        return;
      }
      const rows = data.items.map((u) => el("tr", { class: u.status === "inactive" ? "is-muted" : null, dataset: { userId: String(u.id) } },
        el("td", { class: "select-cell" },
          String(u.id) === String(config.currentAdminId) ? null :
          el("input", { type: "checkbox", value: String(u.id), "data-select-row": true, "aria-label": "Select " + u.username,
            onchange: (e) => { e.target.checked ? selected.add(u.id) : selected.delete(u.id); syncBulk(); } })),
        el("td", { "data-label": "Account" },
          el("div", { class: "cell-stack" },
            el("span", null,
              el("a", { href: "/admin/users/" + u.id, class: "strong-link", text: u.username }),
              u.role === "admin" ? el("span", { class: "badge badge--role", text: "Admin" }) : null),
            u.display_name ? el("small", { class: "muted", text: u.display_name }) : null)),
        el("td", { "data-label": "ID", class: "mono" }, "#" + u.id),
        el("td", { "data-label": "Status" }, el("span", { class: "badge badge--" + u.status, text: u.status === "active" ? "Active" : "Inactive" })),
        el("td", { "data-label": "Balance", class: "num strong" }, u.role === "user" ? formatCents(u.balance_cents) : el("span", { class: "muted", text: "—" })),
        el("td", { "data-label": "Joined" }, timeEl(u.created_at, "date"),
          u.signup_source === "self" ? el("small", { class: "muted block", text: "Self sign-up" }) : null),
        el("td", { "data-label": "Last sign-in" }, u.last_login_at
          ? el("span", { title: L.formatDateTime(u.last_login_at), text: L.relativeTime(u.last_login_at) })
          : el("span", { class: "muted", text: "Never" })),
        el("td", { "data-label": "Actions", class: "actions-cell" }, actionsFor(u))));
      const table = el("div", { class: "table-wrap" },
        el("table", { class: "table table--stack" },
          el("caption", { class: "visually-hidden", text: "Accounts" }),
          el("thead", null, el("tr", null,
            el("th", { scope: "col", class: "select-cell" },
              el("input", { type: "checkbox", "data-select-all": true, "aria-label": "Select all on this page",
                onchange: (e) => {
                  $all("[data-select-row]", tableBox).forEach((cb) => { e.target.checked ? selected.add(Number(cb.value)) : selected.delete(Number(cb.value)); });
                  syncBulk();
                } })),
            ["Account", "ID", "Status", "Balance", "Joined", "Last sign-in", "Actions"].map((h) =>
              el("th", { scope: "col", class: h === "Balance" ? "num" : null, text: h })))),
          el("tbody", null, rows)));
      tableBox.replaceChildren(table, pagination(data, loadUsers, "Account pages") || "");
      syncBulk();
    }

    searchInput.addEventListener("input", L.debounce(() => { query = searchInput.value.trim(); loadUsers(1); }, 250));
    filterForm.addEventListener("change", (e) => { if (e.target !== searchInput) loadUsers(1); });
    filterForm.addEventListener("submit", (e) => { e.preventDefault(); query = searchInput.value.trim(); loadUsers(1); });

    // ---- bulk actions
    $("[data-bulk-clear]").addEventListener("click", () => { selected.clear(); syncBulk(); });
    function reportBulk(result, verb) {
      const failed = result.results.filter((r) => !r.ok);
      const queued = result.results.filter((r) => r.pending).length;
      toast((result.succeeded - queued) + " account(s) " + verb + "." +
        (queued ? " " + queued + " waiting for approval." : "") +
        (failed.length ? " " + failed.length + " skipped: " + failed[0].error : ""),
      failed.length ? "error" : "success");
    }
    $all("[data-bulk]").forEach((btn) => btn.addEventListener("click", () => {
      const ids = Array.from(selected);
      const action = btn.dataset.bulk;
      if (action === "adjust") { openBulkAdjust(ids); return; }
      confirmAction({
        title: (action === "activate" ? "Activate " : "Deactivate ") + ids.length + " account(s)?",
        message: action === "activate"
          ? "The selected accounts will be able to sign in again."
          : "The selected accounts will be signed out and unable to sign in. Balances and history are kept.",
        confirmLabel: action === "activate" ? "Activate" : "Deactivate",
        danger: action === "deactivate",
        action: async () => {
          const res = await api("/api/admin/users/bulk/", { method: "POST", body: { user_ids: ids, action } });
          reportBulk(res, action === "activate" ? "activated" : "deactivated");
          selected.clear();
          refreshAll();
        },
      });
    }));

    const bulkDialog = $("#bulk-dialog");
    const bulkForm = $("[data-bulk-form]", bulkDialog);
    let bulkIds = [];
    function openBulkAdjust(ids) {
      bulkIds = ids;
      bulkForm.reset();
      fieldErrors(bulkForm, {});
      showError($("[data-dialog-error]", bulkDialog), "");
      $("[data-bulk-results]", bulkDialog).hidden = true;
      $("[data-bulk-dialog-count]", bulkDialog).textContent = String(ids.length);
      $("[data-submit]", bulkDialog).hidden = false;
      openDialog(bulkDialog);
    }
    bulkForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const button = $("[data-submit]", bulkDialog);
      if (button.disabled) return;
      const parsed = parseAmountToCents(bulkForm.amount.value);
      const errors = {};
      if (!parsed.ok) errors.amount = parsed.error;
      else if (parsed.cents === 0) errors.amount = "Adjustment must not be zero.";
      fieldErrors(bulkForm, errors);
      if (Object.keys(errors).length) return;
      setBusy(button, true, "Applying…");
      bulkDialog.dataset.locked = "1";
      try {
        const res = await api("/api/admin/users/bulk/", {
          method: "POST",
          body: { user_ids: bulkIds, action: "adjust", amount_cents: parsed.cents, reason: bulkForm.reason.value.trim() || null },
        });
        const box = $("[data-bulk-results]", bulkDialog);
        box.replaceChildren(
          el("p", { class: "strong", text: res.succeeded + " updated · " + res.failed + " skipped" }),
          el("ul", { class: "bulk-results__list" }, res.results.map((r) => el("li", { class: r.ok ? "ok" : "fail" },
            r.pending ? (r.username + ": waiting for approval")
              : r.ok ? (r.username + " → " + formatCents(r.resulting_cents)) : ("#" + r.user_id + ": " + r.error)))));
        box.hidden = false;
        button.hidden = true;
        reportBulk(res, "adjusted");
        selected.clear();
        refreshAll();
      } catch (err) {
        showError($("[data-dialog-error]", bulkDialog), err.message);
      } finally {
        bulkDialog.dataset.locked = "0";
        setBusy(button, false, "Apply to selected");
      }
    });

    initCreateDialog((created) => {
      query = "";
      searchInput.value = "";
      filterForm.status.value = "";
      filterForm.role.value = "";
      filterForm.sort.value = "newest";
      loadStats();
      loadUsers(1).then(() => {
        const row = tableBox.querySelector('tr[data-user-id="' + created.id + '"]');
        if (row) row.classList.add("is-new");
      });
    });
    if (urlParams.get("new")) $("[data-open-create]").click();

    loadStats();
    loadUsers(1);
  }

  function initCreateDialog(onCreated) {
    const dialog = $("#create-dialog");
    const form = $("[data-create-form]", dialog);
    const submitBtn = $("[data-submit]", dialog);
    const errorBox = $("[data-dialog-error]", dialog);
    let step = "edit";

    function showStep(next) {
      step = next;
      ["edit", "confirm", "done"].forEach((s) => { $("[data-step=" + s + "]", dialog).hidden = s !== next; });
      $("[data-back]", dialog).hidden = next !== "confirm";
      $("[data-cancel]", dialog).hidden = next !== "edit";
      $("[data-label]", submitBtn).textContent = next === "edit" ? "Review" : next === "confirm" ? "Create account" : "Done";
      showError(errorBox, "");
    }

    function syncPwMode() {
      const manual = form.pw_mode.value === "manual";
      $("[data-manual-password]", dialog).hidden = !manual;
      if (!manual) form.password.value = "";
    }

    $("[data-open-create]").addEventListener("click", () => {
      form.reset();
      syncPwMode();
      fieldErrors(form, {});
      showStep("edit");
      openDialog(dialog);
    });
    form.addEventListener("change", (e) => { if (e.target.name === "pw_mode") syncPwMode(); });
    form.addEventListener("input", () => { fieldErrors(form, {}); showError(errorBox, ""); });
    $("[data-back]", dialog).addEventListener("click", () => showStep("edit"));
    $("[data-copy-password]", dialog).addEventListener("click", async () => {
      const value = $("[data-done-password]", dialog).textContent;
      try { await navigator.clipboard.writeText(value); toast("Temporary password copied.", "success"); }
      catch (_) { toast("Copy failed — select the password and copy it manually.", "error"); }
    });
    dialog.addEventListener("close", () => {
      // Never keep a one-time password in the DOM after the dialog closes.
      $("[data-done-password]", dialog).textContent = "";
      form.password.value = "";
    });

    function validate() {
      const errors = {};
      const username = form.username.value.trim().toLowerCase();
      if (!/^[a-z0-9](?:[a-z0-9._-]{1,30})[a-z0-9]$/.test(username)) {
        errors.username = "Use 3–32 letters, numbers, “.”, “_” or “-”, starting and ending with a letter or number.";
      }
      if (form.pw_mode.value === "manual") {
        const pw = form.password.value;
        const bytes = new TextEncoder().encode(pw).length;
        if (pw.length < 8) errors.password = "Password must be at least 8 characters.";
        else if (bytes > 72) errors.password = "Password must be at most 72 bytes.";
        else if (pw.trim() !== pw) errors.password = "Password must not start or end with a space.";
      }
      fieldErrors(form, errors);
      if (errors.username) form.username.focus();
      else if (errors.password) form.password.focus();
      return Object.keys(errors).length === 0 ? username : null;
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (submitBtn.disabled) return;
      if (step === "done") { closeDialog(dialog); return; }
      if (step === "edit") {
        const username = validate();
        if (!username) return;
        $("[data-review-username]", dialog).textContent = username;
        $("[data-review-password]", dialog).textContent = form.pw_mode.value === "manual"
          ? "Entered manually (" + form.password.value.length + " characters)"
          : "Generated securely, shown once after creation";
        showStep("confirm");
        submitBtn.focus();
        return;
      }
      const generate = form.pw_mode.value === "generate";
      const display = form.display_name.value.trim() || null;
      const body = generate
        ? { username: form.username.value.trim(), display_name: display, generate_password: true }
        : { username: form.username.value.trim(), display_name: display, password: form.password.value };
      setBusy(submitBtn, true, "Creating…");
      $("[data-back]", dialog).disabled = true;
      dialog.dataset.locked = "1";
      try {
        const result = await api("/api/admin/users/", { method: "POST", body });
        $("[data-done-username]", dialog).textContent = result.user.username;
        $("[data-done-id]", dialog).textContent = "#" + result.user.id;
        $("[data-done-password-block]", dialog).hidden = !result.temporary_password;
        $("[data-done-password]", dialog).textContent = result.temporary_password || "";
        form.password.value = "";
        showStep("done");
        submitBtn.focus();
        onCreated(result.user);
      } catch (err) {
        if (err.status === 409 || err.status === 422) {
          showStep("edit");
          const field = err.code === "username_taken" ? "username"
            : (err.details && err.details[0] && err.details[0].field) || null;
          if (field === "username" || field === "password") {
            fieldErrors(form, { [field]: err.message });
            form[field].focus();
          } else showError(errorBox, err.message);
        } else {
          showError(errorBox, err.message);
        }
      } finally {
        dialog.dataset.locked = "0";
        $("[data-back]", dialog).disabled = false;
        setBusy(submitBtn, false);
      }
    });
  }

  // ---- user detail page ------------------------------------------------------------

  function initDetail() {
    const userId = config.userId;
    const activity = ActivityTable($("[data-activity]"));
    activity.load(1);

    function applyUser(u) {
      $("[data-balance]").textContent = formatCents(u.balance_cents);
      $all("[data-balance-updated], [data-updated-at]").forEach((t) => { t.dataset.ts = u.updated_at; });
      hydrateTimes(document);
      $all("[data-status-badge]").forEach((b) => {
        b.className = "badge badge--" + u.status;
        b.textContent = u.status === "active" ? "Active" : "Inactive";
      });
      const deact = $("[data-status-action=inactive]");
      const react = $("[data-status-action=active]");
      if (deact) deact.hidden = u.status !== "active";
      if (react) react.hidden = u.status === "active";
    }

    async function reloadUser() {
      try {
        const data = await api("/api/admin/users/" + userId + "/?page_size=1");
        applyUser(data.user);
        return data.user;
      } catch (err) {
        toast(err.message, "error");
        return null;
      }
    }

    $all("[data-balance-action]").forEach((button) => {
      button.addEventListener("click", () => BalanceDialog.open(userId, button.dataset.balanceAction, (result) => {
        applyUser(result.user); // server-confirmed values only
        activity.reset();
      }));
    });

    $all("[data-status-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const user = await reloadUser();
        if (user) changeStatus(user, button.dataset.statusAction, applyUser);
      });
    });

    // ---- password reset
    const resetBtn = $("[data-reset-password]");
    if (resetBtn) {
      resetBtn.addEventListener("click", () => confirmAction({
        title: "Reset this password?",
        message: "A new temporary password will be generated and shown once. The account holder will be signed out everywhere.",
        confirmLabel: "Reset password",
        danger: true,
        action: async () => {
          const res = await api("/api/admin/users/" + userId + "/password-reset/", { method: "POST" });
          const dialog = $("#reset-dialog");
          $("[data-reset-value]", dialog).textContent = res.temporary_password;
          dialog.addEventListener("close", () => { $("[data-reset-value]", dialog).textContent = ""; }, { once: true });
          setTimeout(() => openDialog(dialog), 0);
        },
      }));
    }

    // ---- two-step sign-in reset (administrator accounts)
    const totpBtn = $("[data-reset-totp]");
    if (totpBtn) {
      totpBtn.addEventListener("click", () => confirmAction({
        title: "Reset two-step sign-in?",
        message: "Two-step sign-in will be turned off for this administrator and they will be signed out everywhere. They can set it up again from My security.",
        confirmLabel: "Reset",
        danger: true,
        action: async () => {
          await api("/api/admin/users/" + userId + "/totp-reset/", { method: "POST" });
          toast("Two-step sign-in was reset.", "success");
          totpBtn.remove();
          const state = $("[data-totp-state]");
          if (state) state.textContent = "Off";
        },
      }));
    }

    // ---- internal notes
    const notesList = $("[data-notes]");
    const noteForm = $("[data-note-form]");
    async function loadNotes() {
      try {
        const notes = await api("/api/admin/users/" + userId + "/notes/");
        if (!notes.length) {
          notesList.replaceChildren(el("li", { class: "notes__empty", text: "No notes yet." }));
          return;
        }
        notesList.replaceChildren(...notes.map((n) => el("li", { class: "note" },
          el("p", { class: "note__body", text: n.body }),
          el("p", { class: "note__meta" },
            (n.author_username || "Unknown") + " · ", L.timeEl(n.created_at),
            String(n.author_id) === String(config.currentAdminId)
              ? el("button", { type: "button", class: "link-btn", onclick: () => deleteNote(n.id) }, "Delete")
              : null))));
      } catch (err) {
        notesList.replaceChildren(el("li", { class: "notes__empty", text: "Couldn’t load notes: " + err.message }));
      }
    }
    async function deleteNote(id) {
      try {
        await api("/api/admin/users/" + userId + "/notes/" + id + "/", { method: "DELETE" });
        loadNotes();
      } catch (err) { toast(err.message, "error"); }
    }
    noteForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const button = $("[data-submit]", noteForm);
      const body = noteForm.body.value.trim();
      if (!body || button.disabled) return;
      setBusy(button, true, "Saving…");
      try {
        await api("/api/admin/users/" + userId + "/notes/", { method: "POST", body: { body } });
        noteForm.reset();
        loadNotes();
      } catch (err) { toast(err.message, "error"); }
      finally { setBusy(button, false, "Add note"); }
    });
    loadNotes();
  }

  document.addEventListener("DOMContentLoaded", () => {
    const root = $("[data-admin-page]");
    if (!root) return;
    config = {
      page: root.dataset.adminPage,
      userId: root.dataset.userId,
      currentAdminId: root.dataset.currentAdminId,
      allowNegative: root.dataset.allowNegative === "1",
      maxCents: Number(root.dataset.maxCents) || L.MAX_AMOUNT_CENTS,
    };
    BalanceDialog.init();
    if (config.page === "overview") initOverview();
    else if (config.page === "detail") initDetail();
  });
})();
