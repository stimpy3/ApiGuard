// API Guard for VS Code: the editor front end for api-guard.
//
// It runs the same api-guard CI runs (the installed command or the Docker
// image) and shows the result where the developer already is: a status bar
// headline, squiggles on the spec, the Problems panel, a sidebar, a guided
// setup, and a quick fix that writes the waiver when a break is intended.
// Nothing here decides anything; api-guard's result.json is the answer.

import * as fs from "fs";
import * as path from "path";
import { execFile } from "child_process";
import * as vscode from "vscode";
import { readConfig } from "./config";
import { locate } from "./locate";
import { blockingChanges, Change, headline, parseResult, RunResult, statusText } from "./result";
import { checkArgs } from "./args";
import { resolveRunner, RunnerMissing, RunnerSettings, runApiGuard } from "./runner";
import { Located, ResultsTree } from "./tree";
import { appendWaiver, validateReason, waivedFingerprints, waiverEntry } from "./waiver";

const SOURCE = "api-guard";
const WATCHED = /(^|[\\/])(openapi|swagger)\.(ya?ml|json)$|(^|[\\/])(api-guard|waivers)\.ya?ml$/i;

let output: vscode.OutputChannel;
let diagnostics: vscode.DiagnosticCollection;
let status: vscode.StatusBarItem;
let tree: ResultsTree;
let located: Located[] = [];
let lastResult: RunResult | undefined;
let running: Promise<void> | undefined;
let rerun = false;

export function activate(context: vscode.ExtensionContext): void {
  output = vscode.window.createOutputChannel("API Guard");
  diagnostics = vscode.languages.createDiagnosticCollection(SOURCE);
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  status.command = "apiGuard.check";
  status.text = "$(shield) API Guard";
  status.tooltip = "Check the API contract";
  status.show();
  tree = new ResultsTree();

  context.subscriptions.push(
    output,
    diagnostics,
    status,
    vscode.window.registerTreeDataProvider("apiGuard.results", tree),
    vscode.commands.registerCommand("apiGuard.check", () => check(true)),
    vscode.commands.registerCommand("apiGuard.init", init),
    vscode.commands.registerCommand("apiGuard.acceptBreak", acceptBreak),
    vscode.commands.registerCommand("apiGuard.openReport", openReport),
    vscode.commands.registerCommand("apiGuard.showOutput", () => output.show()),
    vscode.languages.registerCodeActionsProvider(
      [{ language: "yaml" }, { language: "json" }, { pattern: "**/*.{yaml,yml,json}" }],
      new AcceptBreakActions(),
      { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] },
    ),
    vscode.workspace.onDidSaveTextDocument((doc) => {
      if (setting<boolean>("checkOnSave") && WATCHED.test(doc.uri.fsPath)) {
        void check(false);
      }
    }),
  );

  // A first look on opening, when the project is already set up or has a spec.
  void check(false);
}

export function deactivate(): void {}

// --- helpers --------------------------------------------------------------------

function setting<T>(key: string): T {
  return vscode.workspace.getConfiguration("apiGuard").get<T>(key) as T;
}

function runnerSettings(): RunnerSettings {
  return {
    runner: setting("runner"),
    cliPath: setting("cliPath"),
    dockerImage: setting("dockerImage"),
  };
}

function projectRoot(): string | undefined {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders?.length) {
    return undefined;
  }
  // The folder that has api-guard.yaml, or else the first one.
  const configured = folders.find((f) => fs.existsSync(path.join(f.uri.fsPath, "api-guard.yaml")));
  return (configured ?? folders[0]).uri.fsPath;
}

function readText(file: string): string | null {
  try {
    return fs.readFileSync(file, "utf-8");
  } catch {
    return null;
  }
}

function log(text: string): void {
  output.appendLine(text.replace(/\s+$/, ""));
}

// --- check ----------------------------------------------------------------------

/** Run `api-guard check` and show the result. Calls during a run queue one rerun. */
async function check(manual: boolean): Promise<void> {
  if (running) {
    rerun = true;
    return running;
  }
  running = doCheck(manual).finally(() => {
    running = undefined;
    if (rerun) {
      rerun = false;
      void check(false);
    }
  });
  return running;
}

