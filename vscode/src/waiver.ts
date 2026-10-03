// Writing a waiver into waivers.yaml from the editor.
//
// In CI an approval can only print the waiver (CI can't write to git). In the
// editor the file is right there, so "Accept this break" writes it — the
// person only supplies who they are, why, and for how long. The fingerprint
// always comes from api-guard's result, never typed. No vscode import.

import { isSeq, parseDocument, stringify } from "yaml";
import { Change } from "./result";

export const MIN_REASON = 10; // the same bar api-guard applies when loading

export interface WaiverInput {
  approvedBy: string;
  reason: string;
  days: number;
  today?: Date;
}

export function expiryDate(days: number, today: Date = new Date()): string {
  const d = new Date(Date.UTC(today.getFullYear(), today.getMonth(), today.getDate()));
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function waiverEntry(change: Change, input: WaiverInput): Record<string, string> {
  if (!change.fingerprint) {
    throw new Error("this change has no fingerprint, so it can't be waived");
  }
  const entry: Record<string, string> = { fingerprint: change.fingerprint };
  if (change.id) entry.id = change.id;
  if (change.path) entry.path = change.path;
  entry.reason = input.reason.trim();
  entry.approved_by = input.approvedBy.trim();
  entry.expires = expiryDate(input.days, input.today);
  return entry;
}

export function validateReason(reason: string): string | null {
  return reason.trim().length >= MIN_REASON
    ? null
    : `At least ${MIN_REASON} characters: a reviewer has to be able to evaluate it.`;
}

/** Fingerprints already in the file, so the same break isn't waived twice. */
export function waivedFingerprints(existing: string): Set<string> {
  try {
    const data = parseDocument(existing).toJS();
    if (Array.isArray(data)) {
      return new Set(data.map((w) => String(w?.fingerprint ?? "")).filter(Boolean));
    }
  } catch {
    // An unreadable file is reported by api-guard on the next check.
  }
  return new Set();
}

/**
 * The new file content: the existing text with the entry appended, keeping
 * every comment. An empty flow list (`[]`) is replaced, since a block entry
 * can't follow it.
 */
export function appendWaiver(existing: string, entry: Record<string, string>): string {
  const block = stringify([entry], { lineWidth: 0 });
  let text = existing;
  try {
    const doc = parseDocument(existing);
    if (isSeq(doc.contents) && doc.contents.flow && doc.contents.items.length === 0) {
      text = existing.replace(/^\s*\[\s*\]\s*$/m, "");
    }
  } catch {
    // keep the text as is
  }
  if (text.length && !text.endsWith("\n")) {
    text += "\n";
  }
  return text + block;
}

/**
 * Add the entries of an approval's waiver snippet (api-guard's YAML list) to
 * waivers.yaml, skipping fingerprints already there.
 */
export function appendSnippet(
  existing: string,
  snippet: string,
): { text: string; added: string[]; skipped: string[] } {
  const entries = parseDocument(snippet).toJS();
  if (!Array.isArray(entries)) {
    throw new Error("the approval's waiver snippet is not a YAML list");
  }
  const have = waivedFingerprints(existing);
  const added: string[] = [];
  const skipped: string[] = [];
  let text = existing;
  for (const entry of entries) {
    const fp = String(entry?.fingerprint ?? "");
    if (!fp) continue;
    if (have.has(fp)) {
      skipped.push(fp);
      continue;
    }
    const clean: Record<string, string> = {};
    for (const [k, v] of Object.entries(entry)) clean[k] = String(v);
    text = appendWaiver(text, clean);
    have.add(fp);
    added.push(fp);
  }
  return { text, added, skipped };
}
