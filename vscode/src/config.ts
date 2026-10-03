// The few api-guard.yaml settings the extension needs to find things.
// Defaults mirror api-guard's own. No vscode import.

import { parseDocument } from "yaml";

export interface GuardConfig {
  exists: boolean;
  reportDir: string;
  waiversFile: string | null; // null: the config exists but has no waivers file
  maxWaiverDays: number;
  runtimeUrl: string | null; // where the running API is, for conformance
}

export function readConfig(text: string | null): GuardConfig {
  if (text === null) {
    // No api-guard.yaml: api-guard runs on defaults, and uses waivers.yaml
    // at the project root when it exists.
    return {
      exists: false,
      reportDir: "api-guard-report",
      waiversFile: "waivers.yaml",
      maxWaiverDays: 90,
      runtimeUrl: null,
    };
  }
  let data: any = {};
  try {
    data = parseDocument(text).toJS() ?? {};
  } catch {
    data = {};
  }
  return {
    exists: true,
    reportDir: data?.report?.dir ?? "api-guard-report",
    waiversFile: data?.policy?.waivers ?? null,
    maxWaiverDays: Number(data?.policy?.max_waiver_days ?? 90),
    runtimeUrl: data?.runtime?.url ?? null,
  };
}