async function doCheck(manual: boolean): Promise<void> {
  const root = projectRoot();
  if (!root) {
    if (manual) {
      void vscode.window.showWarningMessage("API Guard: open a project folder first.");
    }
    return;
  }
  const config = readConfig(readText(path.join(root, "api-guard.yaml")));
  const resultFile = path.join(root, config.reportDir, "result.json");
  const started = Date.now();

  status.text = "$(sync~spin) API: checking";
  log(`\n[${new Date().toLocaleTimeString()}] api-guard check`);
  // Conformance runs only if the API is up (see args.ts); otherwise it's
  // "not checked", never an error or a long wait.
  let out;
  try {
    const kind = await resolveRunner(runnerSettings(), root);
    out = await runApiGuard(runnerSettings(), root, checkArgs(kind, config.runtimeUrl));
  } catch (error) {
    return showProblem(error instanceof RunnerMissing ? error.message : String(error), manual);
  }
  log(`$ ${out.command}\n${out.stdout}${out.stderr}`);

  const fresh = fs.existsSync(resultFile) && fs.statSync(resultFile).mtimeMs >= started - 2000;
  if (!fresh) {
    // api-guard stopped before writing a result: no spec, a bad config, ...
    const reason = (out.stderr || out.stdout).trim().split("\n").filter(Boolean).pop() ?? "no result";
    return showProblem(reason.replace(/^api-guard:\s*/, ""), manual);
  }

  let result: RunResult;
  try {
    result = parseResult(fs.readFileSync(resultFile, "utf-8"));
  } catch (error) {
    return showProblem(`Couldn't read ${resultFile}: ${error}`, manual);
  }
  show(root, config.waiversFile, result);
  if (manual) {
    const message = headline(result);
    if (result.verdict === "passed") {
      void vscode.window.showInformationMessage(message);
    } else {
      void vscode.window.showWarningMessage(message, "Open the report").then((pick) => {
        if (pick) void openReport();
      });
    }
  }
}

function showProblem(message: string, manual: boolean): void {
  status.text = "$(warning) API: can't check";
  status.tooltip = message;
  tree.showProblem(message);
  diagnostics.clear();
  located = [];
  if (manual) {
    void vscode.window.showWarningMessage(`API Guard: ${message}`, "Show the log", "Set up API Guard").then((pick) => {
      if (pick === "Show the log") output.show();
      if (pick === "Set up API Guard") void init();
    });
  }
}

function show(root: string, waiversFile: string | null, result: RunResult): void {
  lastResult = result;
  status.text = statusText(result);
  status.tooltip = headline(result) + "\nClick to check again";
  diagnostics.clear();
  located = [];

  const specRel = result.meta.spec ?? "openapi.yaml";
  const specUri = vscode.Uri.file(path.join(root, specRel));
  const specText = readText(specUri.fsPath) ?? "";
  const lines = specText.split(/\r?\n/);
  const blocking = new Set(blockingChanges(result));
  const specDiags: vscode.Diagnostic[] = [];

  for (const change of result.changes) {
    const loc = locate(specText, change.path, change.operation);
    const end = loc.endCol > loc.startCol ? loc.endCol : (lines[loc.line] ?? "").length;
    const range = new vscode.Range(loc.line, loc.startCol, loc.line, end);
    const severity = blocking.has(change)
      ? vscode.DiagnosticSeverity.Error
      : change.severity === "INFO"
        ? vscode.DiagnosticSeverity.Information
        : vscode.DiagnosticSeverity.Warning;
    const where = loc.exact ? "" : ` (${change.operation ?? ""} ${change.path ?? ""} is no longer in the spec)`;
    const diag = new vscode.Diagnostic(range, `${change.text}${where}`, severity);
    diag.source = SOURCE;
    diag.code = change.id;
    specDiags.push(diag);
    located.push({ change, uri: specUri, range });
  }

  for (const c of result.checks) {
    if (c.name !== "breaking" && (c.status === "failed" || c.status === "error")) {
      const first = (c.detail ?? "").split("\n").find((l) => l.trim()) ?? "";
      const diag = new vscode.Diagnostic(
        new vscode.Range(0, 0, 0, (lines[0] ?? "").length),
        `${c.name}: ${c.summary}${first ? ` - ${first.trim()}` : ""}`,
        c.status === "failed" ? vscode.DiagnosticSeverity.Error : vscode.DiagnosticSeverity.Warning,
      );
      diag.source = SOURCE;
      specDiags.push(diag);
    }
  }
  diagnostics.set(specUri, specDiags);

  const expired = result.waivers.expired ?? [];
  if (expired.length && waiversFile) {
    const waiversUri = vscode.Uri.file(path.join(root, waiversFile));
    const waiverLines = (readText(waiversUri.fsPath) ?? "").split(/\r?\n/);
    diagnostics.set(
      waiversUri,
      expired.map((w) => {
        const line = Math.max(0, waiverLines.findIndex((l) => l.includes(w.fingerprint)));
        const diag = new vscode.Diagnostic(
          new vscode.Range(line, 0, line, (waiverLines[line] ?? "").length),
          `Expired ${w.expires}: this waiver is ignored now. Remove it, or renew it with a fresh reason.`,
          vscode.DiagnosticSeverity.Warning,
        );
        diag.source = SOURCE;
        return diag;
      }),
    );
  }

  tree.show(result, located);
}

