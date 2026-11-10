// combo-edge dashboard helpers. Chart.js styling follows the dataviz rules: thin marks, recessive grid,
// legend for >= 2 series, hover tooltips by default, one y-axis per chart, text in ink tokens.
(function () {
  const css = getComputedStyle(document.documentElement);
  const v = (n) => css.getPropertyValue(n).trim();
  const INK2 = v("--ink-2"), MUTED = v("--ink-muted"), GRID = v("--grid"), S1 = v("--series-1"), S2 = v("--series-2"), SURF = v("--surface-1");

  if (window.Chart) {
    Chart.defaults.color = MUTED;
    Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
    Chart.defaults.font.size = 12;
    Chart.defaults.borderColor = GRID;
    Chart.defaults.plugins.legend.labels.color = INK2;
    Chart.defaults.plugins.legend.labels.boxWidth = 10;
    Chart.defaults.plugins.tooltip.backgroundColor = "#0d0d0d";
    Chart.defaults.plugins.tooltip.borderColor = "rgba(255,255,255,.12)";
    Chart.defaults.plugins.tooltip.borderWidth = 1;
    Chart.defaults.maintainAspectRatio = false;
  }

  const axes = (yTitle) => ({
    x: { grid: { display: false }, ticks: { color: MUTED, maxRotation: 0, autoSkip: true } },
    y: { grid: { color: GRID }, border: { display: false }, ticks: { color: MUTED }, title: { display: !!yTitle, text: yTitle, color: MUTED } },
  });

  async function fetchJSON(url) { const r = await fetch(url); return r.json(); }

  // Mean edge per bucket with its 95% CI as a whisker (single series -> slot 1, no legend box).
  async function edgeBars(el) {
    const d = await fetchJSON(el.dataset.src);
    if (!d.labels || !d.labels.length) { el.innerHTML = '<div class="empty">No quotes yet</div>'; return; }
    const canvas = document.createElement("canvas"); el.appendChild(canvas);
    const whisker = {
      id: "whisker",
      afterDatasetsDraw(chart) {
        const { ctx, scales: { y } } = chart; const meta = chart.getDatasetMeta(0);
        ctx.save(); ctx.strokeStyle = INK2; ctx.lineWidth = 1.5;
        meta.data.forEach((bar, i) => {
          const lo = d.lo[i], hi = d.hi[i]; if (lo == null || hi == null) return;
          const x = bar.x, y1 = y.getPixelForValue(lo), y2 = y.getPixelForValue(hi);
          ctx.beginPath(); ctx.moveTo(x, y1); ctx.lineTo(x, y2); ctx.moveTo(x - 4, y1); ctx.lineTo(x + 4, y1); ctx.moveTo(x - 4, y2); ctx.lineTo(x + 4, y2); ctx.stroke();
        });
        ctx.restore();
      },
    };
    new Chart(canvas, {
      type: "bar",
      data: { labels: d.labels, datasets: [{ label: "Mean edge %", data: d.mean, backgroundColor: S1, borderRadius: 4, borderSkipped: "start", maxBarThickness: 36 }] },
      options: {
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => {
          const i = c.dataIndex; const ci = d.lo[i] == null ? "" : ` (95% CI ${d.lo[i]}% to ${d.hi[i]}%)`;
          return `${c.formattedValue}% mean edge${ci}, n=${d.n[i]}`; } } } },
        scales: axes("mean edge at the quote, %"),
      },
      plugins: [whisker],
    });
  }

  async function countBars(el) {
    const d = await fetchJSON(el.dataset.src);
    if (!d.labels || !d.labels.length) { el.innerHTML = '<div class="empty">No data yet</div>'; return; }
    const canvas = document.createElement("canvas"); el.appendChild(canvas);
    const horizontal = el.dataset.horizontal === "1";
    new Chart(canvas, {
      type: "bar",
      data: { labels: d.labels, datasets: [{ label: el.dataset.label || "count", data: d.n, backgroundColor: S1, borderRadius: 4, borderSkipped: "start", maxBarThickness: 28 }] },
      options: {
        indexAxis: horizontal ? "y" : "x",
        plugins: { legend: { display: false } },
        scales: horizontal ? { x: { grid: { color: GRID }, ticks: { color: MUTED } }, y: { grid: { display: false }, ticks: { color: INK2, font: { size: 11 } } } } : axes(el.dataset.ytitle),
      },
    });
  }

  // Paper vs live lines on one axis (same unit), legend + direct end labels via the legend.
  async function bookLines(el) {
    const d = await fetchJSON(el.dataset.src);
    const sets = [["paper", S1], ["live", S2]].filter(([k]) => d[k] && d[k].length);
    if (!sets.length) { el.innerHTML = '<div class="empty">Nothing settled yet</div>'; return; }
    const canvas = document.createElement("canvas"); el.appendChild(canvas);
    new Chart(canvas, {
      type: "line",
      data: { datasets: sets.map(([k, c]) => ({ label: k, data: d[k].map((p, i) => ({ x: i + 1, y: p.y, t: p.x })), borderColor: c, backgroundColor: c,
        borderWidth: 2, pointRadius: 3, pointHoverRadius: 5, pointBorderColor: SURF, pointBorderWidth: 2, tension: 0 })) },
      options: {
        parsing: false, interaction: { mode: "nearest", intersect: false },
        plugins: { legend: { display: sets.length > 1, position: "top", align: "end" },
          tooltip: { callbacks: { title: (items) => items[0].raw.t, label: (c) => `${c.dataset.label}: ${c.formattedValue}${el.dataset.unit || ""}` } } },
        scales: { x: { type: "linear", grid: { display: false }, ticks: { color: MUTED, precision: 0 }, title: { display: true, text: el.dataset.xtitle || "bet #", color: MUTED } },
                  y: { grid: { color: GRID }, border: { display: false }, ticks: { color: MUTED }, title: { display: true, text: el.dataset.ytitle || "", color: MUTED } } },
      },
    });
  }

  function init(root) {
    root.querySelectorAll("[data-chart='edge']").forEach(edgeBars);
    root.querySelectorAll("[data-chart='count']").forEach(countBars);
    root.querySelectorAll("[data-chart='lines']").forEach(bookLines);
  }
  document.addEventListener("DOMContentLoaded", () => init(document));

  window.ceEnvSwitch = function (form) {
    const env = form.env.value;
    if (env === "prod") {
      const typed = prompt("Switching to PRODUCTION (real account). Type 'prod' to confirm:");
      if (typed !== "prod") return false;
      form.confirm.value = "prod";
    }
    return true;
  };
  document.addEventListener("change", (e) => { if (e.target.matches(".topbar select[name=env]")) { if (window.ceEnvSwitch(e.target.form)) e.target.form.submit(); } });
})();

