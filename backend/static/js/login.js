/* Login form: light client-side checks and a busy state. The server validates everything. */
(function () {
  "use strict";
  const { $, setBusy, fieldErrors } = window.Ledger;

  document.addEventListener("DOMContentLoaded", () => {
    const form = $("[data-login-form]");
    if (!form) return;
    form.addEventListener("submit", (event) => {
      const errors = {};
      if (!form.username.value.trim()) errors.username = "Enter your username.";
      if (!form.password.value) errors.password = "Enter your password.";
      fieldErrors(form, errors);
      if (Object.keys(errors).length) {
        event.preventDefault();
        form[errors.username ? "username" : "password"].focus();
        return;
      }
      const button = $("[data-submit]", form);
      if (button.disabled) { event.preventDefault(); return; }
      setBusy(button, true, "Signing in…");
    });
    // Restore the button if the page is shown again from the back/forward cache.
    window.addEventListener("pageshow", () => setBusy($("[data-submit]", form), false, "Sign in"));
  });
})();
