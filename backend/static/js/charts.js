/* Lightweight SVG charts (no external libraries; CSP-safe).
 * - columns(): one bar per category, grows from a zero baseline (handles negatives).
 * - line():    a single series with an area wash, crosshair and end label.
 * Both add a hover tooltip and a "View as table" fallback for accessibility. */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const { el } = window.Ledger;

  function svg(tag, attrs) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
    return node;
  }

  function niceStep(range, target) {
    const raw = range / Math.max(1, target);
    const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const norm = raw / mag;
    return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  }

  function ticks(min, max, count) {
    if (min === max) { max = min + 1; }
    const step = niceStep(max - min, count);
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const out = [];
    for (let v = lo; v <= hi + step / 2; v += step) out.push(Math.round(v * 1e6) / 1e6);
    return out;
  }

  function compact(n) {
    const abs = Math.abs(n);
    if (abs >= 1e9) return (n / 1e9).toFixed(abs >= 1e10 ? 0 : 1).replace(/\.0$/, "") + "B";
    if (abs >= 1e6) return (n / 1e6).toFixed(abs >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
    if (abs >= 1e3) return (n / 1e3).toFixed(abs >= 1e4 ? 0 : 1).replace(/\.0$/, "") + "K";
    return String(Math.round(n));
  }

  function tableView(rows, headers) {
    return el("details", { class: "chart-table" },
      el("summary", null, "View as table"),
      el("div", { class: "table-wrap" },
        el("table", { class: "table" },
          el("thead", null, el("tr", null, headers.map((h, i) => el("th", { scope: "col", class: i ? "num" : null, text: h })))),
          el("tbody", null, rows.map((r) => el("tr", null, r.map((c, i) => el("td", { class: i ? "num" : null, text: c }))))))));
  }

  function frame(container, title) {
    const tooltip = el("div", { class: "chart-tip", role: "presentation", hidden: true });
    const plot = el("div", { class: "chart-plot" });
    container.replaceChildren(plot, tooltip);
    return { plot, tooltip, title };
  }

  function showTip(tooltip, plot, x, y, lines) {
    tooltip.replaceChildren(...lines.map((l, i) => el(i === 0 ? "strong" : "span", { text: l })));
    tooltip.hidden = false;
    const pw = plot.clientWidth;
    const tw = tooltip.offsetWidth;
    tooltip.style.left = Math.max(4, Math.min(pw - tw - 4, x - tw / 2)) + "px";
    tooltip.style.top = Math.max(0, y - tooltip.offsetHeight - 12) + "px";
  }

  function observe(container, draw) {
    draw();
    if (typeof ResizeObserver === "function") {
      let last = container.clientWidth;
      const ro = new ResizeObserver(() => {
        if (Math.abs(container.clientWidth - last) > 4) { last = container.clientWidth; draw(); }
      });
      ro.observe(container);
    }
  }

  /**
   * columns(container, { data: [{label, value, tip?}], format, colorFor(value), unit, title, valueLabel })
   */
  function columns(container, cfg) {
    const H = 220, M = { t: 16, r: 12, b: 28, l: 48 };
    const data = cfg.data || [];
    const format = cfg.format || ((v) => String(v));
    const allZero = data.every((d) => !d.value);

    observe(container, () => {
      const { plot, tooltip } = frame(container);
      const W = Math.max(280, container.clientWidth);
      const iw = W - M.l - M.r, ih = H - M.t - M.b;
      const values = data.map((d) => d.value);
      const yt = ticks(Math.min(0, ...values), Math.max(0, ...values, allZero ? 1 : 0), 4);
      const yMin = yt[0], yMax = yt[yt.length - 1];
      const y = (v) => M.t + ih - ((v - yMin) / (yMax - yMin)) * ih;
      const band = iw / Math.max(1, data.length);
      const barW = Math.max(2, Math.min(24, band - 2));

      const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": cfg.title || "Column chart" });
      const grid = svg("g", { class: "chart-grid" });
      yt.forEach((v) => {
        grid.append(svg("line", { x1: M.l, x2: W - M.r, y1: y(v), y2: y(v), class: v === 0 ? "chart-zero" : "" }));
        const t = svg("text", { x: M.l - 8, y: y(v) + 4, "text-anchor": "end", class: "chart-axis" });
        t.textContent = cfg.axisFormat ? cfg.axisFormat(v) : compact(v);
        grid.append(t);
      });
      root.append(grid);

      const every = Math.ceil(data.length / Math.max(2, Math.floor(iw / 70)));
      const bars = svg("g");
      data.forEach((d, i) => {
        const cx = M.l + band * i + band / 2;
        if (i % every === 0 || i === data.length - 1) {
          const t = svg("text", { x: cx, y: H - 8, "text-anchor": "middle", class: "chart-axis" });
          t.textContent = d.label;
          root.append(t);
        }
        if (d.value) {
          const y0 = y(0), y1 = y(d.value);
          const top = Math.min(y0, y1), h = Math.max(1, Math.abs(y1 - y0));
          const r = Math.min(4, barW / 2, h);
          const x0 = cx - barW / 2;
          // Rounded data-end, square at the baseline.
          const path = d.value > 0
            ? `M${x0},${top + h} V${top + r} Q${x0},${top} ${x0 + r},${top} H${x0 + barW - r} Q${x0 + barW},${top} ${x0 + barW},${top + r} V${top + h} Z`
            : `M${x0},${top} V${top + h - r} Q${x0},${top + h} ${x0 + r},${top + h} H${x0 + barW - r} Q${x0 + barW},${top + h} ${x0 + barW},${top + h - r} V${top} Z`;
          bars.append(svg("path", { d: path, fill: cfg.colorFor ? cfg.colorFor(d.value) : "var(--chart-1)", class: "chart-bar" }));
        }
        const hit = svg("rect", { x: M.l + band * i, y: M.t, width: band, height: ih, class: "chart-hit" });
        hit.addEventListener("mouseenter", () => {
          hit.classList.add("is-hover");
          const scale = plot.clientWidth / W;
          showTip(tooltip, plot, cx * scale, Math.min(y(0), y(d.value)) * scale, [d.tip || d.label, (cfg.valueLabel ? cfg.valueLabel + ": " : "") + format(d.value)]);
        });
        hit.addEventListener("mouseleave", () => { hit.classList.remove("is-hover"); tooltip.hidden = true; });
        bars.append(hit);
      });
      root.append(bars);
      plot.append(root);
      if (allZero) plot.append(el("p", { class: "chart-empty", text: cfg.emptyText || "No data in this period yet." }));
      container.append(tableView(data.map((d) => [d.tip || d.label, format(d.value)]), [cfg.labelHeader || "Date", cfg.valueLabel || "Value"]));
    });
  }

  /**
   * line(container, { points: [{label, value, tip?}], format, title, emptyText })
   */
  function line(container, cfg) {
    const H = 240, M = { t: 18, r: 72, b: 28, l: 56 };
    const pts = cfg.points || [];
    const format = cfg.format || ((v) => String(v));

    observe(container, () => {
      const { plot, tooltip } = frame(container);
      if (pts.length < 2) {
        plot.append(el("div", { class: "state state--empty" },
          el("p", { class: "state__title", text: cfg.emptyTitle || "Not enough data yet" }),
          el("p", { class: "state__text", text: cfg.emptyText || "The chart appears after a few changes have been recorded." })));
        return;
      }
      const W = Math.max(280, container.clientWidth);
      const iw = W - M.l - M.r, ih = H - M.t - M.b;
      const values = pts.map((p) => p.value);
      const yt = ticks(Math.min(0, ...values), Math.max(...values, 1), 4);
      const yMin = yt[0], yMax = yt[yt.length - 1];
      const x = (i) => M.l + (iw * i) / (pts.length - 1);
      const y = (v) => M.t + ih - ((v - yMin) / (yMax - yMin)) * ih;

      const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": cfg.title || "Line chart" });
      const grid = svg("g", { class: "chart-grid" });
      yt.forEach((v) => {
        grid.append(svg("line", { x1: M.l, x2: W - M.r, y1: y(v), y2: y(v), class: v === 0 ? "chart-zero" : "" }));
        const t = svg("text", { x: M.l - 8, y: y(v) + 4, "text-anchor": "end", class: "chart-axis" });
        t.textContent = cfg.axisFormat ? cfg.axisFormat(v) : compact(v);
        grid.append(t);
      });
      root.append(grid);
      [0, pts.length - 1].forEach((i) => {
        const t = svg("text", { x: x(i), y: H - 8, "text-anchor": i ? "end" : "start", class: "chart-axis" });
        t.textContent = pts[i].label;
        root.append(t);
      });

      const d = pts.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.value)}`).join(" ");
      root.append(svg("path", { d: `${d} L${x(pts.length - 1)},${y(Math.max(yMin, 0))} L${x(0)},${y(Math.max(yMin, 0))} Z`, class: "chart-area" }));
      root.append(svg("path", { d, class: "chart-line" }));
      const last = pts[pts.length - 1];
      root.append(svg("circle", { cx: x(pts.length - 1), cy: y(last.value), r: 5, class: "chart-dot" }));
      const endLabel = svg("text", { x: x(pts.length - 1) + 10, y: y(last.value) + 4, class: "chart-label" });
      endLabel.textContent = format(last.value);
      root.append(endLabel);

      const cross = svg("line", { y1: M.t, y2: M.t + ih, class: "chart-cross", visibility: "hidden" });
      const marker = svg("circle", { r: 5, class: "chart-dot", visibility: "hidden" });
      root.append(cross, marker);
      const hit = svg("rect", { x: M.l, y: M.t, width: iw, height: ih, class: "chart-hit chart-hit--line" });
      hit.addEventListener("mousemove", (e) => {
        const box = root.getBoundingClientRect();
        const px = ((e.clientX - box.left) / box.width) * W;
        const i = Math.max(0, Math.min(pts.length - 1, Math.round(((px - M.l) / iw) * (pts.length - 1))));
        cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
        marker.setAttribute("cx", x(i)); marker.setAttribute("cy", y(pts[i].value)); marker.setAttribute("visibility", "visible");
        const scale = plot.clientWidth / W;
        showTip(tooltip, plot, x(i) * scale, y(pts[i].value) * scale, [pts[i].tip || pts[i].label, format(pts[i].value)]);
      });
      hit.addEventListener("mouseleave", () => {
        cross.setAttribute("visibility", "hidden"); marker.setAttribute("visibility", "hidden"); tooltip.hidden = true;
      });
      root.append(hit);
      plot.append(root);
      container.append(tableView(pts.map((p) => [p.tip || p.label, format(p.value)]), [cfg.labelHeader || "Date", cfg.valueLabel || "Value"]));
    });
  }

  window.LedgerCharts = { columns, line };
})();