// --- accept a break (write the waiver) -----------------------------------------------

class AcceptBreakActions implements vscode.CodeActionProvider {
  provideCodeActions(
    document: vscode.TextDocument,
    _range: vscode.Range,
    context: vscode.CodeActionContext,
  ): vscode.CodeAction[] {
    const actions: vscode.CodeAction[] = [];
    for (const diag of context.diagnostics) {
      if (diag.source !== SOURCE) continue;
      const hit = located.find(
        (l) =>
          l.uri.fsPath === document.uri.fsPath &&
          l.range.isEqual(diag.range) &&
          l.change.fingerprint &&
          diag.message.startsWith(l.change.text),
      );
      if (!hit) continue;
      const action = new vscode.CodeAction(
        `Accept this break: add a waiver for ${hit.change.fingerprint}`,
        vscode.CodeActionKind.QuickFix,
      );
      action.diagnostics = [diag];
      action.command = { command: "apiGuard.acceptBreak", title: action.title, arguments: [hit.change] };
      actions.push(action);
    }
    return actions;
  }
}

function gitUserName(cwd: string): Promise<string> {
  return new Promise((resolve) => {
    execFile("git", ["config", "user.name"], { cwd, windowsHide: true }, (_e, stdout) =>
      resolve(String(stdout ?? "").trim()),
    );
  });
}

async function acceptBreak(change?: Change): Promise<void> {
  const root = projectRoot();
  if (!root || !change?.fingerprint) {
    void vscode.window.showWarningMessage("API Guard: pick a breaking change in the spec (lightbulb) to accept it.");
    return;
  }
  const configFile = path.join(root, "api-guard.yaml");
  const configText = readText(configFile);
  const config = readConfig(configText);

  let waiversRel = config.waiversFile;
  if (!waiversRel) {
    const pick = await vscode.window.showWarningMessage(
      "This project's api-guard.yaml doesn't use a waivers file yet. Add `waivers: waivers.yaml` to it?",
      { modal: true },
      "Add it",
    );
    if (pick !== "Add it" || configText === null) return;
    const updated = /^policy:\s*$/m.test(configText)
      ? configText.replace(/^policy:\s*$/m, "policy:\n  waivers: waivers.yaml")
      : configText.replace(/\s*$/, "\n\npolicy:\n  waivers: waivers.yaml\n");
    fs.writeFileSync(configFile, updated, "utf-8");
    waiversRel = "waivers.yaml";
  }
  const waiversFile = path.join(root, waiversRel);
  const existing = readText(waiversFile) ?? "";
  if (waivedFingerprints(existing).has(change.fingerprint)) {
    void vscode.window.showInformationMessage(`${change.fingerprint} is already waived in ${waiversRel}.`);
    return;
  }

  const approvedBy = await vscode.window.showInputBox({
    title: `Accept: ${change.text}`,
    prompt: "Your name, recorded as approved_by",
    value: await gitUserName(root),
    validateInput: (v) => (v.trim() ? null : "A waiver needs a name."),
  });
  if (!approvedBy) return;
  const reason = await vscode.window.showInputBox({
    title: `Accept: ${change.text}`,
    prompt: "Why is this break acceptable? (reviewers read this in the pull request)",
    placeHolder: "PROD-142: both apps migrated to phone, confirmed with both teams",
    validateInput: validateReason,
  });
  if (!reason) return;
  const choices = [7, 14, 30, 60, 90].filter((d) => d <= config.maxWaiverDays);
  const days = await vscode.window.showQuickPick(
    choices.map((d) => ({ label: `${d} days`, days: d, description: d === 30 ? "a typical pull request" : "" })),
    { title: "Expires in", placeHolder: "Pick the date the reason stops being true" },
  );
  if (!days) return;

  const entry = waiverEntry(change, { approvedBy, reason, days: days.days });
  fs.writeFileSync(waiversFile, appendWaiver(existing, entry), "utf-8");
  log(`waiver added to ${waiversRel} for ${change.fingerprint} (expires ${entry.expires})`);
  const open = await vscode.window.showInformationMessage(
    `Waiver added for ${change.fingerprint}, until ${entry.expires}. Commit ${waiversRel} in your pull request so reviewers see it.`,
    "Open it",
  );
  if (open) {
    await vscode.window.showTextDocument(vscode.Uri.file(waiversFile));
  }
  void check(false);
}

