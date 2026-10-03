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

if (args[0] === "init") {
  process.stdout.write(JSON.stringify({
    status: "set_up", exit_code: 0, text: "api-guard init\n", spec: "openapi.yaml",
    written: ["waivers.yaml"], kept: ["api-guard.yaml"], create_spec_cmd: null, jenkins_stage: null,
  }));
  process.exit(0);
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
