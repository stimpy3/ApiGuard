# Changelog

## 0.1.2

- Conformance is checked when your API is running, and simply "not
  checked: API not running" when it isn't. No setting to switch, no error,
  no 30-second wait (uses api-guard's `--if-running`).
- With the Docker runner, a `runtime.url` on `localhost` is reached as
  `host.docker.internal`, so an API running on your machine is found from
  inside the container. Your api-guard.yaml is unchanged for CI.
- The `apiGuard.conformance` setting from 0.1.1 is gone.

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
