// Where in the spec file a change belongs, so it can be underlined.
//
// api-guard reports a change by endpoint and method ("GET /users"), not by
// line. This finds that endpoint in the spec's text: the method's key if it
// still exists, else the path's key, else the `paths:` key (a removed
// endpoint no longer has a line of its own). Works for YAML and JSON alike,
// since JSON is YAML. No vscode import: unit-tested on plain text.

import { isMap, isPair, isScalar, LineCounter, parseDocument, Pair, YAMLMap } from "yaml";

export interface Location {
  line: number; // 0-based
  startCol: number;
  endCol: number;
  exact: boolean; // false when it fell back to a parent key
}

export function locate(specText: string, path: string | null, operation: string | null): Location {
  const lineCounter = new LineCounter();
  let doc;
  try {
    doc = parseDocument(specText, { lineCounter, keepSourceTokens: false });
  } catch {
    return { line: 0, startCol: 0, endCol: 0, exact: false };
  }
  const root = doc.contents;
  if (!isMap(root)) {
    return { line: 0, startCol: 0, endCol: 0, exact: false };
  }

  const pathsPair = findPair(root, "paths");
  const pathsMap = pathsPair && isMap(pathsPair.value) ? pathsPair.value : null;
  const pathPair = path && pathsMap ? findPair(pathsMap, path) : null;
  const opPair =
    operation && pathPair && isMap(pathPair.value) ? findPair(pathPair.value, operation.toLowerCase()) : null;

  const best = opPair ?? pathPair ?? pathsPair;
  if (!best || !best.key || !isScalar(best.key) || !best.key.range) {
    return { line: 0, startCol: 0, endCol: 0, exact: false };
  }
  const [start, end] = best.key.range;
  const from = lineCounter.linePos(start);
  const to = lineCounter.linePos(end);
  return {
    line: from.line - 1,
    startCol: from.col - 1,
    endCol: to.line === from.line ? to.col - 1 : from.col - 1 + String(best.key.value).length,
    exact: best === (operation ? opPair : pathPair),
  };
}

function findPair(map: YAMLMap, key: string): Pair | null {
  for (const item of map.items) {
    if (isPair(item) && isScalar(item.key) && String(item.key.value) === key) {
      return item;
    }
  }
  return null;
}
