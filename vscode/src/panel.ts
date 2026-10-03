// The API Guard panel: one window with every feature, as a page in VS Code.
//
//   Overview  the headline, each check, every change with "Show in spec" and
//             "Accept this break" (writes the waiver), waivers to clean up
//   Set up    api-guard init, visually: what was detected, what would be
//             written (previewed), the CI to set up, or the steps to add a spec
//   Ask       our agent on the person's free Groq key, about past builds
//   Reviews   saved reviews: approve (and add the waiver), reject, or ask
//
// The page (media/panel.js) only draws and sends messages; everything runs
// here, through the same api-guard commands CI uses, with --json. The Groq
// key lives in VS Code's secret storage, typed by the person into VS Code's
// own prompt; it reaches api-guard as an environment variable, never in a
// command line, a log or the page.

import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { aiEnv } from "./args";
import { readConfig } from "./config";
import { Change, RunResult, headline, blockingChanges } from "./result";
import { RunOutput, RunnerSettings, resolveRunner, runApiGuard } from "./runner";
import { appendSnippet, appendWaiver, validateReason, waivedFingerprints, waiverEntry } from "./waiver";

export const KEY_SECRET = "apiGuard.groqApiKey";

export type Tab = "overview" | "setup" | "ask" | "reviews";

/** What the panel needs from the rest of the extension. */
export interface PanelHost {
  context: vscode.ExtensionContext;
  root(): string | undefined;
  runner(): RunnerSettings;
  setting<T>(key: string): T;
  lastResult(): RunResult | undefined;
  lastProblem(): string | undefined;
  checking(): boolean;
  check(): Promise<void>;
  reveal(change: Change): Promise<void>;
  openReport(): Promise<void>;
  showLog(): void;
  gitUserName(cwd: string): Promise<string>;
  log(text: string): void;
}

type Msg = { type: string; [key: string]: any };

export class GuardPanel {
  static current: GuardPanel | undefined;
  private busy = new Set<string>();
  /** For tests: every message sent to the page. */
  readonly sent: Msg[] = [];

