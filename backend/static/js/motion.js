/* RefundClaimers — motion layer.
 * Scroll-triggered reveals and a smooth FAQ accordion. Pure enhancement:
 * if JS is off or reduced-motion is on, everything is fully visible and usable. */
(function () {
  "use strict";
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  // Only arm the reveal styles when we can actually animate; otherwise content
  // stays visible via its normal styles (the CSS is gated on this class).
  if (!reduce && typeof IntersectionObserver === "function") {
    document.documentElement.classList.add("reveal-on");
  }

  function reveals() {
    const items = Array.prototype.slice.call(document.querySelectorAll("[data-reveal]"));
    if (!items.length) return;
    if (reduce || typeof IntersectionObserver !== "function") {
      items.forEach((el) => el.classList.add("is-revealed"));
      return;
    }
    // Stagger items that share a data-reveal-group so a row animates in sequence.
    const groups = {};
    items.forEach((el) => {
      const g = el.getAttribute("data-reveal-group");
      if (!g) return;
      groups[g] = groups[g] || 0;
      el.style.setProperty("--reveal-delay", groups[g] * 70 + "ms");
      groups[g] += 1;
    });
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-revealed");
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });
    items.forEach((el) => io.observe(el));
  }

  function accordion() {
    document.querySelectorAll(".faq__item").forEach((item) => {
      const summary = item.querySelector("summary");
      const panel = item.querySelector(".faq__panel");
      if (!summary || !panel || reduce) return;
      summary.addEventListener("click", (event) => {
        event.preventDefault();
        if (item.open) {
          panel.style.height = panel.scrollHeight + "px";
          requestAnimationFrame(() => { panel.style.height = "0px"; });
          panel.addEventListener("transitionend", function end() {
            item.open = false;
            panel.style.height = "";
            panel.removeEventListener("transitionend", end);
          }, { once: true });
        } else {
          item.open = true;
          const target = panel.scrollHeight;
          panel.style.height = "0px";
          requestAnimationFrame(() => { panel.style.height = target + "px"; });
          panel.addEventListener("transitionend", function end() {
            panel.style.height = "";
            panel.removeEventListener("transitionend", end);
          }, { once: true });
        }
      });
    });
  }

  function run() { reveals(); accordion(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
  else run();
})();
