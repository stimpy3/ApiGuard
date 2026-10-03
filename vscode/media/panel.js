// The API Guard panel page. It only draws and sends messages; the extension
// (src/panel.ts) runs api-guard. Every text from api-guard or the model is
// set with textContent, never as HTML.
// @ts-check
(function () {
  // @ts-ignore
  const vscode = acquireVsCodeApi();
  const saved = vscode.getState() || {};

  const S = {
    tab: saved.tab || "overview",
    state: null, // from the extension: result, ai, waivers, user, ...
    busy: new Set(),
    init: null, // last init --json
    initRan: false,
    ci: "auto",
    force: false,
    chat: saved.chat || [], // {question, data?, pending?}
    reviews: null,
    review: null, // {id, data}
    decided: {}, // id -> outcome
    openForm: null, // fingerprint whose waiver form is open
    draft: "", // the Ask box, kept across redraws
  };

  const send = (type, extra) => vscode.postMessage({ type, ...(extra || {}) });
  const keep = () => vscode.setState({ tab: S.tab, chat: S.chat.slice(-20) });

  // --- tiny DOM helper ----------------------------------------------------------------
  /** h("div.card", {onclick}, "text", child, [children]) */
  function h(spec, attrs, ...kids) {
    const [tag, ...classes] = spec.split(".");
    const el = document.createElement(tag || "div");
    if (classes.length) el.className = classes.join(" ");
    if (attrs && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) {
      kids.unshift(attrs);
      attrs = null;
    }
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "value") el.value = v;
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    const add = (k) => {
      if (k === null || k === undefined || k === false) return;
      if (Array.isArray(k)) return k.forEach(add);
      el.append(k instanceof Node ? k : document.createTextNode(String(k)));
    };
    kids.forEach(add);
    return el;
  }
  const btn = (label, onclick, cls = "", busy = false) =>
    h("button" + (cls ? "." + cls : ""), { onclick, disabled: busy }, busy ? h("span.spin") : null, label);
  const copyBtn = (text) => btn("Copy", () => send("copy", { text }), "small secondary");
  const fill = (id, ...kids) => {
    const el = document.getElementById(id);
    el.replaceChildren(...kids.flat().filter(Boolean));
  };
  const statusBadge = (status) => h("span.badge." + status.replace(/\s+/g, "-"), status);

  // --- tabs ---------------------------------------------------------------------------
  function selectTab(tab) {
    S.tab = tab;
    keep();
    document.querySelectorAll("nav.tabs button").forEach((b) => {
      b.setAttribute("aria-selected", String(b.getAttribute("data-tab") === tab));
    });
    document.querySelectorAll("main section").forEach((s) => {
      s.hidden = s.id !== tab;
    });
    if (tab === "setup" && !S.init && !S.busy.has("init")) send("initPreview", { ci: S.ci });
    if (tab === "reviews" && !S.reviews && !S.busy.has("reviews")) send("reviews");
  }
  document.querySelectorAll("nav.tabs button").forEach((b) =>
    b.addEventListener("click", () => selectTab(b.getAttribute("data-tab"))),
  );

  // --- overview -----------------------------------------------------------------------
  function renderOverview() {
    const st = S.state;
    if (!st) return fill("overview", h("p.muted", "Loading…"));
    if (!st.root) return fill("overview", h("div.card", "Open a project folder to check its API."));

    const r = st.result;
    const tone = st.checking ? "busy" : !r ? "none" : r.verdict === "passed" ? "ok" : r.verdict === "failed" ? "blocked" : "error";
    const title = st.checking ? "Checking…" : r ? st.headline : st.problem ? "API contract: can't check" : "Not checked yet";
    const banner = h(
      "div.banner." + tone,
      h("div.banner-text", h("h1", title), r ? h("p.muted", "Spec ", h("code", r.meta.spec || "openapi.yaml"), " compared with ", h("code", r.meta.base || "the main branch"), " · ", new Date(r.generated_at).toLocaleString()) : null),
      h("div.actions", btn("Check again", () => send("check"), "", st.checking), r ? btn("Full report", () => send("openReport"), "secondary") : null),
    );

    const parts = [banner];
    if (st.problem && !st.checking) {
      parts.push(
        h("div.card.warn", h("h3", "api-guard couldn't run a check"), h("p", st.problem),
          h("div.actions", btn("Set up this project", () => selectTab("setup")), btn("Show the log", () => send("log"), "secondary"))),
      );
    }
    if (!st.configured && !st.problem && r) {
      parts.push(h("div.card.hint", "Running on defaults (no api-guard.yaml). ", h("a", { href: "#", onclick: (e) => (e.preventDefault(), selectTab("setup")) }, "Set up"), " to add CI and turn on freshness."));
    }
    if (r) {
      parts.push(h("h2", "Checks"), h("div.grid", r.checks.map(checkCard)));
      parts.push(h("h2", `Changes (${r.changes.length})`));
      parts.push(r.changes.length ? h("div.list", r.changes.map((c, i) => changeRow(c, st.blocking[i]))) : h("p.muted", "No contract changes against the main branch."));
      parts.push(waiversSection(r));
    }
    fill("overview", parts);
  }

  function checkCard(c) {
    const label = { breaking: "Breaking changes", freshness: "Spec is up to date", conformance: "API matches its spec" }[c.name] || c.name;
    return h("div.check." + c.status,
      h("div.check-head", h("span.dot"), h("strong", label), statusBadge(c.status === "skipped" ? "not checked" : c.status)),
      h("p", c.summary),
      c.detail ? h("details", h("summary", "Details"), h("pre", c.detail)) : null);
  }

  function changeRow(c, blocking) {
    const where = [c.operation, c.path].filter(Boolean).join(" ");
    const formOpen = S.openForm === c.fingerprint;
    const row = h("div.change" + (blocking ? ".blocking" : ""),
      h("div.change-head",
        h("span.sev." + c.severity, c.severity), blocking ? h("span.badge.failed", "blocks") : null,
        h("code", where || "(whole spec)")),
      h("p", c.text),
      h("div.meta", "rule ", h("code", c.id), c.fingerprint ? [" · fingerprint ", h("code", c.fingerprint)] : null),
      h("div.actions",
        btn("Show in spec", () => send("reveal", { change: c }), "small secondary"),
        blocking && c.fingerprint && !formOpen ? btn("Accept this break…", () => { S.openForm = c.fingerprint; renderOverview(); }, "small") : null),
      formOpen ? waiverForm(c) : null);
    return row;
  }

  function waiverForm(c) {
    const st = S.state;
    const name = h("input", { type: "text", value: st.user || "", placeholder: "your name" });
    const reason = h("textarea", { rows: 2, placeholder: "PROD-142: both apps migrated to phone, confirmed with both teams" });
    const days = h("select", [7, 14, 30, 60, 90].filter((d) => d <= st.waivers.maxDays).map((d) => h("option", { value: d, selected: d === 30 }, `${d} days`)));
    return h("div.form",
      h("p.muted", "Writes a waiver for this exact change into ", h("code", st.waivers.file || "waivers.yaml"), ". Commit it in your pull request so reviewers see it."),
      h("label", "Approved by", name),
      h("label", "Why is this break acceptable?", reason),
      h("label", "Expires in", days),
      h("div.actions",
        btn("Add the waiver", () => send("waive", { change: c, approvedBy: name.value, reason: reason.value, days: Number(days.value) })),
        btn("Cancel", () => { S.openForm = null; renderOverview(); }, "secondary")));
  }

  function waiversSection(r) {
    const w = r.waivers;
    const items = [];
    if (w.expired && w.expired.length) {
      items.push(h("div.card.warn", h("h3", "Expired waivers: remove or renew them"), h("p.muted", "They're ignored now, so the change they covered counts again."),
        w.expired.map((x) => h("div.waiver", h("code", x.fingerprint), ` expired ${x.expires} · ${x.reason}`))));
    }
    if (w.applied.length) {
      items.push(h("h2", `Accepted breaks (${w.applied.length})`),
        h("div.list", w.applied.map((x) => h("div.waiver", h("code", x.fingerprint), h("span", ` ${x.reason}`), h("span.muted", ` — ${x.approved_by}, until ${x.expires}`)))));
    }
    if (w.stale.length) {
      items.push(h("div.card.hint", `${w.stale.length} waiver(s) no longer match any change and can be deleted: `, w.stale.map((x) => h("code", x.fingerprint + " "))));
    }
    return h("div", items);
  }

  // --- set up -------------------------------------------------------------------------
  function renderSetup() {
    const busy = S.busy.has("init");
    const d = S.init;
    if (!d) return fill("setup", h("p.muted", busy ? [h("span.spin"), " Looking at your project…"] : "Open this tab to look at the project."));
    if (d.error) return fill("setup", h("div.card.warn", d.error));

    if (d.status === "needs_spec") {
      return fill("setup",
        h("div.banner.blocked", h("div.banner-text", h("h1", "First, your API needs an OpenAPI spec"), h("p.muted", "api-guard compares your API's spec between branches, so it needs one to compare. Nothing was written."))),
        d.stack ? h("p", "Detected: ", h("strong", d.stack)) : null,
        d.note ? h("p.muted", d.note) : null,
        h("h2", "Add one"),
        h("ol.steps", stepItems(d.steps || [])),
        h("div.actions", btn("Check again", () => send("initPreview", { ci: S.ci }), "", busy)));
    }

    const ran = S.initRan && !d.dry_run;
    const check = (on, text) => h("li", h("span.tick." + (on ? "on" : "off"), on ? "✓" : "–"), text);
    const parts = [];
    if (ran) {
      parts.push(h("div.banner.ok", h("div.banner-text", h("h1", "API Guard is set up"),
        h("p.muted", d.written.length ? `Wrote ${d.written.join(", ")}.` : "Nothing new written.", d.kept.length ? ` Kept your ${d.kept.join(", ")}.` : "")),
        h("div.actions", btn("Check now", () => { send("check"); selectTab("overview"); }))));
    } else {
      parts.push(h("h1", "Set up API Guard in this project"), h("p.muted", "Here's what was found. Nothing is written until you press Set up."));
    }

    parts.push(h("div.grid",
      h("div.card", h("h3", "Spec"), h("code", d.spec), h("p.muted", d.spec_exists ? "found" : "not created yet")),
      h("div.card", h("h3", "Framework"), h("p", d.framework || "not recognised"), d.generate_cmd ? h("p.muted", "Generates the spec with ", h("code", d.generate_cmd)) : null),
      h("div.card", h("h3", "Compared with"), h("code", "origin/" + d.base_branch)),
      h("div.card", h("h3", "CI"), ran ? h("p", d.ci.join(", ") || "none") : ciPicker(d))));

    parts.push(h("h2", "What will run"), h("ul.checks",
      check(d.checks.breaking, "Breaking changes: your branch's spec vs the main branch"),
      check(d.checks.freshness, d.checks.freshness ? "Freshness: the spec is regenerated from your code and compared" : "Freshness: off, no way to regenerate the spec was detected"),
      check(true, "Conformance: in the editor whenever your API is running (set runtime.url in api-guard.yaml)")));

    if (!d.spec_exists && d.create_spec_cmd) {
      parts.push(h("div.card.hint", h("h3", "Create the spec once"), h("div.cmd", h("code", d.create_spec_cmd), copyBtn(d.create_spec_cmd))));
    }

    const preview = d.preview || {};
    parts.push(h("h2", ran ? "Files" : "Files it will write"));
    parts.push(h("div.list",
      (d.written || []).map((f) => h("div.file",
        h("div.file-head", h("span.badge.passed", ran ? "written" : "new"), h("code", f), ran ? btn("Open", () => send("openFile", { path: f }), "small secondary") : null),
        preview[f] ? h("details", h("summary", "Preview"), h("pre", preview[f])) : null)),
      (d.kept || []).map((f) => h("div.file", h("div.file-head", h("span.badge.skipped", "exists"), h("code", f), h("span.muted", " kept as is"), btn("Open", () => send("openFile", { path: f }), "small secondary")))),
      (d.gitignore_added || []).length ? h("div.file", h("div.file-head", h("span.badge.passed", ran ? "added" : "new"), h("code", ".gitignore"), h("span.muted", " " + d.gitignore_added.join(", ")))) : null));

    if (d.jenkins_stage) {
      parts.push(h("h2", "Jenkins"), h("p.muted", "Add this stage to your Jenkinsfile:"), h("div.cmd.block", h("pre", d.jenkins_stage), copyBtn(d.jenkins_stage)));
    }

    if (!ran) {
      const force = h("input", { type: "checkbox", checked: S.force, onchange: (e) => (S.force = e.target.checked) });
      parts.push(h("div.actions.bottom",
        btn("Set up API Guard", () => { S.initRan = true; send("initRun", { ci: S.ci, force: S.force }); }, "", busy),
        (d.kept || []).length ? h("label.inline", force, " Replace files that already exist") : null));
    } else {
      parts.push(h("div.actions.bottom", btn("Look again", () => { S.initRan = false; send("initPreview", { ci: S.ci }); }, "secondary", busy)));
    }
    fill("setup", parts);
  }

  function ciPicker(d) {
    const sel = h("select", { onchange: (e) => { S.ci = e.target.value; send("initPreview", { ci: S.ci }); } },
      [["auto", `Detected (${d.ci.join(", ") || "none"})`], ["github", "GitHub Actions"], ["jenkins", "Jenkins"], ["none", "None"]].map(([v, t]) => h("option", { value: v, selected: v === S.ci }, t)));
    return sel;
  }

  function stepItems(lines) {
    // "$ " marks a command; text lines start a new step.
    const out = [];
    let current = null;
    for (const line of lines) {
      if (line.startsWith("$ ")) {
        const cmd = line.slice(2);
        if (!current) out.push((current = h("li")));
        current.append(h("div.cmd", h("code", cmd), copyBtn(cmd)));
      } else {
        out.push((current = h("li", line)));
      }
    }
    out.push(h("li", "Then press Check again: API Guard will find the spec and finish the setup."));
    return out;
  }

  // --- ask ----------------------------------------------------------------------------
  function renderAsk() {
    const st = S.state;
    if (!st) return fill("ask", h("p.muted", "Loading…"));
    const ai = st.ai;
    const keyRow = ai.hasKey
      ? h("div.card.slim", h("span.tick.on", "✓"), " Groq key saved in VS Code ", btn("Change", () => send("setKey"), "small secondary"), btn("Remove", () => send("clearKey"), "small secondary"))
      : h("div.card.hint", h("h3", "Add your free Groq key"), h("p", "Our agent runs on a free Groq key: create one at console.groq.com/keys, then paste it into VS Code's prompt. It's stored encrypted on this machine and only passed to api-guard."), btn("Add your Groq key", () => send("setKey")));
    const where = h("p.muted", "Reads builds from Jenkins at ", h("code", ai.jenkinsUrl || "(not set)"), ai.jenkinsJob ? [" job ", h("code", ai.jenkinsJob)] : null, " · ", h("a", { href: "#", onclick: (e) => (e.preventDefault(), send("settings")) }, "change"));

    const log = h("div.chat", S.chat.length ? S.chat.map(chatItem) : h("p.muted", "Ask about past builds: why one failed, what changed, which waivers expire soon. The agent looks things up with read-only tools; it can't change anything."));
    const input = h("textarea", { rows: 2, value: S.draft || "", placeholder: "Why did build 42 fail?", oninput: (e) => (S.draft = e.target.value), onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); } } });
    const busy = S.busy.has("ask");
    const go = () => { const q = input.value.trim(); if (q && !busy) { send("ask", { question: q }); input.value = S.draft = ""; } };
    const ideas = ["Why did the last build fail?", "Which waivers expire soon?", "What is waiting for approval, and why?"];
    fill("ask",
      h("h1", "Ask our agent"), keyRow, where, log,
      h("div.composer", input, btn("Ask", go, "", busy)),
      h("div.chips", ideas.map((q) => h("button.chip", { onclick: () => { input.value = S.draft = q; input.focus(); } }, q))),
      S.chat.length ? h("div.actions", btn("Clear the conversation", () => { S.chat = []; keep(); renderAsk(); }, "small secondary")) : null);
    log.scrollTop = log.scrollHeight;
  }

  function chatItem(item) {
    const d = item.data;
    return h("div.turn",
      h("div.q", item.question),
      item.pending ? h("div.a.muted", h("span.spin"), " Investigating…")
        : d.error ? h("div.a.err", d.error)
        : h("div.a",
            d.steps && d.steps.length ? h("div.steps", "Looked at: ", d.steps.map((s) => h("span.chip.static", s))) : null,
            h("div.answer", d.text),
            h("div.conf", "Model's confidence: ", h("strong", d.confidence || "not stated"), d.confidence_reason ? ` — ${d.confidence_reason}` : ""),
            (d.warnings || []).map((w) => h("div.warning", "Check: " + w)),
            h("div.caution", d.caution)));
  }

  // --- reviews ------------------------------------------------------------------------
  function renderReviews() {
    const busyList = S.busy.has("reviews");
    const build = h("input", { type: "text", placeholder: "build #", size: 6 });
    const parts = [
      h("h1", "Reviews"),
      h("p.muted", "A blocked build can wait for a person: approve it (with a waiver to commit), reject it, or ask the agent first. Rules decide what's blocked; only a person approves."),
      h("div.toolbar",
        btn("Refresh", () => send("reviews"), "secondary", busyList),
        h("span.sep"), "Review a Jenkins build: ", build,
        btn("Start review", () => send("reviewBuild", { build: build.value }), "", S.busy.has("reviewBuild"))),
    ];
    const data = S.reviews;
    if (data && data.error) parts.push(h("div.card.warn", data.error));
    else if (data) {
      parts.push(data.reviews.length
        ? h("table.reviews", h("thead", h("tr", ["Review", "Status", "Verdict", "Risk", "Changes", "Commit", "Updated"].map((t) => h("th", t)))),
            h("tbody", data.reviews.map((r) => h("tr" + (S.review && S.review.id === r.review_id ? ".sel" : ""), { onclick: () => send("reviewShow", { id: r.review_id }) },
              h("td", h("code", r.review_id)), h("td", statusBadge(r.status)), h("td", r.verdict), h("td", r.band || "–"),
              h("td", String(r.changes)), h("td", h("code", r.commit || "–")), h("td.muted", (r.updated || "").slice(0, 16).replace("T", " "))))))
        : h("p.muted", "No reviews in this project yet. Start one from a Jenkins build above, or run `api-guard review` in CI."));
    }
    if (S.review) parts.push(reviewDetail(S.review.id, S.review.data));
    fill("reviews", parts);
  }

  function reviewDetail(id, d) {
    if (d.error) return h("div.card.warn", d.error);
    const busy = S.busy.has(`decide:${id}`);
    const outcome = S.decided[id];
    const parts = [
      h("div.detail-head", h("h2", `Review ${id}`), statusBadge(d.status)),
      h("p.muted", d.full_commit ? ["commit ", h("code", d.full_commit.slice(0, 12))] : null, d.branch ? [" · branch ", h("code", d.branch)] : null, ` · ${d.checkpoints} saved step(s)`),
      h("h3", `What the rules found (verdict: ${d.verdict})`),
      h("div.list", d.change_list.map((c) => h("div.change", h("div.change-head", h("span.sev." + (c.severity || "ERR"), c.severity || "ERR"), h("code", [c.operation, c.path].filter(Boolean).join(" "))), h("p", c.text), h("div.meta", "fingerprint ", h("code", c.fingerprint || "–"))))),
    ];
    if (d.band || d.impact) {
      parts.push(h("div.card.model",
        h("div.model-tag", "Written by the model, advisory only"),
        d.band ? h("p", h("strong", "Risk: "), d.band, d.rationale ? ` — ${d.rationale}` : "") : null,
        d.impact ? h("p", h("strong", "What breaks: "), d.impact) : null,
        d.migration ? h("p", h("strong", "Safer route: "), d.migration) : null));
    }
    if (d.qa.length) {
      parts.push(h("h3", "Questions"), d.qa.map((q) => h("div.turn", h("div.q", q.question), h("div.a", q.tools.length ? h("div.steps", "Looked at: ", q.tools.map((t) => h("span.chip.static", t))) : null, h("div.answer", q.answer)))));
    }

    if (d.status === "waiting") {
      const name = h("input", { type: "text", value: S.state ? S.state.user : "", placeholder: "your name" });
      const reason = h("textarea", { rows: 2, placeholder: "Why it's acceptable (approve), or why not (reject)" });
      const max = S.state ? S.state.waivers.maxDays : 90;
      const days = h("select", [7, 14, 30, 60, 90].filter((n) => n <= max).map((n) => h("option", { value: n, selected: n === 30 }, `waiver for ${n} days`)));
      const question = h("input", { type: "text", placeholder: "Does this break the mobile app?" });
      parts.push(h("div.card.decide",
        h("h3", "Decide"),
        h("label", "Your name", name),
        h("label", "Reason", reason),
        h("div.actions",
          btn("Approve", () => send("decide", { action: "approve", id, by: name.value, reason: reason.value, days: Number(days.value) }), "", busy), days,
          btn("Reject", () => send("decide", { action: "reject", id, by: name.value, reason: reason.value }), "danger", busy)),
        d.questions_left > 0
          ? h("div.ask-row", h("label", `Not sure yet? Ask the agent (${d.questions_left} left)`, question), btn("Ask", () => send("decide", { action: "ask", id, question: question.value }), "secondary", busy))
          : h("p.muted", "No questions left: approve or reject.")));
    } else if (d.decision === "approve") {
      parts.push(h("div.card.ok", h("h3", `Approved by ${d.decided_by}`), h("p", d.reason), h("p.muted", `Waiver expires ${d.expires}`)));
      const snippet = (outcome && outcome.waiver_snippet) || d.waiver_snippet;
      if (snippet) {
        parts.push(h("div.card", h("h3", "Waiver to commit"), h("p.muted", "So the next build passes without another approval."), h("pre", snippet),
          h("div.actions", btn("Add to waivers.yaml", () => send("applySnippet", { snippet })), copyBtn(snippet))));
      }
    } else if (d.decision === "reject") {
      parts.push(h("div.card.warn", h("h3", `Rejected by ${d.decided_by}`), h("p", d.reason),
        d.checklist.length ? h("ol", d.checklist.map((c) => h("li", c))) : null));
    }
    return h("div.detail", parts);
  }

  // --- messages from the extension ---------------------------------------------------
  function toast(kind, text) {
    const el = document.getElementById("toast");
    el.className = "show " + kind;
    el.textContent = text;
    clearTimeout(toast.t);
    toast.t = setTimeout(() => (el.className = ""), kind === "error" ? 8000 : 3500);
  }

  function renderAll() {
    renderOverview();
    renderSetup();
    renderAsk();
    renderReviews();
  }

  window.addEventListener("message", (event) => {
    const m = event.data;
    switch (m.type) {
      case "state":
        S.state = m;
        if (S.openForm && !(m.result && m.result.changes.some((c) => c.fingerprint === S.openForm))) S.openForm = null;
        renderOverview();
        renderAsk();
        return;
      case "tab":
        selectTab(m.tab);
        return;
      case "busy": {
        m.on ? S.busy.add(m.what) : S.busy.delete(m.what);
        const tab = m.what === "init" ? "setup" : m.what === "ask" ? "ask" : "reviews";
        ({ setup: renderSetup, ask: renderAsk, reviews: renderReviews })[tab]();
        return;
      }
      case "toast":
        toast(m.kind, m.text);
        return;
      case "waived":
        S.openForm = null;
        toast("info", `Waiver added to ${m.file} until ${m.expires}. Commit it in your pull request.`);
        return;
      case "init":
        S.init = m.data;
        if (!m.ran) S.initRan = false;
        renderSetup();
        return;
      case "asking":
        S.chat.push({ question: m.question, pending: true });
        renderAsk();
        return;
      case "answer": {
        const item = [...S.chat].reverse().find((c) => c.pending && c.question === m.question);
        if (item) { item.pending = false; item.data = m.data; } else S.chat.push({ question: m.question, data: m.data });
        keep();
        renderAsk();
        return;
      }
      case "reviews":
        S.reviews = m.data;
        renderReviews();
        return;
      case "review":
        S.review = { id: m.id, data: m.data };
        renderReviews();
        return;
      case "decided":
        S.decided[m.id] = m.data;
        if (m.action === "ask") toast("info", "Answered: see Questions.");
        return;
    }
  });

  renderAll();
  selectTab(S.tab);
  send("ready");
})();
