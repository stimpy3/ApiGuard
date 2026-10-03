// The extension inside a real VS Code, on the fixture project with a fake
// api-guard (see fixtures/fake-api-guard.js).

import * as assert from "assert";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

const root = () => vscode.workspace.workspaceFolders![0].uri.fsPath;
const specUri = () => vscode.Uri.file(path.join(root(), "openapi.yaml"));

async function until<T>(get: () => T | undefined, what: string, ms = 30_000): Promise<T> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const value = get();
    if (value) return value;
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`timed out waiting for ${what}`);
}

const guardDiagnostics = () =>
  vscode.languages.getDiagnostics(specUri()).filter((d) => d.source === "api-guard");

suite("API Guard in the editor", () => {
  suiteSetup(async () => {
    const ext = vscode.extensions.all.find((e) => e.packageJSON.name === "api-guard")!;
    await ext.activate();
  });

  test("a check underlines each breaking change on its line in the spec", async () => {
    await vscode.commands.executeCommand("apiGuard.check");
    const diags = await until(() => (guardDiagnostics().length === 2 ? guardDiagnostics() : undefined), "2 diagnostics");

    const doc = await vscode.workspace.openTextDocument(specUri());
    const byLine = diags.map((d) => [doc.lineAt(d.range.start.line).text.trim(), d.message, d.severity] as const);
    assert.deepStrictEqual(
      byLine.sort(),
      [
        ["get:", "removed the required property `email` from the response", vscode.DiagnosticSeverity.Error],
        ["post:", "added the new required request property `email_address`", vscode.DiagnosticSeverity.Error],
      ].sort(),
    );
    assert.ok(diags.every((d) => d.code === "response-required-property-removed" || d.code === "new-required-request-property"));
  });

  test("conformance is asked for only if the API is running", () => {
    // Regression: sample-api configures runtime.url, nothing listens while
    // editing, and every check ended "could not check" after a 30 s wait.
    const args = JSON.parse(fs.readFileSync(path.join(root(), "api-guard-report", "args.json"), "utf-8"));
    assert.deepStrictEqual(args, ["check", "--if-running"]);
  });

  test("the lightbulb offers to accept a break, and that writes a valid waiver", async () => {
    const doc = await vscode.workspace.openTextDocument(specUri());
    const getDiag = guardDiagnostics().find((d) => doc.lineAt(d.range.start.line).text.trim() === "get:")!;
    const actions = await vscode.commands.executeCommand<vscode.CodeAction[]>(
      "vscode.executeCodeActionProvider", specUri(), getDiag.range, vscode.CodeActionKind.QuickFix.value,
    );
    const accept = actions.find((a) => a.title.includes("aaa111aaa111"));
    assert.ok(accept, `no accept action among: ${actions.map((a) => a.title).join(" | ")}`);

    // Answer the three prompts: name, reason, expiry.
    const win = vscode.window as any;
    const originals = { input: win.showInputBox, pick: win.showQuickPick, info: win.showInformationMessage };
    const answers = ["sohan", "PROD-142: both apps migrated to phone"];
    win.showInputBox = async (opts: vscode.InputBoxOptions) => {
      const answer = answers.shift()!;
      assert.strictEqual(opts.validateInput ? await opts.validateInput(answer) : null, null);
      return answer;
    };
    win.showQuickPick = async (items: any[]) => items.find((i) => i.days === 30);
    win.showInformationMessage = async () => undefined;
    try {
      await vscode.commands.executeCommand(accept!.command!.command, ...(accept!.command!.arguments ?? []));
    } finally {
      win.showInputBox = originals.input;
      win.showQuickPick = originals.pick;
      win.showInformationMessage = originals.info;
    }

    const waivers = fs.readFileSync(path.join(root(), "waivers.yaml"), "utf-8");
    assert.match(waivers, /fingerprint: aaa111aaa111/);
    assert.match(waivers, /approved_by: sohan/);
    assert.match(waivers, /reason: "?PROD-142: both apps migrated to phone"?/);
    assert.match(waivers, /expires: \d{4}-\d{2}-\d{2}/);

    // The extension re-checks after writing: the waived change disappears.
    await until(() => (guardDiagnostics().length === 1 ? true : undefined), "one diagnostic left");
    assert.strictEqual(guardDiagnostics()[0].message, "added the new required request property `email_address`");
  });

  // --- the panel ---------------------------------------------------------------------
  // The page only sends messages; these drive the extension side with the same
  // messages and check what it sends back and what it writes.

  // The same module instance the extension host loaded (require cache).
  const ext = () =>
    require(path.join(vscode.extensions.all.find((e) => e.packageJSON.name === "api-guard")!.extensionPath, "out", "src", "extension.js"));
  const panel = () => ext()._state().panel!;
  const aiCalls = (): any[] =>
    fs.readFileSync(path.join(root(), "api-guard-report", "ai-calls.jsonl"), "utf-8").trim().split("\n").map((l) => JSON.parse(l));
  const lastSent = (type: string) => [...panel().sent].reverse().find((m) => m.type === type);

  test("set up opens the panel and previews what init would write", async () => {
    await vscode.commands.executeCommand("apiGuard.init");
    assert.ok(panel(), "the panel opened");
    await panel().handle({ type: "initPreview", ci: "jenkins" });
    const preview = lastSent("init")!;
    assert.strictEqual(preview.ran, false);
    assert.strictEqual(preview.data.dry_run, true);
    assert.deepStrictEqual(preview.data.preview, { "waivers.yaml": "# waivers\n" });
    assert.ok(preview.data.jenkins_stage, "the Jenkins stage to copy");

    await panel().handle({ type: "initRun", ci: "auto", force: false });
    const ran = lastSent("init")!;
    assert.strictEqual(ran.ran, true);
    assert.deepStrictEqual([ran.data.written, ran.data.kept], [["waivers.yaml"], ["api-guard.yaml"]]);
  });

  test("the overview gets the result, and accepting a break there writes the waiver", async () => {
    await panel().handle({ type: "ready" });
    const state = lastSent("state")!;
    assert.match(state.headline, /BLOCKED/);
    const change = state.result.changes.find((c: any) => c.fingerprint === "bbb222bbb222");
    assert.ok(change);

    await panel().handle({ type: "waive", change, approvedBy: "sohan", reason: "short", days: 30 });
    assert.match(lastSent("toast")!.text, /At least 10 characters/);

    await panel().handle({ type: "waive", change, approvedBy: "sohan", reason: "PROD-150: the web app sends email_address", days: 30 });
    assert.strictEqual(lastSent("waived")!.fingerprint, "bbb222bbb222");
    const waivers = fs.readFileSync(path.join(root(), "waivers.yaml"), "utf-8");
    assert.match(waivers, /fingerprint: bbb222bbb222/);
    assert.match(lastSent("state")!.headline, /OK/);
  });

  test("ask needs a key, then sends it only in the environment", async () => {
    await panel().handle({ type: "ask", question: "why did build 3 fail?" });
    assert.match(lastSent("toast")!.text, /Groq key/);

    const win = vscode.window as any;
    const original = win.showInputBox;
    win.showInputBox = async () => "gsk_test_key_0123456789abcdef";
    try {
      await panel().handle({ type: "setKey" });
    } finally {
      win.showInputBox = original;
    }
    assert.strictEqual(lastSent("state")!.ai.hasKey, true);

    await panel().handle({ type: "ask", question: "why did build 3 fail?" });
    assert.strictEqual(lastSent("answer")!.data.text, "Answer to: why did build 3 fail?");
    const call = aiCalls().pop();
    assert.strictEqual(call.hasKey, true);
    assert.strictEqual(call.jenkins, "http://localhost:8081");
    assert.ok(!JSON.stringify(call.args).includes("gsk_test_key"), "the key is never an argument");
    assert.ok(!JSON.stringify(panel().sent).includes("gsk_test_key"), "nor sent to the page");
  });

  test("reviews: list, open, approve, then add the waiver to waivers.yaml", async () => {
    await panel().handle({ type: "reviews" });
    assert.strictEqual(lastSent("reviews")!.data.reviews[0].review_id, "42");

    await panel().handle({ type: "decide", action: "approve", id: "42", by: "sohan", reason: "PROD-142 both apps migrated", days: 30 });
    const decided = lastSent("decided")!;
    assert.strictEqual(decided.data.decision, "approve");
    const call = aiCalls().find((c) => c.args[0] === "approve");
    assert.deepStrictEqual(call.args.slice(0, 2), ["approve", "42"]);
    assert.ok(call.args.includes("--expires-in"));

    await panel().handle({ type: "applySnippet", snippet: decided.data.waiver_snippet });
    const waivers = fs.readFileSync(path.join(root(), "waivers.yaml"), "utf-8");
    assert.match(waivers, /fingerprint: ccc333ccc333/);
    await panel().handle({ type: "applySnippet", snippet: decided.data.waiver_snippet });
    assert.match(lastSent("toast")!.text, /Already in waivers\.yaml/);
  });
});
