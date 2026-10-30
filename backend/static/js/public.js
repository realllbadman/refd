/* Public forms: sign-up checks, password strength, contact form busy state.
 * Usability only — the server validates everything again. */
(function () {
  "use strict";
  const { $, setBusy, fieldErrors } = window.Ledger;

  function strength(pw) {
    let score = 0;
    if (pw.length >= 8) score++;
    if (pw.length >= 12) score++;
    if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score++;
    if (/\d/.test(pw)) score++;
    if (/[^A-Za-z0-9]/.test(pw)) score++;
    return Math.min(4, score);
  }
  const LABELS = ["Too short", "Weak", "Fair", "Good", "Strong"];

  function initRegister(form) {
    const pw = form.password;
    const bar = $("[data-strength-bar]", form);
    const label = $("[data-strength-label]", form);
    pw.addEventListener("input", () => {
      const s = pw.value ? strength(pw.value) : 0;
      bar.dataset.level = String(s);
      label.textContent = pw.value ? "Strength: " + LABELS[s] + (pw.value.length < 8 ? " — at least 8 characters" : "") : "At least 8 characters.";
    });
    form.addEventListener("submit", (e) => {
      const errors = {};
      const username = form.username.value.trim().toLowerCase();
      if (!/^[a-z0-9](?:[a-z0-9._-]{1,30})[a-z0-9]$/.test(username)) {
        errors.username = "Use 3–32 letters, numbers, “.”, “_” or “-”, starting and ending with a letter or number.";
      }
      const bytes = new TextEncoder().encode(pw.value).length;
      if (pw.value.length < 8) errors.password = "Use at least 8 characters.";
      else if (bytes > 72) errors.password = "Use at most 72 bytes.";
      else if (pw.value.trim() !== pw.value) errors.password = "Don’t start or end with a space.";
      if (pw.value !== form.confirm_password.value) errors.confirm_password = "Passwords do not match.";
      if (!form.accept_terms.checked) errors.accept_terms = "Please confirm to continue.";
      fieldErrors(form, errors);
      if (Object.keys(errors).length) {
        e.preventDefault();
        const first = ["username", "password", "confirm_password", "accept_terms"].find((k) => errors[k]);
        form[first].focus();
        return;
      }
      setBusy($("[data-submit]", form), true, "Creating account…");
    });
  }

  function initContact(form) {
    form.addEventListener("submit", (e) => {
      const button = $("[data-submit]", form);
      if (button.disabled) { e.preventDefault(); return; }
      setBusy(button, true, "Sending…");
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const register = $("[data-register-form]");
    if (register) initRegister(register);
    const contact = $("[data-contact-form]");
    if (contact) initContact(contact);
    window.addEventListener("pageshow", () => {
      document.querySelectorAll("[data-submit]").forEach((b) => setBusy(b, false));
    });
  });
})();