// Max-payout filter on the Scan tab. Client-side, so it stacks with the server-side family/team/kickoff
// filters; re-applied after every HTMX swap and remembered in localStorage. Parsing mirrors
// ranking.parse_max_payout: "40", "40x", "<40x", "<=100x"; blank = show all.
(function () {
  const KEY = "combo-edge:max-payout";
  const RE = /^\s*(?:<=?|≤)?\s*(\d+(?:\.\d+)?)\s*x?\s*$/i;

  function load() { try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; } }
  function save(v) { try { localStorage.setItem(KEY, v); } catch (e) { /* private mode: not remembered */ } }

  function parse(text) {
    if (!text || !text.trim()) return { max: null };
    const m = RE.exec(text);
    if (!m || parseFloat(m[1]) <= 0) return { error: "Use a number like 40, 40x or <40x" };
    return { max: parseFloat(m[1]) };
  }

  function apply() {
    const input = document.getElementById("max-payout");
    const table = document.getElementById("results-table");
    if (!input || !table) return;
    const { max, error } = parse(input.value);
    const err = document.getElementById("payout-error");
    if (err) err.textContent = error || "";
    const rows = table.querySelectorAll("tbody tr[data-payout]");
    let shown = 0;
    rows.forEach((tr) => {
      const p = tr.dataset.payout === "" ? null : parseFloat(tr.dataset.payout);
      const hide = !error && max != null && p != null && p > max + 1e-9;
      tr.hidden = hide;
      if (!hide) shown += 1;
    });
    const total = parseInt(table.dataset.total || rows.length, 10);
    const count = document.getElementById("payout-count");
    if (count) count.textContent = `showing ${shown} of ${rows.length}` + (total > rows.length ? ` (top ${rows.length} of ${total})` : "");
    document.querySelectorAll("[data-max-payout]").forEach((b) => b.classList.toggle("active", b.dataset.maxPayout === input.value.trim()));
  }

  function bind() {
    const input = document.getElementById("max-payout");
    if (!input || input.dataset.bound) { apply(); return; }
    input.dataset.bound = "1";
    input.value = load();
    input.addEventListener("input", () => { save(input.value); apply(); });
    document.querySelectorAll("[data-max-payout]").forEach((b) => b.addEventListener("click", () => {
      input.value = b.dataset.maxPayout; save(input.value); apply();
    }));
    apply();
  }

  document.addEventListener("DOMContentLoaded", bind);
  document.addEventListener("htmx:afterSwap", bind);
})();
