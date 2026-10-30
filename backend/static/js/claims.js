/* Account-holder refund request submission and tracking. */
(function () {
  "use strict";
  const L = window.Ledger;
  const { el, $, api, toast, setBusy, fieldErrors, formatCents, formatDate, timeEl, stateBlock } = L;

  const STATUS = {
    submitted: "Submitted",
    under_review: "Under review",
    awaiting_customer: "Action needed",
    approved: "Approved",
    declined: "Declined",
    cancelled: "Cancelled",
  };
  const REASON = {
    not_received: "Order not received",
    damaged: "Item arrived damaged",
    incorrect_item: "Incorrect item",
    duplicate_charge: "Duplicate charge",
    cancelled: "Order cancelled",
    other: "Other",
  };

  const NEXT_STEP = {
    submitted: "The review team will look at your claim soon. You can add documents or details while you wait.",
    under_review: "The team is reviewing your claim. We’ll post an update here.",
    awaiting_customer: "The team needs something from you. Read their latest reply, then send an update or edit your claim details.",
    approved: "Your claim was approved.",
    declined: "Your claim was declined. Read the team’s reply for the reason. Contact support if you have questions.",
    cancelled: "This claim is closed. You can start a new claim at any time.",
  };
  const MAX_FILES = 5;
  let claimsCache = [];
  let seenSent = false;

  function eventTitle(event) {
    if (event.event_type === "submitted") return "Claim submitted";
    if (event.event_type === "status_change") return (STATUS[event.from_status] || "Status") + " → " + (STATUS[event.to_status] || "Updated");
    if (event.event_type === "admin_reply") return "Reply from the team";
    if (event.event_type === "evidence_added") return "Document added";
    return "You sent an update";
  }

  /** Submitted → Under review → (Action needed) → Decision. */
  function progress(claim) {
    const decided = ["approved", "declined", "cancelled"].includes(claim.status);
    const reached = (status) => claim.events.some((e) => e.to_status === status) || claim.status === status;
    const steps = [
      { label: "Submitted", done: true },
      { label: "Under review", done: reached("under_review") || reached("awaiting_customer") || decided, current: claim.status === "under_review" },
    ];
    if (reached("awaiting_customer")) steps.push({ label: "Action needed", done: claim.status !== "awaiting_customer", current: claim.status === "awaiting_customer" });
    steps.push({ label: decided ? STATUS[claim.status] : "Decision", done: decided, current: decided });
    if (claim.status === "submitted") steps[0].current = true;
    return el("ol", { class: "claim-progress", "aria-label": "Claim progress" }, steps.map((step) => el("li", {
      class: "claim-progress__step" + (step.done ? " is-done" : "") + (step.current ? " is-current" : ""),
      "aria-current": step.current ? "step" : null,
    }, el("span", { class: "claim-progress__dot", "aria-hidden": "true" }), el("span", { text: step.label }))));
  }

  async function uploadFiles(claimId, files) {
    const failures = [];
    for (const file of files) {
      const data = new FormData();
      data.append("file", file);
      try { await api("/api/user/claims/" + claimId + "/evidence/", { method: "POST", body: data }); }
      catch (err) { failures.push(file.name + ": " + err.message); }
    }
    return failures;
  }

  function checkFiles(files, existing) {
    if (files.length + (existing || 0) > MAX_FILES) return "A claim can have at most " + MAX_FILES + " documents.";
    const tooBig = files.find((f) => f.size > 8 * 1024 * 1024);
    return tooBig ? tooBig.name + " is larger than 8 MB." : "";
  }

  function markSeen(claims) {
    if (seenSent || !claims.some((c) => c.has_update)) return;
    seenSent = true;
    api("/api/user/claims/seen/", { method: "POST" }).then(() => {
      const badge = document.querySelector('.sidebar__link[href="/dashboard/claims"] .sidebar__badge');
      if (badge) badge.remove();
    }).catch(() => { seenSent = false; });
  }

  function applyFilters(claims) {
    const status = $(`[data-user-claims-status]`).value;
    const query = $(`[data-user-claims-search]`).value.trim().toLowerCase();
    return claims.filter((claim) => {
      const isClosed = ["approved", "declined", "cancelled"].includes(claim.status);
      if (status === "open" && isClosed) return false;
      if (status && status !== "open" && claim.status !== status) return false;
      const text = [claim.merchant, claim.order_reference, claim.description, REASON[claim.reason], STATUS[claim.status]].join(" ").toLowerCase();
      return !query || text.includes(query);
    });
  }

  async function loadClaims() {
    const list = $(`[data-claim-list]`);
    list.replaceChildren(stateBlock("loading", "Loading claims…"));
    try {
      const claims = await api("/api/user/claims/");
      // Keep "new update" highlights for this visit even after they're marked seen.
      const previouslyNew = new Set(claimsCache.filter((c) => c.has_update).map((c) => c.id));
      claims.forEach((c) => { if (previouslyNew.has(c.id)) c.has_update = true; });
      claimsCache = claims;
      markSeen(claims);
      if (!claims.length) {
        list.replaceChildren(stateBlock("empty", "No claims yet", "Your submitted requests and their updates will appear here."));
        return;
      }
      const filtered = applyFilters(claims);
      if (!filtered.length) {
        list.replaceChildren(stateBlock("empty", "No matching claims", "Change the status filter or search phrase."));
        return;
      }
      list.replaceChildren(...filtered.map(renderClaim));
      const target = location.hash && document.getElementById(location.hash.slice(1));
      if (target) { target.classList.add("is-highlighted"); target.scrollIntoView({ block: "start" }); }
    } catch (err) {
      list.replaceChildren(stateBlock("error", "Couldn’t load claims", err.message,
        el("button", { type: "button", class: "btn btn--secondary btn--sm", onclick: loadClaims }, "Try again")));
    }
  }

  function renderClaim(claim) {
    const closed = ["approved", "declined", "cancelled"].includes(claim.status);
    const timeline = el("ol", { class: "claim-timeline" }, claim.events.map((event) => el("li", { class: "claim-timeline__event" },
      el("span", { class: "claim-timeline__dot", "aria-hidden": "true" }),
      el("div", { class: "claim-timeline__content" },
        el("div", { class: "claim-timeline__head" },
          el("strong", { text: eventTitle(event) }),
          el("span", { class: "muted small" }, timeEl(event.created_at))),
        event.message ? el("p", { text: event.message }) : null))));
    const evidence = claim.evidence.length
      ? el("div", { class: "claim-evidence" },
        el("h4", { class: "claim-section-heading", text: "Supporting documents" }),
        el("ul", { class: "claim-evidence__list" }, claim.evidence.map((item) => el("li", null,
          el("a", { href: item.download_path, text: item.original_filename, download: true }),
          el("span", { class: "muted small", text: Math.max(1, Math.round(item.size_bytes / 1024)) + " KB" })))))
      : null;
    const credited = claim.credit_cents !== null && claim.credit_cents !== undefined;
    const card = el("article", { class: "claim-card" + (claim.has_update ? " has-update" : ""), id: "claim-" + claim.id },
      el("header", { class: "claim-card__header" },
        el("div", null,
          el("h3", { text: claim.merchant }),
          el("p", { class: "muted small", text: "Claim #" + claim.id + " · Order " + claim.order_reference })),
        el("div", { class: "claim-card__status" },
          claim.has_update ? el("span", { class: "chip chip--set", text: "New update" }) : null,
          el("span", { class: "claim-state claim-state--" + claim.status, text: STATUS[claim.status] || claim.status }))),
      progress(claim),
      el("p", { class: "claim-next" + (claim.status === "awaiting_customer" ? " claim-next--action" : "") },
        NEXT_STEP[claim.status] || "",
        credited ? el("span", null, " ", el("strong", { text: formatCents(claim.credit_cents) + " was credited to your balance." }), " ",
          el("a", { href: "/dashboard/activity", text: "See activity" })) : null),
      el("dl", { class: "claim-card__facts" },
        el("div", null, el("dt", { text: "Amount" }), el("dd", { text: formatCents(claim.amount_cents) })),
        el("div", null, el("dt", { text: "Purchase date" }), el("dd", { text: formatDate(claim.purchase_date + "T12:00:00") })),
        el("div", null, el("dt", { text: "Reason" }), el("dd", { text: REASON[claim.reason] || claim.reason }))),
      el("p", { class: "claim-card__description", text: claim.description }),
      el("h4", { class: "claim-section-heading", text: "Claim activity" }), timeline);

    if (!closed) {
      const fileInput = el("input", { type: "file", class: "visually-hidden", multiple: true,
        accept: "application/pdf,image/jpeg,image/png,image/webp", "aria-label": "Add documents to this claim" });
      const addFiles = el("button", { type: "button", class: "btn btn--secondary btn--xs", onclick: () => fileInput.click(),
        disabled: claim.evidence.length >= MAX_FILES }, "Add documents");
      fileInput.addEventListener("change", async () => {
        const files = Array.from(fileInput.files);
        if (!files.length) return;
        const problem = checkFiles(files, claim.evidence.length);
        if (problem) { toast(problem, "error"); fileInput.value = ""; return; }
        setBusy(addFiles, true);
        const failures = await uploadFiles(claim.id, files);
        setBusy(addFiles, false);
        toast(failures.length ? "Some files weren’t added. " + failures.join(" ") : "Documents added.", failures.length ? "error" : "success");
        loadClaims();
      });
      card.append(el("div", { class: "claim-card__tools" }, addFiles, fileInput,
        claim.status === "awaiting_customer"
          ? el("button", { type: "button", class: "btn btn--secondary btn--xs", onclick: () => openEdit(card, claim) }, "Edit claim details")
          : null));
      const message = el("textarea", { class: "input textarea", rows: "3", maxlength: "4000", required: true,
        "aria-label": "Message about this claim", placeholder: "Add details or reply to the team" });
      const messageForm = el("form", { class: "claim-reply" }, message,
        el("button", { type: "submit", class: "btn btn--secondary btn--sm" }, "Send update"));
      messageForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!message.value.trim()) return;
        const button = messageForm.querySelector("button");
        setBusy(button, true, "Sending…");
        try {
          await api("/api/user/claims/" + claim.id + "/messages/", { method: "POST", body: { message: message.value } });
          toast("Update sent.", "success");
          await loadClaims();
        } catch (err) { toast(err.message, "error"); }
        finally { setBusy(button, false, "Send update"); }
      });
      card.append(messageForm,
        el("button", { type: "button", class: "btn btn--danger-outline btn--xs", onclick: async () => {
          if (!window.confirm("Cancel this claim? This cannot be undone.")) return;
          try {
            await api("/api/user/claims/" + claim.id + "/cancel/", { method: "POST", body: {} });
            toast("Claim cancelled.", "success");
            loadClaims();
          } catch (err) { toast(err.message, "error"); }
        } }, "Cancel claim"));
    }
    return card;
  }

  function openEdit(card, claim) {
    if (card.querySelector(".claim-edit")) return;
    const field = (label, input) => el("label", { class: "field" }, el("span", { class: "field__label", text: label }), input);
    const merchant = el("input", { class: "input", name: "merchant", maxlength: "120", value: claim.merchant });
    const order = el("input", { class: "input", name: "order_reference", maxlength: "80", value: claim.order_reference });
    const date = el("input", { class: "input", name: "purchase_date", type: "date", value: claim.purchase_date });
    const amount = el("input", { class: "input", name: "amount", inputmode: "decimal", value: (claim.amount_cents / 100).toFixed(2) });
    const reason = el("select", { class: "input", name: "reason" },
      Object.entries(REASON).map(([value, text]) => el("option", { value, text, selected: value === claim.reason })));
    const description = el("textarea", { class: "input textarea", name: "description", rows: "4", maxlength: "4000" });
    description.value = claim.description;
    const save = el("button", { type: "submit", class: "btn btn--primary btn--sm" },
      el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { "data-label": true, text: "Save changes" }));
    const form = el("form", { class: "form claim-edit" },
      el("h4", { class: "claim-section-heading", text: "Edit claim details" }),
      field("Merchant or seller", merchant),
      el("div", { class: "form-row" }, field("Order reference", order), field("Purchase date", date)),
      el("div", { class: "form-row" }, field("Purchase amount (USD)", amount), field("Reason", reason)),
      field("What happened", description),
      el("div", { class: "button-row" }, save,
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => form.remove() }, "Cancel")));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const parsed = L.parseAmountToCents(amount.value, { allowSign: false });
      if (!parsed.ok) { toast(parsed.error, "error"); amount.focus(); return; }
      setBusy(save, true, "Saving…");
      try {
        await api("/api/user/claims/" + claim.id + "/", { method: "POST", body: {
          merchant: merchant.value, order_reference: order.value, purchase_date: date.value,
          amount_cents: parsed.cents, reason: reason.value, description: description.value,
        } });
        toast("Claim details updated. The team can see what changed.", "success");
        loadClaims();
      } catch (err) { toast(err.message, "error"); }
      finally { setBusy(save, false, "Save changes"); }
    });
    card.querySelector(".claim-card__tools").after(form);
    merchant.focus();
  }

  function init() {
    const form = $(`[data-claim-form]`);
    $(`[data-user-claims-status]`).addEventListener("change", loadClaims);
    $(`[data-user-claims-search]`).addEventListener("input", L.debounce(loadClaims, 200));
    const submit = $(`[data-submit]`, form);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const errors = {};
      const amount = L.parseAmountToCents(form.amount.value, { allowSign: false });
      if (!form.merchant.value.trim()) errors.merchant = "Enter the merchant or seller.";
      if (!form.order_reference.value.trim()) errors.order_reference = "Enter the order reference.";
      if (!form.purchase_date.value) errors.purchase_date = "Choose the purchase date.";
      if (!amount.ok) errors.amount = amount.error;
      if (!form.reason.value) errors.reason = "Choose a reason.";
      if (!form.description.value.trim()) errors.description = "Describe what happened.";
      const files = Array.from(form.files.files || []);
      const fileProblem = checkFiles(files, 0);
      if (fileProblem) errors.files = fileProblem;
      fieldErrors(form, errors);
      if (Object.keys(errors).length) return;
      setBusy(submit, true, "Submitting…");
      try {
        const created = await api("/api/user/claims/", { method: "POST", body: {
          merchant: form.merchant.value,
          order_reference: form.order_reference.value,
          purchase_date: form.purchase_date.value,
          amount_cents: amount.cents,
          reason: form.reason.value,
          description: form.description.value,
          contact_email: form.contact_email.value.trim() || null,
        } });
        const failures = files.length ? await uploadFiles(created.id, files) : [];
        form.reset();
        fieldErrors(form, {});
        toast(failures.length
          ? "Claim submitted, but some files weren’t added: " + failures.join(" ") + " You can add them from the claim."
          : "Claim submitted.", failures.length ? "error" : "success");
        await loadClaims();
      } catch (err) {
        const errorsFromServer = {};
        (err.details || []).forEach((detail) => { if (detail.field) errorsFromServer[detail.field] = detail.message || err.message; });
        fieldErrors(form, errorsFromServer);
        toast(err.message, "error");
      } finally { setBusy(submit, false, "Submit claim"); }
    });
    loadClaims();
  }

  document.addEventListener("DOMContentLoaded", () => {
    if ($(`[data-user-page="claims"]`)) init();
  });
})();