  static show(host: PanelHost, tab: Tab = "overview"): GuardPanel {
    if (GuardPanel.current) {
      GuardPanel.current.panel.reveal(vscode.ViewColumn.Active);
      GuardPanel.current.post({ type: "tab", tab });
      return GuardPanel.current;
    }
    const media = vscode.Uri.joinPath(host.context.extensionUri, "media");
    const panel = vscode.window.createWebviewPanel("apiGuard.panel", "API Guard", vscode.ViewColumn.Active, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [media],
    });
    panel.iconPath = vscode.Uri.joinPath(media, "shield.svg");
    GuardPanel.current = new GuardPanel(host, panel, tab);
    return GuardPanel.current;
  }

  private constructor(
    private readonly host: PanelHost,
    readonly panel: vscode.WebviewPanel,
    private tab: Tab,
  ) {
    panel.webview.html = this.html();
    panel.onDidDispose(() => {
      if (GuardPanel.current === this) GuardPanel.current = undefined;
    });
    panel.webview.onDidReceiveMessage((msg: Msg) => void this.handle(msg));
  }

  post(msg: Msg): void {
    this.sent.push(msg);
    void this.panel.webview.postMessage(msg);
  }

  /** The extension calls this whenever a check finishes or starts. */
  async refresh(): Promise<void> {
    this.post({ type: "state", ...(await this.state()) });
  }

  private async state() {
    const root = this.host.root();
    const result = this.host.lastResult();
    const config = readConfig(root ? readText(path.join(root, "api-guard.yaml")) : null);
    const waiversText = root && config.waiversFile ? readText(path.join(root, config.waiversFile)) ?? "" : "";
    const blocking = new Set(result ? blockingChanges(result) : []);
    return {
      root: root ?? null,
      checking: this.host.checking(),
      problem: this.host.lastProblem() ?? null,
      result: result ?? null,
      headline: result ? headline(result) : null,
      blocking: result ? result.changes.map((c) => blocking.has(c)) : [],
      configured: config.exists,
      waivers: {
        file: config.waiversFile,
        maxDays: config.maxWaiverDays,
        already: [...waivedFingerprints(waiversText)],
      },
      ai: {
        hasKey: Boolean(await this.host.context.secrets.get(KEY_SECRET)),
        jenkinsUrl: this.host.setting<string>("jenkinsUrl"),
        jenkinsJob: this.host.setting<string>("jenkinsJob"),
        image: this.host.setting<string>("aiDockerImage"),
      },
      user: root ? await this.host.gitUserName(root) : "",
    };
  }

  // --- messages from the page ------------------------------------------------------

  async handle(msg: Msg): Promise<void> {
    const root = this.host.root();
    try {
      switch (msg.type) {
        case "ready":
          await this.refresh();
          this.post({ type: "tab", tab: this.tab });
          return;
        case "check":
          await this.host.check();
          return this.refresh();
        case "reveal":
          return this.host.reveal(msg.change);
        case "openReport":
          return this.host.openReport();
        case "log":
          return this.host.showLog();
        case "openFile":
          if (root) await vscode.window.showTextDocument(vscode.Uri.file(path.join(root, msg.path)));
          return;
        case "copy":
          await vscode.env.clipboard.writeText(String(msg.text));
          return this.toast("info", "Copied.");
        case "settings":
          await vscode.commands.executeCommand("workbench.action.openSettings", "apiGuard");
          return;
        case "waive":
          return this.waive(msg);
        case "initPreview":
        case "initRun":
          return this.init(msg);
        case "setKey":
          return this.setKey();
        case "clearKey":
          await this.host.context.secrets.delete(KEY_SECRET);
          this.toast("info", "Groq key removed from VS Code.");
          return this.refresh();
        case "ask":
          return this.ask(String(msg.question ?? ""));
        case "reviews":
          return this.reviews();
        case "reviewShow":
          return this.reviewShow(String(msg.id));
        case "reviewBuild":
          return this.reviewBuild(String(msg.build ?? ""));
        case "decide":
          return this.decide(msg);
        case "applySnippet":
          return this.applySnippet(String(msg.snippet ?? ""));
      }
    } catch (error) {
      this.toast("error", error instanceof Error ? error.message : String(error));
    }
  }

  private toast(kind: "info" | "error", text: string): void {
    this.post({ type: "toast", kind, text });
  }

  /** Run api-guard with --json, marking `what` busy on the page meanwhile. */
  private async run(what: string, args: string[], ai = false): Promise<any> {
    const root = this.host.root();
    if (!root) throw new Error("Open a project folder first.");
    if (this.busy.has(what)) return undefined;
    this.busy.add(what);
    this.post({ type: "busy", what, on: true });
    let out: RunOutput;
    try {
      let options = {};
      if (ai) {
        const kind = await resolveRunner(this.host.runner(), root);
        options = {
          image: this.host.setting<string>("aiDockerImage"),
          env: aiEnv(kind, {
            groqKey: await this.host.context.secrets.get(KEY_SECRET),
            jenkinsUrl: this.host.setting<string>("jenkinsUrl"),
            jenkinsJob: this.host.setting<string>("jenkinsJob"),
          }),
        };
      }
      this.host.log(`\n[${new Date().toLocaleTimeString()}] api-guard ${args[0]} (panel)`);
      out = await runApiGuard(this.host.runner(), root, args, options);
    } finally {
      this.busy.delete(what);
      this.post({ type: "busy", what, on: false });
    }
    this.host.log(`$ ${out.command}\n${out.stderr}`);
    try {
      return JSON.parse(out.stdout);
    } catch {
      const last = (out.stderr || out.stdout).trim().split("\n").filter(Boolean).pop() ?? "no output";
      throw new Error(last.replace(/^api-guard:\s*/, ""));
    }
  }

  // --- overview: accept a break --------------------------------------------------------

  private async waive(msg: Msg): Promise<void> {
    const root = this.host.root()!;
    const change = msg.change as Change;
    const reasonProblem = validateReason(String(msg.reason ?? ""));
    if (!String(msg.approvedBy ?? "").trim()) return this.toast("error", "A waiver needs your name.");
    if (reasonProblem) return this.toast("error", reasonProblem);

    const configFile = path.join(root, "api-guard.yaml");
    const configText = readText(configFile);
    const config = readConfig(configText);
    let waiversRel = config.waiversFile;
    if (!waiversRel) {
      // The config exists without a waivers file: add the usual one.
      if (configText === null) return;
      const updated = /^policy:\s*$/m.test(configText)
        ? configText.replace(/^policy:\s*$/m, "policy:\n  waivers: waivers.yaml")
        : configText.replace(/\s*$/, "\n\npolicy:\n  waivers: waivers.yaml\n");
      fs.writeFileSync(configFile, updated, "utf-8");
      waiversRel = "waivers.yaml";
    }
    const days = Math.min(Number(msg.days) || 30, config.maxWaiverDays);
    const file = path.join(root, waiversRel);
    const existing = readText(file) ?? "";
    if (waivedFingerprints(existing).has(change.fingerprint ?? "")) {
      return this.toast("info", `${change.fingerprint} is already waived in ${waiversRel}.`);
    }
    const entry = waiverEntry(change, { approvedBy: msg.approvedBy, reason: msg.reason, days });
    fs.writeFileSync(file, appendWaiver(existing, entry), "utf-8");
    this.host.log(`waiver added to ${waiversRel} for ${change.fingerprint} (expires ${entry.expires})`);
    this.post({ type: "waived", fingerprint: change.fingerprint, file: waiversRel, expires: entry.expires });
    await this.host.check();
    await this.refresh();
  }

  // --- set up ----------------------------------------------------------------------

  private async init(msg: Msg): Promise<void> {
    const ci = ["auto", "github", "jenkins", "none"].includes(msg.ci) ? msg.ci : "auto";
    const args = ["init", "--json", "--ci", ci];
    if (msg.type === "initPreview") args.push("--dry-run");
    if (msg.type === "initRun" && msg.force) args.push("--force");
    const data = await this.run("init", args);
    if (!data) return;
    this.post({ type: "init", data, ran: msg.type === "initRun" });
    if (msg.type === "initRun" && data.status === "set_up") {
      await this.host.check();
      await this.refresh();
    }
  }

  // --- ask -------------------------------------------------------------------------

  private async setKey(): Promise<void> {
    const key = await vscode.window.showInputBox({
      title: "Groq API key for API Guard",
      prompt: "Free at console.groq.com/keys. Stored encrypted in VS Code's secret storage, only on this machine.",
      password: true,
      ignoreFocusOut: true,
      validateInput: (v) => (v.trim().length >= 20 ? null : "That doesn't look like a Groq key."),
    });
    if (!key) return;
    await this.host.context.secrets.store(KEY_SECRET, key.trim());
    this.toast("info", "Groq key saved in VS Code.");
    await this.refresh();
  }

  private async ask(question: string): Promise<void> {
    if (!question.trim()) return;
    if (!(await this.host.context.secrets.get(KEY_SECRET))) {
      return this.toast("error", "Add your Groq key first (the key button above).");
    }
    this.post({ type: "asking", question });
    const data = await this.run("ask", ["ask", question, "--json"], true).catch((e: Error) => ({
      error: e.message,
    }));
    if (data) this.post({ type: "answer", question, data });
  }

  // --- reviews ---------------------------------------------------------------------

  private async reviews(): Promise<void> {
    const data = await this.run("reviews", ["review", "list", "--json"], true);
    if (data) this.post({ type: "reviews", data });
  }

  private async reviewShow(id: string): Promise<void> {
    const data = await this.run(`review:${id}`, ["review", "show", id, "--json"], true);
    if (data) this.post({ type: "review", id, data });
  }

  private async reviewBuild(build: string): Promise<void> {
    if (!/^\d+$/.test(build.trim())) return this.toast("error", "A Jenkins build number, like 42.");
    const data = await this.run("reviewBuild", ["review", "--build", build.trim(), "--json"], true);
    if (!data) return;
    if (data.error) return this.toast("error", data.error);
    await this.reviews();
    await this.reviewShow(data.review_id);
  }

  private async decide(msg: Msg): Promise<void> {
    const id = String(msg.id);
    let args: string[];
    if (msg.action === "ask") {
      if (!String(msg.question ?? "").trim()) return;
      args = ["ask-review", id, String(msg.question), "--json"];
    } else {
      const by = String(msg.by ?? "").trim();
      const reason = String(msg.reason ?? "").trim();
      if (!by) return this.toast("error", "Who is deciding? Fill in your name.");
      const problem = validateReason(reason);
      if (problem) return this.toast("error", problem);
      args = [msg.action === "approve" ? "approve" : "reject", id, "--by", by, "--reason", reason, "--json"];
      if (msg.action === "approve") args.push("--expires-in", String(Number(msg.days) || 30));
    }
    const data = await this.run(`decide:${id}`, args, true);
    if (!data) return;
    if (data.error) return this.toast("error", data.error);
    this.post({ type: "decided", id, action: msg.action, data });
    await this.reviews();
    await this.reviewShow(id);
  }

  private async applySnippet(snippet: string): Promise<void> {
    const root = this.host.root()!;
    const rel = readConfig(readText(path.join(root, "api-guard.yaml"))).waiversFile ?? "waivers.yaml";
    const file = path.join(root, rel);
    const { text, added, skipped } = appendSnippet(readText(file) ?? "", snippet);
    if (added.length) fs.writeFileSync(file, text, "utf-8");
    this.toast(
      "info",
      added.length
        ? `Added ${added.length} waiver(s) to ${rel}. Commit it in the pull request.`
        : `Already in ${rel}: ${skipped.join(", ")}.`,
    );
    await vscode.window.showTextDocument(vscode.Uri.file(file));
    await this.host.check();
    await this.refresh();
  }

  // --- the page --------------------------------------------------------------------

  private html(): string {
    const webview = this.panel.webview;
    const media = vscode.Uri.joinPath(this.host.context.extensionUri, "media");
    const script = webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.css"));
    const nonce = [...Array(32)].map(() => Math.floor(Math.random() * 36).toString(36)).join("");
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${webview.cspSource} data:; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="${style}" rel="stylesheet">
<title>API Guard</title>
</head>
<body>
<nav class="tabs" role="tablist">
  <button role="tab" data-tab="overview">Overview</button>
  <button role="tab" data-tab="setup">Set up</button>
  <button role="tab" data-tab="ask">Ask</button>
  <button role="tab" data-tab="reviews">Reviews</button>
</nav>
<main>
  <section id="overview" role="tabpanel"></section>
  <section id="setup" role="tabpanel"></section>
  <section id="ask" role="tabpanel"></section>
  <section id="reviews" role="tabpanel"></section>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
  }
}

function readText(file: string): string | null {
  try {
    return fs.readFileSync(file, "utf-8");
  } catch {
    return null;
  }
}