// --- set up ---------------------------------------------------------------------

interface InitInfo {
  status: "set_up" | "needs_spec";
  exit_code: number;
  text: string;
  stack?: string | null;
  note?: string;
  steps?: string[];
  spec?: string;
  written?: string[];
  kept?: string[];
  create_spec_cmd?: string | null;
  jenkins_stage?: string | null;
  checks?: Record<string, boolean>;
}

async function init(): Promise<void> {
  const root = projectRoot();
  if (!root) {
    void vscode.window.showWarningMessage("API Guard: open a project folder first.");
    return;
  }
  log(`\n[${new Date().toLocaleTimeString()}] api-guard init --json`);
  let out;
  try {
    out = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Setting up API Guard…" },
      () => runApiGuard(runnerSettings(), root, ["init", "--json"]),
    );
  } catch (error) {
    void vscode.window.showErrorMessage(String(error instanceof Error ? error.message : error));
    return;
  }
  log(`$ ${out.command}\n${out.stderr}`);

  let info: InitInfo;
  try {
    info = JSON.parse(out.stdout) as InitInfo;
  } catch {
    log(out.stdout);
    void vscode.window.showErrorMessage("API Guard: setup didn't finish. See the log.", "Show the log").then((p) => {
      if (p) output.show();
    });
    return;
  }
  log(info.text);

  if (info.status === "needs_spec") {
    const commands = (info.steps ?? []).filter((l) => l.startsWith("$ ")).map((l) => l.slice(2));
    const detail = [
      info.stack ? `Detected: ${info.stack}` : "",
      info.note ?? "",
      "",
      ...(info.steps ?? []).map((l) => (l.startsWith("$ ") ? `    ${l.slice(2)}` : l)),
      "",
      "Then run Set up again: it will find the spec and finish.",
    ]
      .filter((l, i, all) => l || all[i - 1])
      .join("\n");
    const pick = await vscode.window.showInformationMessage(
      "API Guard needs an OpenAPI spec first",
      { modal: true, detail },
      ...(commands.length ? ["Copy the commands"] : []),
      "Run Set up again",
    );
    if (pick === "Copy the commands") {
      await vscode.env.clipboard.writeText(commands.join("\n"));
    } else if (pick === "Run Set up again") {
      void init();
    }
    return;
  }

  const wrote = info.written?.length ? `Wrote ${info.written.join(", ")}.` : "Nothing new written.";
  const kept = info.kept?.length ? ` Kept your ${info.kept.join(", ")}.` : "";
  const buttons = ["Open api-guard.yaml", "Check now"];
  if (info.create_spec_cmd) buttons.unshift("Copy the spec command");
  if (info.jenkins_stage) buttons.push("Copy the Jenkins stage");
  const pick = await vscode.window.showInformationMessage(`API Guard is set up. ${wrote}${kept}`, ...buttons);
  if (pick === "Open api-guard.yaml") {
    await vscode.window.showTextDocument(vscode.Uri.file(path.join(root, "api-guard.yaml")));
  } else if (pick === "Check now") {
    void check(true);
  } else if (pick === "Copy the spec command" && info.create_spec_cmd) {
    await vscode.env.clipboard.writeText(info.create_spec_cmd);
  } else if (pick === "Copy the Jenkins stage" && info.jenkins_stage) {
    await vscode.env.clipboard.writeText(info.jenkins_stage);
  }
}

// --- report ---------------------------------------------------------------------

async function openReport(): Promise<void> {
  const root = projectRoot();
  if (!root) return;
  const config = readConfig(readText(path.join(root, "api-guard.yaml")));
  const report = vscode.Uri.file(path.join(root, config.reportDir, "report.md"));
  if (!fs.existsSync(report.fsPath)) {
    void vscode.window.showInformationMessage("No report yet: run a check first.", "Check now").then((p) => {
      if (p) void check(true);
    });
    return;
  }
  await vscode.commands.executeCommand("markdown.showPreview", report);
}

// For tests: what the extension currently shows.
export function _state() {
  return { lastResult, located, status: status?.text };
}
