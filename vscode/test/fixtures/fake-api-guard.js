#!/usr/bin/env node
// A stand-in for the api-guard command, for the editor integration tests.
// `check` writes a result.json with two breaking changes (one already waived
// once waivers.yaml lists it) and exits 1; `init --json` reports a set-up
// project. The real api-guard is exercised separately; this keeps the editor
// tests fast and deterministic.
const fs = require("fs");
const path = require("path");

const args = process.argv.slice(2);
if (args[0] === "--help") process.exit(0);

const json = (data, code = 0) => {
  process.stdout.write(JSON.stringify(data));
  process.exit(code);
};

// What the AI commands received, so tests can check the key arrives in the
// environment and never on the command line.
if (["ask", "review", "approve", "reject", "ask-review"].includes(args[0])) {
  fs.mkdirSync("api-guard-report", { recursive: true });
  fs.appendFileSync(path.join("api-guard-report", "ai-calls.jsonl"), JSON.stringify({
    args, hasKey: Boolean(process.env.GROQ_API_KEY), jenkins: process.env.JENKINS_URL || null,
  }) + "\n");
}

if (args[0] === "init") {
  const dry = args.includes("--dry-run");
  json({
    status: "set_up", exit_code: 0, text: "api-guard init\n", spec: "openapi.yaml", spec_exists: true,
    framework: null, generate_cmd: null, base_branch: "main", ci: args.includes("jenkins") ? ["jenkins"] : [],
    checks: { breaking: true, freshness: false, conformance: false },
    written: ["waivers.yaml"], kept: ["api-guard.yaml"], gitignore_added: [], create_spec_cmd: null,
    jenkins_stage: args.includes("jenkins") ? "stage('API contract') {}" : null,
    dry_run: dry, preview: dry ? { "waivers.yaml": "# waivers\n" } : {},
  });
}

if (args[0] === "ask") {
  json({ text: `Answer to: ${args[1]}`, steps: ["get_report"], confidence: "high", confidence_reason: "read it",
    warnings: [], caution: "Written by a language model." });
}

const REVIEW = {
  review_id: "42", status: "waiting", verdict: "failed", band: "risky", changes: 1, commit: "94cf4b0",
  question: "", questions_asked: 0, started: "", updated: "2026-10-04T10:00:00", full_commit: "94cf4b0c9573",
  branch: "demo/rename",
  change_list: [{ severity: "ERR", operation: "GET", path: "/users", text: "removed `email`", fingerprint: "ccc333ccc333" }],
  rationale: "", impact: "", migration: "", explain_model: "", qa: [], questions_left: 5, decision: "",
  decided_by: "", reason: "", expires: "", waiver_snippet: "", checklist: [], checkpoints: 3,
};
if (args[0] === "review" && args[1] === "list") json({ state: ".api-guard/reviews.db", reviews: [REVIEW] });
if (args[0] === "review" && args[1] === "show") json(REVIEW);
if (args[0] === "approve") {
  json({
    review_id: args[1], paused: false, decision: "approve", approved_by: args[args.indexOf("--by") + 1],
    waiver_snippet: "- fingerprint: ccc333ccc333\n  reason: PROD-142 both apps migrated\n  approved_by: sohan\n  expires: '2026-11-03'\n",
    report: "", question: "", answer: "", checklist: [],
  });
}

if (args[0] === "check") {
  fs.mkdirSync("api-guard-report", { recursive: true });
  fs.writeFileSync(path.join("api-guard-report", "args.json"), JSON.stringify(args));
  const waivers = fs.existsSync("waivers.yaml") ? fs.readFileSync("waivers.yaml", "utf-8") : "";
  const all = [
    { fingerprint: "aaa111aaa111", id: "response-required-property-removed", text: "removed the required property `email` from the response", severity: "ERR", operation: "GET", path: "/users" },
    { fingerprint: "bbb222bbb222", id: "new-required-request-property", text: "added the new required request property `email_address`", severity: "ERR", operation: "POST", path: "/users" },
  ];
  const changes = all.filter((c) => !waivers.includes(c.fingerprint));
  const blocked = changes.length > 0;
  const result = {
    schema_version: "1.1", generated_at: new Date().toISOString(),
    verdict: blocked ? "failed" : "passed", exit_code: blocked ? 1 : 0,
    checks: [
      { name: "breaking", status: blocked ? "failed" : "passed", summary: blocked ? `${changes.length} breaking change(s) at or above ERR` : "no breaking changes", detail: null },
      { name: "conformance", status: "skipped", summary: "no runtime section configured", detail: null },
    ],
    changes,
    waivers: { applied: [], stale: [], expired: [] },
    meta: { spec: "openapi.yaml", base: "git:origin/main" },
  };
  fs.mkdirSync("api-guard-report", { recursive: true });
  fs.writeFileSync(path.join("api-guard-report", "result.json"), JSON.stringify(result));
  process.stdout.write(blocked ? "API contract: BLOCKED\n" : "API contract: OK\n");
  process.exit(blocked ? 1 : 0);
}
process.exit(2);
