// The "Contract" view in the API Guard sidebar: the headline, each check,
// every change (click to jump to it in the spec), and waivers.

import * as vscode from "vscode";
import { blockingChanges, Change, CheckResult, headline, RunResult } from "./result";

export interface Located {
  change: Change;
  uri: vscode.Uri;
  range: vscode.Range;
}

type Node = vscode.TreeItem & { children?: Node[] };

const ICON: Record<string, vscode.ThemeIcon> = {
  passed: new vscode.ThemeIcon("pass", new vscode.ThemeColor("testing.iconPassed")),
  failed: new vscode.ThemeIcon("error", new vscode.ThemeColor("testing.iconFailed")),
  error: new vscode.ThemeIcon("warning", new vscode.ThemeColor("problemsWarningIcon.foreground")),
  skipped: new vscode.ThemeIcon("circle-slash"),
};

export class ResultsTree implements vscode.TreeDataProvider<Node> {
  private readonly changed = new vscode.EventEmitter<Node | undefined>();
  readonly onDidChangeTreeData = this.changed.event;
  private nodes: Node[] = [];

  show(result: RunResult, located: Located[]): void {
    this.nodes = build(result, located);
    this.changed.fire(undefined);
  }

  showProblem(message: string): void {
    const node: Node = new vscode.TreeItem(message);
    node.iconPath = ICON.error;
    node.tooltip = message;
    const log: Node = new vscode.TreeItem("Show the log");
    log.command = { command: "apiGuard.showOutput", title: "Show the log" };
    log.iconPath = new vscode.ThemeIcon("output");
    this.nodes = [node, log];
    this.changed.fire(undefined);
  }

  getTreeItem(node: Node): vscode.TreeItem {
    return node;
  }

  getChildren(node?: Node): Node[] {
    return node ? node.children ?? [] : this.nodes;
  }
}

function item(label: string, opts: Partial<Node> = {}, children?: Node[]): Node {
  const node: Node = new vscode.TreeItem(
    label,
    children?.length ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None,
  );
  Object.assign(node, opts);
  node.children = children;
  return node;
}

function build(result: RunResult, located: Located[]): Node[] {
  const nodes: Node[] = [];
  const icon =
    result.verdict === "passed" ? ICON.passed : result.verdict === "failed" ? ICON.failed : ICON.error;
  nodes.push(item(headline(result), { iconPath: icon, tooltip: headline(result) }));

  const blocking = new Set(blockingChanges(result));
  for (const check of result.checks.filter((c) => c.status !== "skipped")) {
    nodes.push(checkNode(check, located, blocking));
  }

  const skipped = result.checks.filter((c) => c.status === "skipped");
  if (skipped.length) {
    nodes.push(
      item(
        "Not checked",
        { iconPath: ICON.skipped, description: skipped.map((c) => c.name).join(", ") },
        skipped.map((c) => item(c.name, { description: c.summary, tooltip: c.detail ?? c.summary })),
      ),
    );
  }

  const { applied, expired = [] } = result.waivers;
  if (applied.length) {
    nodes.push(
      item(
        `Waived (${applied.length})`,
        { iconPath: new vscode.ThemeIcon("shield") },
        applied.map((w) =>
          item(w.id ?? w.fingerprint, {
            description: `${w.approved_by}, until ${w.expires}`,
            tooltip: `${w.reason}\nfingerprint ${w.fingerprint}`,
          }),
        ),
      ),
    );
  }
  if (expired.length) {
    nodes.push(
      item(
        `Expired waivers (${expired.length}): remove them`,
        { iconPath: ICON.error },
        expired.map((w) => item(w.id ?? w.fingerprint, { description: `expired ${w.expires}` })),
      ),
    );
  }
  return nodes;
}

function checkNode(check: CheckResult, located: Located[], blocking: Set<Change>): Node {
  let children: Node[] | undefined;
  if (check.name === "breaking") {
    children = located.map(({ change, uri, range }) =>
      item(`${change.operation ?? ""} ${change.path ?? ""}`.trim() || change.id, {
        description: change.text,
        tooltip: `${change.text}\nrule ${change.id}${change.fingerprint ? `\nfingerprint ${change.fingerprint}` : ""}`,
        iconPath: blocking.has(change) ? ICON.failed : new vscode.ThemeIcon("info"),
        command: {
          command: "vscode.open",
          title: "Show in the spec",
          arguments: [uri, { selection: range }],
        },
      }),
    );
  } else if (check.detail && check.status !== "passed") {
    children = check.detail
      .split("\n")
      .filter((l) => l.trim())
      .slice(0, 12)
      .map((l) => item(l.trim(), { tooltip: l }));
  }
  return item(check.name, { iconPath: ICON[check.status], description: check.summary, tooltip: check.summary }, children);
}
