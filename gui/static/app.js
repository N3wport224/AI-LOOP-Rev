// AutoMonetize control panel. No framework, no inline handlers (the CSP forbids them), and data
// only ever reaches the page through textContent, never innerHTML.
(function () {
  "use strict";
  let csrf = "";
  let settingsLoaded = false;
  let statusTimer = null;
  let logsTimer = null;

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("data-") || k.startsWith("aria-") || ["type", "id", "name", "value", "placeholder", "for", "title", "autocomplete", "inputmode", "role"].includes(k)) node.setAttribute(k, v === true ? "" : v);
      else node[k] = v;
    }
    for (const c of children) if (c !== null && c !== undefined) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return node;
  }

  async function api(path, options) {
    const opts = Object.assign({ headers: {}, credentials: "same-origin" }, options || {});
    if (opts.body && typeof opts.body !== "string") {
      opts.body = JSON.stringify(opts.body);
      opts.headers["Content-Type"] = "application/json";
    }
    if (opts.method && opts.method !== "GET") opts.headers["X-CSRF-Token"] = csrf;
    const resp = await fetch(path, opts);
    if (resp.status === 401) { location.href = "/login"; throw new Error("signed out"); }
    let data = {};
    try { data = await resp.json(); } catch (e) { /* empty body */ }
    data._status = resp.status;
    return data;
  }

  const money = (c) => "$" + ((c || 0) / 100).toFixed(2);
  function duration(s) {
    s = Math.floor(s || 0);
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  }
  const shortTime = (iso) => (iso || "").replace("T", " ").slice(0, 19);

  function badge(id, text, level) {
    const b = $(id);
    b.textContent = text;
    b.className = "badge" + (level ? " " + level : "");
  }

  // ------------------------------------------------------------------ tabs
  function showTab(name) {
    $$("[data-tab]").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
    $$(".tab").forEach((s) => { s.hidden = s.id !== "tab-" + name; });
    if (name === "settings" && !settingsLoaded) loadSettings();
    if (name === "logs") refreshLogs();
    if (name === "outreach") loadOutreach();
    if (name === "share") loadShare();
    try { localStorage.setItem("am_tab", name); } catch (e) { /* private mode */ }
  }

  // ------------------------------------------------------------------ status
  async function refreshStatus() {
    let s;
    try { s = await api("/api/status"); } catch (e) { return; }
    if (s._status !== 200) return;
    const sup = s.supervisor;
    badge("#b-supervisor", sup.running ? `Supervisor running (${sup.mode}, pid ${sup.pid})` : "Supervisor stopped", sup.running ? "ok" : "bad");
    badge("#b-webhook", s.webhook.healthy ? "Webhook healthy" : (sup.running ? "Webhook down" : "Webhook offline"),
      s.webhook.healthy ? "ok" : (s.webhook.secret_set ? "bad" : "warn"));
    const engineLevel = { running: "ok", paused: "warn", offline: "warn", quarantined: "warn", stopped: "bad", tripped: "bad" }[s.engine.state] || "";
    const flagNote = s.engine.state === "not running" && s.engine.flag !== "running" ? ` (${s.engine.flag})` : "";
    badge("#b-engine", "Engine " + s.engine.state + flagNote, engineLevel);
    badge("#b-mode", s.dry_run ? "Dry run" : "Live email", s.dry_run ? "warn" : "ok");

    $("#k-revenue").textContent = money(s.revenue.net_cents);
    const pct = Math.min(100, Math.round(100 * (s.revenue.net_cents || 0) / Math.max(1, s.revenue.target_cents)));
    $("#k-revenue-bar").style.width = pct + "%";
    $("#k-target").textContent = `${pct}% of the ${money(s.revenue.target_cents)}/day target`;
    $("#k-mrr").textContent = money(s.mrr_cents);
    $("#k-subs").textContent = `${s.subscribers} paying subscriber${s.subscribers === 1 ? "" : "s"}`;
    $("#k-niche").textContent = s.niche.key ? s.niche.key.split(":")[1] || s.niche.key : "none yet";
    $("#k-niche-sub").textContent = s.niche.key ? `cycle ${s.niche.iterations} of ${s.niche.pivot_after} before a pivot review` : "";
    $("#k-uptime").textContent = sup.running ? duration(s.uptime_seconds) : "—";
    $("#k-cycle").textContent = s.engine.last_cycle_at ? "last cycle " + shortTime(s.engine.last_cycle_at) + " UTC" : "";
    const leads = s.leads || {};
    $("#k-leads").textContent = String((leads.active || 0) + (leads.pending || 0));
    $("#k-leads-sub").textContent = `${leads.active || 0} confirmed · ${leads.pending || 0} pending · ${leads.unsubscribed || 0} left`;
    $("#engine-reason").textContent = s.engine.reason ? "Engine " + s.engine.state + ": " + s.engine.reason : "";
    renderGrowth(s.growth || {});
    renderEvolution(s.evolution || {});
    const fin = s.finance || {};
    $("#k-balance").textContent = fin.at ? money((fin.available_cents || 0) + (fin.pending_cents || 0)) : "—";
    $("#k-balance-sub").textContent = !fin.at ? "read after the next cycle"
      : fin.error ? "couldn't read: " + fin.error
      : `${money(fin.available_cents)} ready · ${money(fin.pending_cents)} on the way` +
        (fin.last_payout ? ` · last payout ${money(fin.last_payout.amount_cents)} (${fin.last_payout.status}, ${fin.last_payout.arrival_date})` : "");
    const items = s.todo || [];
    $("#todo-card").hidden = !items.length;
    $("#todo-list").replaceChildren(...items.map((i) => el("li", {},
      el("b", { text: i.title }), el("span", { class: "muted", text: ` (~${i.minutes} min): ${i.why}. ` }),
      el("code", { text: i.how }))));
    const pace = s.pace || {};
    $("#k-pace").textContent = pace.date ? money(pace.net_per_day_cents) + "/day" : "—";
    $("#k-pace-bar").style.width = Math.min(100, Math.round((pace.progress || 0) * 100)) + "%";
    $("#k-pace-sub").textContent = pace.date ? `goal ${money(pace.goal_cents)}/day · next: ${pace.next_step}` : "worked out after the next cycle";
    const paused = s.engine.flag === "paused";
    $("#btn-pause").hidden = paused;
    $("#btn-resume").hidden = !paused;
  }

  function renderGrowth(g) {
    const api = g.api || {};
    $("#k-api").textContent = String(api.requests_today || 0);
    $("#k-api-sub").textContent = `${api.requests_7d || 0} in 7 days` + (api.by_endpoint && Object.keys(api.by_endpoint).length
      ? " · " + Object.entries(api.by_endpoint).map(([k, v]) => `${k} ${v}`).join(", ") : "");
    $("#k-api-subs").textContent = String(api.subscribers || 0);
    $("#k-api-keys").textContent = api.enabled === false ? "API tier disabled"
      : `${api.active_keys || 0} active · ${api.degraded_keys || 0} past-due keys` + (api.checkout_url ? "" : " · not on sale yet");
    const copy = g.copy || {};
    $("#copy-meta").textContent = copy.version ? `v${copy.version} · ${copy.algorithm}` : "(no data yet)";
    $("#copy-arms tbody").replaceChildren(...(copy.arms || []).map((r) => {
      const win = (copy.winners || {})[r.slot] === r.variant;
      return el("tr", { class: win ? "winner" : (r.state === "deprecated" ? "retired" : "") },
        el("td", { text: r.slot }), el("td", { text: r.variant + (r.control ? " (control)" : "") + (win ? " ★" : "") }),
        el("td", { text: String(r.views) }), el("td", { text: String(r.clicks) }), el("td", { text: String(r.signups) }),
        el("td", { text: String(r.purchases) }), el("td", { text: (100 * r.conversion_rate).toFixed(1) + "%" }),
        el("td", { text: Math.round(100 * r.allocation) + "%" }), el("td", { class: "status-" + (r.state === "active" ? "ok" : "skip"), text: r.state }));
    }));
    const n = g.niches || {};
    const shares = Object.entries(n.shares || {});
    $("#niche-shares").textContent = shares.length
      ? "Niche capacity (last " + n.window_days + " days of verified revenue): " + shares.map(([k, v]) => `${k} ${Math.round(100 * v)}% ($${(((n.revenue_cents || {})[k] || 0) / 100).toFixed(2)})`).join(" · ")
      : "Single niche (no satellites yet).";
    const src = g.sources || {};
    $("#source-counts").textContent = `Discovered job sources: ${src.active || 0} active · ${src.trial || 0} in trial · ${src.candidate || 0} candidates · ${src.rejected || 0} rejected`;
  }

  function renderEvolution(ev) {
    $("#evo-state").textContent = ev.enabled ? "· ON" : "· off";
    $("#evo-counts").textContent = `${ev.attempted || 0} attempted · ${ev.merged || 0} merged · ${ev.rolled_back || 0} rolled back · `
      + `${ev.failed || 0} failed · ${ev.rejected || 0} rejected`;
    const last = ev.last_commit;
    $("#evo-last").textContent = "Last evolution: " + (last ? `${last.commit_sha.slice(0, 12)} ${last.title}` : "none yet");
    const c = ev.canary || {};
    const health = $("#evo-health");
    health.textContent = "Rollback health: " + (c.status ? c.status + (c.reason ? ` (${c.reason})` : c.until ? ` until ${shortTime(c.until)} UTC` : "")
      : "no canary running") + (ev.halted ? " · HALTED: " + ev.halted : "");
    health.className = ev.halted || c.status === "rolled_back" || c.status === "rollback_failed" ? "status-bad"
      : (c.status === "monitoring" ? "status-warn" : "");
    const secs = ev.cooldown_seconds || 0;
    $("#evo-cooldown").textContent = secs > 0
      ? `Cooldown: ${Math.floor(secs / 3600)}h ${String(Math.floor((secs % 3600) / 60)).padStart(2, "0")}m left (${ev.cooldown_reason || ""})`
      : "Cooldown: none";
    const findings = ((ev.diagnosis || {}).findings || []);
    $("#evo-findings").replaceChildren(...findings.map((f) => el("li", { text: `${f.severity} · ${f.kind}: ${f.summary}` })));
  }

  // ------------------------------------------------------------------ outreach review
  async function loadOutreach() {
    let d;
    try { d = await api("/api/outreach"); } catch (e) { return; }
    if (d._status !== 200) return;
    const pending = (d.counts || {}).pending_review || 0;
    $("#outreach-count").textContent = pending ? `(${pending})` : "";
    const warn = $("#outreach-warning");
    const problems = d.send_problems || [];
    warn.hidden = !problems.length && !d.dry_run;
    warn.textContent = problems.length ? "Approved emails can't be sent yet: " + problems.join("; ")
      : (d.dry_run ? "Dry run is on: approved emails are logged, not sent." : "");
    const list = $("#outreach-list");
    if (!d.drafts.length) { list.replaceChildren(el("p", { class: "muted", text: "No drafts waiting. The agent writes new ones as it finds companies." })); return; }
    list.replaceChildren(...d.drafts.map((r) => el("div", { class: "card draft" },
      el("label", {}, el("input", { type: "checkbox", class: "outreach-pick", value: String(r.id) }),
        " ", el("b", { text: r.subject }), el("span", { class: "muted", text: `  →  ${r.recipient} · score ${(r.score || 0).toFixed(2)}` })),
      el("details", {}, el("summary", { text: "Read the email" }), el("pre", { class: "log", text: r.body })),
      el("div", { class: "actions" },
        el("button", { class: "primary", "data-outreach": "approve", "data-id": String(r.id), text: "Approve" }),
        el("button", { "data-outreach": "reject", "data-id": String(r.id), text: "Reject" })))));
  }

  // ------------------------------------------------------------------ share kit
  let sharePosts = [];
  async function loadShare() {
    let d;
    try { d = await api("/api/share"); } catch (e) { return; }
    if (d._status !== 200) return;
    sharePosts = d.posts || [];
    const list = $("#share-list");
    if (!sharePosts.length) { list.replaceChildren(el("p", { class: "muted", text: "Nothing to share yet: posts appear once a product is on sale." })); return; }
    list.replaceChildren(...sharePosts.map((p, i) => el("div", { class: "card draft" },
      el("b", { text: `${p.label}` }), el("span", { class: "muted", text: `  ·  ${p.product} (${p.price})` }),
      el("pre", { class: "log", text: p.text }),
      el("div", { class: "actions" }, el("button", { class: "primary", "data-share": String(i), text: "Copy" })))));
  }

  async function copyShare(i) {
    const msg = $("#share-message");
    try {
      await navigator.clipboard.writeText(sharePosts[i].text);
      msg.textContent = `Copied the ${sharePosts[i].label} post. Paste it where you post.`;
      msg.className = "message ok";
    } catch (e) {
      msg.textContent = "Couldn't copy automatically: select the text and copy it by hand.";
      msg.className = "message bad";
    }
  }

  async function outreachAction(action, ids) {
    if (!ids.length) return;
    const r = await api("/api/outreach", { method: "POST", body: { action, ids } });
    const msg = $("#outreach-message");
    msg.textContent = r.message || (r.ok ? "Done." : "Failed.");
    msg.className = "message " + (r.ok ? "ok" : "bad");
    loadOutreach();
  }

  async function control(action) {
    if (action === "kill" && !confirm("Stop the engine AND the webhook listener until you press Start?\n\nPaid orders will wait (Stripe retries for 3 days).")) return;
    const msg = $("#control-message");
    const buttons = $$("[data-action]");
    buttons.forEach((b) => { b.disabled = true; });
    msg.className = "message";
    msg.textContent = "Working…";
    try {
      const r = await api("/api/control", { method: "POST", body: { action, confirm: action === "kill" } });
      msg.textContent = r.message || (r.ok ? "Done." : "Failed.");
      msg.className = "message " + (r.ok ? "ok" : "bad");
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
      refreshStatus();
      refreshLogs();
    }
  }

  // ------------------------------------------------------------------ logs
  function fillActions(table, rows, withCycle) {
    const body = $("tbody", table);
    body.replaceChildren(...rows.map((a) => el("tr", {},
      el("td", { text: shortTime(a.created_at) }),
      withCycle ? el("td", { text: String(a.cycle) }) : null,
      el("td", { text: a.name }),
      el("td", { class: "status-" + a.status, text: a.status }),
      el("td", { text: a.detail || "" }))));
  }

  async function refreshLogs() {
    let data;
    try { data = await api("/api/logs?lines=300"); } catch (e) { return; }
    if (data._status !== 200) return;
    fillActions($("#recent-actions"), data.actions.slice(0, 15), false);
    fillActions($("#log-actions"), data.actions, true);
    const box = $("#log-files");
    const files = Object.entries(data.files || {});
    box.replaceChildren(...(files.length ? files : [["No log file yet", ["The supervisor writes to ~/Library/Logs/automonetize/ (launchd) or data/agent.log."]]]).map(([path, lines]) =>
      el("div", { class: "card" }, el("h2", {}, path), el("pre", { class: "log", text: lines.join("\n") }))));
    box.querySelectorAll("pre").forEach((p) => { p.scrollTop = p.scrollHeight; });
  }

  // ------------------------------------------------------------------ settings
  function fieldControl(f) {
    const id = "f-" + f.key;
    const wrap = el("div", { class: "control" });
    if (f.key === "STRIPE_MODE") {
      const seg = el("div", { class: "segmented", role: "group", "aria-label": "Stripe mode" });
      ["test", "live"].forEach((m) => {
        const b = el("button", { type: "button", class: m, "aria-pressed": String(f.value === m), "data-mode": m, text: m === "live" ? "Live" : "Test" });
        b.addEventListener("click", () => {
          $$("button", seg).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
          hidden.value = m;
        });
        seg.append(b);
      });
      const hidden = el("input", { type: "hidden", name: f.key, value: f.value || "test", id });
      wrap.append(seg, hidden);
      return wrap;
    }
    if (f.kind === "select" || f.kind === "bool") {
      const sel = el("select", { id, name: f.key });
      if (!f.value) sel.append(el("option", { value: "", text: "(not set)" }));
      f.options.forEach((o) => sel.append(el("option", { value: o, text: o })));
      sel.value = f.value || "";
      wrap.append(sel);
      return wrap;
    }
    const input = el("input", {
      id, name: f.key, type: f.secret ? "password" : (f.kind === "email" ? "email" : "text"),
      value: f.secret ? "" : (f.value || ""), autocomplete: f.secret ? "new-password" : "off",
      placeholder: f.secret && f.set ? `stored (${f.hint}). Leave empty to keep` : (f.placeholder || ""),
      inputmode: f.kind === "number" ? "numeric" : null, spellcheck: false,
    });
    wrap.append(input);
    if (f.secret) {
      const toggle = el("button", { type: "button", class: "toggle", title: "Show or hide what you type", text: "Show" });
      toggle.addEventListener("click", () => {
        const showing = input.type === "text";
        input.type = showing ? "password" : "text";
        toggle.textContent = showing ? "Show" : "Hide";
      });
      wrap.append(toggle);
      if (f.set) {
        wrap.append(el("span", { class: "set-pill", text: "set" }));
        const clear = el("button", { type: "button", class: "clear link", text: "Clear", "data-clear": f.key });
        clear.addEventListener("click", () => {
          if (!confirm(`Remove the stored ${f.label}?`)) return;
          clear.dataset.pending = "1";
          clear.textContent = "Will clear on save";
        });
        wrap.append(clear);
      }
    }
    return wrap;
  }

  async function loadSettings() {
    const data = await api("/api/settings");
    if (data._status !== 200) return;
    settingsLoaded = true;
    $("#env-path").textContent = "Writes to " + data.env_file;
    const groups = $("#settings-groups");
    groups.replaceChildren(...data.groups.map((g) => {
      const fields = data.fields.filter((f) => f.group === g.id);
      return el("fieldset", { class: "card group" },
        el("h2", { text: g.title }), el("p", { class: "muted", text: g.help }),
        ...fields.map((f) => el("div", { class: "field" },
          el("label", { for: "f-" + f.key }, f.label, el("span", { class: "key", text: f.key })),
          fieldControl(f),
          f.help ? el("div", { class: "hint", text: f.help }) : null,
          el("div", { class: "error-text", id: "err-" + f.key }))));
    }));
  }

  function collectSettings() {
    const values = {};
    $$("#settings-form [name]").forEach((i) => {
      const secret = i.type === "password" || (i.type === "text" && $("#f-" + i.name)?.closest(".control")?.querySelector(".toggle"));
      if (secret && !i.value) return; // unchanged secret
      values[i.name] = i.value;
    });
    const clear = $$("[data-clear][data-pending]").map((b) => b.dataset.clear);
    return { values, clear };
  }

  function showChecks(result) {
    $("#settings-result").hidden = false;
    const warn = $("#settings-warnings");
    const notes = [];
    if (result.saved && result.saved.length) notes.push(el("div", { class: "notice ok", text: "Saved: " + result.saved.join(", ") }));
    else if (result.saved) notes.push(el("div", { class: "notice ok", text: "No changes to save." }));
    (result.warnings || []).forEach((w) => notes.push(el("div", { class: "notice", text: w })));
    if (result.restart_required) {
      const btn = el("button", { type: "button", class: "primary", text: "Restart the engine now" });
      btn.addEventListener("click", () => { showTab("control"); control("restart"); });
      notes.push(el("div", { class: "notice" }, "The running engine still uses the old settings. ", btn));
    }
    warn.replaceChildren(...notes);
    $("#checks tbody").replaceChildren(...(result.checks || []).map((c) => el("tr", {},
      el("td", { text: c.step }), el("td", { text: c.name }), el("td", { class: "status-" + c.status, text: c.status }),
      el("td", { text: c.detail || "" }))));
    $("#settings-result").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function saveSettings(ev) {
    ev.preventDefault();
    $$(".error-text").forEach((e) => { e.textContent = ""; });
    $$("[aria-invalid]").forEach((e) => e.removeAttribute("aria-invalid"));
    const btn = $("#save-settings");
    btn.disabled = true;
    btn.textContent = "Saving & verifying…";
    try {
      const r = await api("/api/settings", { method: "POST", body: collectSettings() });
      if (!r.ok) {
        Object.entries(r.errors || {}).forEach(([k, msg]) => {
          const e = $("#err-" + k);
          if (e) e.textContent = msg; else alert(msg);
          $("#f-" + k)?.setAttribute("aria-invalid", "true");
        });
        const first = $("[aria-invalid]");
        if (first) first.focus();
        return;
      }
      settingsLoaded = false;
      await loadSettings();
      showChecks(r);
    } finally {
      btn.disabled = false;
      btn.textContent = "Save & verify";
    }
  }

  async function preflight() {
    const b = $("#run-preflight");
    b.disabled = true;
    try { showChecks(await api("/api/preflight", { method: "POST", body: {} })); } finally { b.disabled = false; }
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    const s = await api("/api/session");
    csrf = s.csrf;
    $$("[data-tab]").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
    $$("[data-action]").forEach((b) => b.addEventListener("click", () => control(b.dataset.action)));
    $("#settings-form").addEventListener("submit", saveSettings);
    $("#run-preflight").addEventListener("click", preflight);
    $("#outreach-list").addEventListener("click", (e) => {  // delegated: the list is re-rendered
      const b = e.target.closest("[data-outreach]");
      if (b) outreachAction(b.dataset.outreach, [Number(b.dataset.id)]);
    });
    $("#share-list").addEventListener("click", (e) => {
      const b = e.target.closest("[data-share]");
      if (b) copyShare(Number(b.dataset.share));
    });
    const picked = () => $$(".outreach-pick").filter((c) => c.checked).map((c) => Number(c.value));
    $("#outreach-approve-selected").addEventListener("click", () => outreachAction("approve", picked()));
    $("#outreach-reject-selected").addEventListener("click", () => outreachAction("reject", picked()));
    $("#outreach-select-all").addEventListener("change", (e) => { $$(".outreach-pick").forEach((c) => { c.checked = e.target.checked; }); });
    $("#logout").addEventListener("click", async () => { await api("/logout", { method: "POST" }); location.href = "/login"; });
    let tab = "control";
    try { tab = localStorage.getItem("am_tab") || "control"; } catch (e) { /* private mode */ }
    showTab(tab);
    refreshStatus();
    refreshLogs();
    statusTimer = setInterval(refreshStatus, 4000);
    logsTimer = setInterval(() => { if (!document.hidden) refreshLogs(); }, 8000);
  }

  boot().catch((e) => { console.error(e); });
})();
