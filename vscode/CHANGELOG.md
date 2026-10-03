# Changelog

## 0.1.1

- The editor checks the spec (breaking and freshness) and leaves out
  conformance by default, since it needs the API running. Projects with
  `runtime.url` set no longer end every check with "could not check".
  Turn it on with `apiGuard.conformance`.
- In a folder VS Code doesn't trust yet (Restricted Mode), VS Code now says
  why API Guard is off instead of hiding it.

## 0.1.0

First version.

- Status bar headline, squiggles on the spec and the Problems panel, from the
  same api-guard run CI uses (installed command or Docker image).
- Check on save of the spec, `api-guard.yaml` and `waivers.yaml`.
- *Set up*: runs `api-guard init`; with no spec yet, shows the steps to add one
  for the detected stack.
- *Accept this break* quick fix: writes the waiver (real fingerprint, your
  name, reason and expiry) into `waivers.yaml`.
- Sidebar with the headline, checks, changes, waivers and expired waivers.
