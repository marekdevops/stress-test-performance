"""Single-page HTML shell. No external assets - the pod may have no egress."""

PAGE = """<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sysbench &middot; OpenShift CPU/RAM performance</title>
<style>
  :root {
    --bg: #ffffff; --fg: #151515; --muted: #6a6e73; --line: #d2d2d2;
    --panel: #f5f5f5; --accent: #06c; --ok: #3e8635; --warn: #c9190b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #151515; --fg: #e0e0e0; --muted: #9a9da1; --line: #3c3f42;
      --panel: #1f1f1f; --accent: #73bcf7; --ok: #5ba352; --warn: #f0561d;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 1.5rem; background: var(--bg); color: var(--fg);
    font: 15px/1.5 "Red Hat Text", -apple-system, system-ui, sans-serif;
  }
  h1 { font-size: 1.35rem; margin: 0 0 .25rem; font-weight: 600; }
  h2 { font-size: 1.05rem; margin: 0; font-weight: 600; }
  .sub { color: var(--muted); font-size: .85rem; margin-bottom: 1.25rem; }
  .grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(430px, 1fr)); }
  .card { border: 1px solid var(--line); border-radius: 6px; background: var(--panel); padding: 1rem; }
  .card header { display: flex; align-items: center; justify-content: space-between; gap: .5rem; margin-bottom: .75rem; }
  button {
    font: inherit; font-size: .85rem; padding: .3rem .8rem; cursor: pointer;
    border: 1px solid var(--accent); background: transparent; color: var(--accent); border-radius: 4px;
  }
  button:hover:not(:disabled) { background: var(--accent); color: var(--bg); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  table { width: 100%; border-collapse: collapse; font-size: .88rem; }
  td { padding: .28rem 0; border-bottom: 1px solid var(--line); }
  td:last-child { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }
  .key { color: var(--muted); font-weight: 400; }
  .big { font-size: 1.9rem; font-weight: 600; font-variant-numeric: tabular-nums; }
  .big small { font-size: .8rem; font-weight: 400; color: var(--muted); }
  pre {
    background: var(--bg); border: 1px solid var(--line); border-radius: 4px;
    padding: .6rem; overflow-x: auto; font-size: .78rem; margin: .75rem 0 0; max-height: 320px;
  }
  details summary { cursor: pointer; color: var(--muted); font-size: .82rem; margin-top: .5rem; }
  .badge { font-size: .72rem; padding: .1rem .45rem; border-radius: 3px; border: 1px solid var(--line); color: var(--muted); }
  .badge.ok { color: var(--ok); border-color: var(--ok); }
  .badge.warn { color: var(--warn); border-color: var(--warn); }
  .bar { margin-bottom: 1rem; display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; }
  .empty { color: var(--muted); font-size: .88rem; padding: 1.5rem 0; text-align: center; }
  .hist { font-size: .8rem; }
  .hist td { padding: .2rem .5rem .2rem 0; }
  code { font-size: .78rem; color: var(--muted); word-break: break-all; }
</style>
</head>
<body>
<h1>sysbench &middot; CPU / RAM</h1>
<div class="sub" id="ctx">&nbsp;</div>

<div class="bar">
  <button id="run-cpu">Uruchom test CPU</button>
  <button id="run-mem">Uruchom test RAM</button>
  <button id="run-all">Uruchom oba</button>
  <span id="state" class="badge">&mdash;</span>
</div>

<div class="grid">
  <section class="card" id="card-cpu">
    <header><h2>CPU</h2><span class="badge" id="badge-cpu">brak wyniku</span></header>
    <div id="body-cpu" class="empty">Jeszcze nie uruchomiono.</div>
  </section>
  <section class="card" id="card-memory">
    <header><h2>RAM</h2><span class="badge" id="badge-memory">brak wyniku</span></header>
    <div id="body-memory" class="empty">Jeszcze nie uruchomiono.</div>
  </section>
</div>

<section class="card" style="margin-top:1rem">
  <header><h2>Historia przebiegów</h2></header>
  <div id="history" class="empty">&mdash;</div>
  <details>
    <summary>lscpu (widok z wnętrza poda)</summary>
    <pre id="lscpu">&mdash;</pre>
  </details>
</section>

<script>
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) =>
  v === null || v === undefined ? "&mdash;" : Number(v).toLocaleString("pl-PL",
    { minimumFractionDigits: d, maximumFractionDigits: d });

function row(key, value) {
  return `<tr><td class="key">${key}</td><td>${value}</td></tr>`;
}

function renderResult(kind, r) {
  const m = r.metrics || {};
  const t = r.throttling || {};
  const throttled = (t.nr_throttled || 0) > 0;
  const headline = kind === "cpu"
    ? `<div class="big">${fmt(m.events_per_second)} <small>events/s</small></div>`
    : `<div class="big">${fmt(m.throughput_mib_s)} <small>MiB/s</small></div>`;

  let rows = "";
  if (kind === "memory") {
    rows += row("przeniesione", `${fmt(m.transferred_mib, 0)} MiB`);
    rows += row("operacje/s", fmt(m.events_per_second));
  }
  rows += row("total time", `${fmt(m.total_time_s, 3)} s`);
  rows += row("total events", fmt(m.total_events, 0));
  rows += row("latency min / avg / max",
    `${fmt(m.latency_min_ms, 3)} / ${fmt(m.latency_avg_ms, 3)} / ${fmt(m.latency_max_ms, 3)} ms`);
  rows += row("latency 95p", `${fmt(m.latency_p95_ms, 3)} ms`);
  rows += row("CFS throttling",
    throttled
      ? `<span class="badge warn">${t.nr_throttled}&times; / ${fmt((t.throttled_usec || 0) / 1e6, 2)} s</span>`
      : `<span class="badge ok">brak</span>`);
  rows += row("limit CPU (cgroup)", `${r.context_before.cpu_quota_cores ?? "brak"} rdz.`);
  rows += row("physical_package_id", `[${r.context_before.physical_package_ids.join(", ")}]`);
  rows += row("MHz (przed &rarr; po)",
    `${fmt(r.context_before.cpu_mhz_max, 0)} &rarr; ${fmt(r.context_after.cpu_mhz_max, 0)}`);

  return `${headline}
    <table>${rows}</table>
    <div style="margin-top:.6rem"><code>${r.command}</code></div>
    <details><summary>Surowy output sysbench</summary><pre>${
      (r.stdout || "").replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]))
    }${r.stderr ? "\\n--- stderr ---\\n" + r.stderr : ""}</pre></details>`;
}

function render(state) {
  const s = state.snapshot || {};
  $("ctx").innerHTML =
    `node <b>${s.node_name}</b> &middot; pod <b>${s.pod_name}</b> &middot; ` +
    `${s.nproc} vCPU &middot; limit ${s.cpu_quota_cores ?? "brak"} rdz. &middot; ` +
    `${s.physical_package_count} pakiet(y) &middot; ${s.sysbench_version}`;
  $("lscpu").textContent = s.lscpu || "—";

  const running = state.running;
  document.querySelectorAll(".bar button").forEach((b) => (b.disabled = !!running));
  $("state").textContent = running ? `trwa: ${running.kind}…` : "gotowy";
  $("state").className = "badge" + (running ? " warn" : " ok");

  for (const kind of ["cpu", "memory"]) {
    const r = (state.latest || {})[kind];
    const badge = $("badge-" + kind);
    if (!r) { badge.textContent = "brak wyniku"; badge.className = "badge"; continue; }
    badge.textContent = `${r.status} · ${r.finished_at}`;
    badge.className = "badge " + (r.status === "ok" ? "ok" : "warn");
    $("body-" + kind).className = "";
    $("body-" + kind).innerHTML = renderResult(kind, r);
  }

  const hist = (state.history || []).slice().reverse();
  $("history").className = hist.length ? "" : "empty";
  $("history").innerHTML = hist.length
    ? `<table class="hist">${hist.map((r) => {
        const m = r.metrics || {};
        const v = r.kind === "cpu"
          ? `${fmt(m.events_per_second)} events/s`
          : `${fmt(m.throughput_mib_s)} MiB/s`;
        return `<tr><td>${r.started_at}</td><td>${r.kind}</td><td>${r.status}</td>
                <td>${r.duration_s}s</td><td><b>${v}</b></td></tr>`;
      }).join("")}</table>`
    : "—";
}

async function refresh() {
  try { render(await (await fetch("api/results")).json()); } catch (e) { /* transient */ }
}

async function trigger(path) {
  const res = await fetch(path, { method: "POST" });
  if (res.status === 409) alert("Benchmark już trwa — poczekaj na zakończenie.");
  refresh();
}

$("run-cpu").onclick = () => trigger("run/cpu");
$("run-mem").onclick = () => trigger("run/memory");
$("run-all").onclick = () => trigger("run/all");

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""
