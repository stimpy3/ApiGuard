// Unit tests for the parts that don't need an editor: run with `npm test`
// (node's built-in test runner).

import { test } from "node:test";
import * as assert from "node:assert/strict";
import { parseDocument } from "yaml";
import { checkArgs, urlForDocker } from "../src/args";
import { readConfig } from "../src/config";
import { locate } from "../src/locate";
import { blockingChanges, headline, parseResult, statusText } from "../src/result";
import { appendWaiver, expiryDate, validateReason, waivedFingerprints, waiverEntry } from "../src/waiver";

// Sorted keys, like sample-api's spec: paths come after a components block.
const YAML_SPEC = `components:
  schemas:
    User:
      type: object
info:
  title: users
openapi: 3.1.0
paths:
  /users:
    get:
      summary: list
    post:
      summary: create
  /users/{user_id}:
    get:
      summary: one
`;

const change = (over: object = {}) => ({
  fingerprint: "631dbccdc316",
  id: "response-required-property-removed",
  text: "removed the required property `email` from the response",
  severity: "ERR" as const,
  operation: "GET",
  path: "/users",
  ...over,
});

// --- locate ---------------------------------------------------------------------

test("finds the method under the path", () => {
  const loc = locate(YAML_SPEC, "/users", "POST");
  assert.equal(loc.line, 11);
  assert.equal(YAML_SPEC.split("\n")[loc.line].slice(loc.startCol, loc.endCol), "post");
  assert.equal(loc.exact, true);
});

test("a path with braces is found", () => {
  assert.equal(locate(YAML_SPEC, "/users/{user_id}", "GET").line, 14);
});

test("a removed method falls back to its path, a removed path to `paths:`", () => {
  assert.deepEqual([locate(YAML_SPEC, "/users", "DELETE").line, locate(YAML_SPEC, "/users", "DELETE").exact], [8, false]);
  assert.equal(locate(YAML_SPEC, "/orders", "GET").line, 7);
});

test("JSON specs work the same", () => {
  const json = JSON.stringify(parseDocument(YAML_SPEC).toJS(), null, 2);
  const loc = locate(json, "/users", "GET");
  assert.match(json.split("\n")[loc.line], /"get"/);
});

test("an unparseable spec doesn't throw", () => {
  assert.equal(locate("paths: [unclosed", "/users", "GET").line, 0);
});

// --- result -----------------------------------------------------------------------

const result = (verdict: string, checks: object[], changes: object[] = []) =>
  parseResult(JSON.stringify({ schema_version: "1.1", generated_at: "", verdict, exit_code: 0, checks, changes, meta: {} }));

test("headline and status match the CLI's", () => {
  const ok = result("passed", [{ name: "breaking", status: "passed", summary: "no breaking changes", detail: null }]);
  assert.equal(headline(ok), "API contract: OK");
  assert.equal(statusText(ok), "$(pass) API: OK");

  const blocked = result(
    "failed",
    [{ name: "breaking", status: "failed", summary: "2 breaking change(s) at or above ERR", detail: null }],
    [change(), change({ fingerprint: "b", severity: "WARN" })],
  );
  assert.equal(headline(blocked), "API contract: BLOCKED - changes that would break clients");
  assert.equal(blockingChanges(blocked).length, 1, "WARN doesn't block under fail_on ERR");
  assert.equal(statusText(blocked), "$(error) API: 1 breaking");
});

test("older result.json without expired waivers still parses", () => {
  const r = parseResult(JSON.stringify({ verdict: "passed", checks: [], waivers: { applied: [], stale: [] } }));
  assert.deepEqual(r.waivers.expired, []);
});

// --- waiver -----------------------------------------------------------------------

test("the waiver entry uses the real fingerprint and loads back", () => {
  const entry = waiverEntry(change(), {
    approvedBy: " sohan ",
    reason: "PROD-142: both apps migrated",
    days: 30,
    today: new Date(2026, 9, 3),
  });
  assert.deepEqual(entry, {
    fingerprint: "631dbccdc316",
    id: "response-required-property-removed",
    path: "/users",
    reason: "PROD-142: both apps migrated",
    approved_by: "sohan",
    expires: "2026-11-02",
  });
});

test("appending keeps comments and earlier entries", () => {
  const existing = "# Breaking changes made on purpose.\n- fingerprint: aaa111\n  reason: an earlier good reason\n  approved_by: x\n  expires: 2026-12-01\n";
  const text = appendWaiver(existing, { fingerprint: "bbb222", reason: "a second good reason", approved_by: "y", expires: "2026-12-02" });
  assert.ok(text.startsWith("# Breaking changes made on purpose."));
  assert.deepEqual([...waivedFingerprints(text)], ["aaa111", "bbb222"]);
});

test("a comments-only file or an empty [] becomes a valid list", () => {
  for (const existing of ["# only comments\n", "# c\n[]\n", ""]) {
    const text = appendWaiver(existing, { fingerprint: "ccc333", reason: "a good enough reason", approved_by: "z", expires: "2026-12-03" });
    assert.deepEqual([...waivedFingerprints(text)], ["ccc333"], JSON.stringify(existing));
  }
});

test("reasons need substance, like api-guard requires", () => {
  assert.ok(validateReason("temp"));
  assert.equal(validateReason("PROD-142: clients migrated"), null);
});

test("expiry dates don't drift across month ends", () => {
  assert.equal(expiryDate(30, new Date(2026, 0, 31)), "2026-03-02");
});

// --- config -----------------------------------------------------------------------

test("no api-guard.yaml means api-guard's defaults", () => {
  assert.deepEqual(readConfig(null), {
    exists: false, reportDir: "api-guard-report", waiversFile: "waivers.yaml", maxWaiverDays: 90, runtimeUrl: null,
  });
});

// --- check arguments ----------------------------------------------------------------

test("every check asks for conformance only if the API is running", () => {
  assert.deepEqual(checkArgs("cli", "http://localhost:8000"), ["check", "--if-running"]);
  assert.deepEqual(checkArgs("docker", null), ["check", "--if-running"]);
});

test("in Docker, localhost means this machine: host.docker.internal", () => {
  assert.deepEqual(checkArgs("docker", "http://localhost:8000"), ["check", "--if-running", "--url", "http://host.docker.internal:8000"]);
  assert.equal(urlForDocker("http://127.0.0.1:8080/api/"), "http://host.docker.internal:8080/api/");
  assert.equal(urlForDocker("https://staging.example.com"), null, "a real host is left alone");
  assert.equal(urlForDocker("not a url"), null);
});

test("runtime.url is read from api-guard.yaml", () => {
  assert.equal(readConfig("spec:\n  path: x.yaml\nruntime:\n  url: http://localhost:8000\n").runtimeUrl, "http://localhost:8000");
});

test("settings come from api-guard.yaml when present", () => {
  const c = readConfig("spec:\n  path: x.yaml\npolicy:\n  waivers: w.yaml\n  max_waiver_days: 30\nreport:\n  dir: out\n");
  assert.deepEqual([c.reportDir, c.waiversFile, c.maxWaiverDays], ["out", "w.yaml", 30]);
  assert.equal(readConfig("spec:\n  path: x.yaml\n").waiversFile, null, "a config without a waivers file says so");
});
