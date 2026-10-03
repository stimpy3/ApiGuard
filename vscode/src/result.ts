// The shape of api-guard's result.json, and what the extension derives from it.
//
// result.json is api-guard's machine-readable interface (it carries a
// schema_version). The extension reads it rather than parsing console text,
// so editor and CI always agree: the editor shows what api-guard decided.
// No vscode import here, so this is unit-tested without an editor.

export type Status = "passed" | "failed" | "skipped" | "error";

export interface Change {
  fingerprint: string | null;
  id: string;
  text: string;
  severity: "INFO" | "WARN" | "ERR";
  operation: string | null;
  path: string | null;
}

export interface CheckResult {
  name: string;
  status: Status;
  summary: string;
  detail: string | null;
}

export interface Waiver {
  fingerprint: string;
  reason: string;
  approved_by: string;
  expires: string;
  id?: string | null;
  path?: string | null;
}

export interface RunResult {
  schema_version: string;
  generated_at: string;
  verdict: "passed" | "failed" | "error";
  exit_code: number;
  checks: CheckResult[];
  changes: Change[];
  waivers: { applied: Waiver[]; stale: Waiver[]; expired?: Waiver[] };
  meta: Record<string, string>;
}

export function parseResult(text: string): RunResult {
  const data = JSON.parse(text) as RunResult;
  if (!data || typeof data !== "object" || !Array.isArray(data.checks)) {
    throw new Error("result.json is not an api-guard result");
  }
  data.changes = data.changes ?? [];
  const waivers: Partial<RunResult["waivers"]> = data.waivers ?? {};
  data.waivers = {
    applied: waivers.applied ?? [],
    stale: waivers.stale ?? [],
    expired: waivers.expired ?? [], // absent before result.json schema 1.1
  };
  data.meta = data.meta ?? {};
  return data;
}

const WHAT_FAILED: Record<string, string> = {
  breaking: "changes that would break clients",
  freshness: "the committed spec is out of date",
  conformance: "the running API doesn't match its spec",
};

/** The same one-line headline the CLI prints first. */
export function headline(result: RunResult): string {
  if (result.verdict === "error") {
    return "API contract: COULD NOT CHECK (a setup problem, not your API)";
  }
  if (result.verdict === "failed") {
    const failed = result.checks.filter((c) => c.status === "failed");
    return "API contract: BLOCKED - " + failed.map((c) => WHAT_FAILED[c.name] ?? c.summary).join("; ");
  }
  const waived = result.waivers.applied.length;
  return "API contract: OK" + (waived ? ` (${waived} change(s) waived)` : "");
}

/** Short text for the status bar. */
export function statusText(result: RunResult): string {
  if (result.verdict === "error") {
    return "$(warning) API: can't check";
  }
  if (result.verdict === "failed") {
    const blocking = blockingChanges(result).length;
    return blocking ? `$(error) API: ${blocking} breaking` : "$(error) API: blocked";
  }
  return "$(pass) API: OK";
}

/**
 * Changes that blocked the run. result.json lists every change left after
 * waivers, including ones below the threshold; when the breaking check
 * failed, the ERR ones are what blocked it (WARN too only under fail_on WARN,
 * which the check's own summary reflects).
 */
export function blockingChanges(result: RunResult): Change[] {
  const breaking = result.checks.find((c) => c.name === "breaking");
  if (!breaking || breaking.status !== "failed") {
    return [];
  }
  const failOnWarn = /at or above WARN/.test(breaking.summary);
  return result.changes.filter((c) => c.severity === "ERR" || (failOnWarn && c.severity === "WARN"));
}
