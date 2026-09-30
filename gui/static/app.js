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
    const paused = s.engine.flag === "paused";
    $("#btn-pause").hidden = paused;
    $("#btn-resume").hidden = !paused;
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
