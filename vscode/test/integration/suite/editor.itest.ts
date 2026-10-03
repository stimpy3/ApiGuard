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

  test("set up reports what init wrote", async () => {
    const win = vscode.window as any;
    const original = win.showInformationMessage;
    let shown = "";
    win.showInformationMessage = async (message: string) => {
      shown = message;
      return undefined;
    };
    try {
      await vscode.commands.executeCommand("apiGuard.init");
    } finally {
      win.showInformationMessage = original;
    }
    assert.match(shown, /API Guard is set up\. Wrote waivers\.yaml\. Kept your api-guard\.yaml\./);
  });
});
