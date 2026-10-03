# Changelog

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
