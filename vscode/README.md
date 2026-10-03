# API Guard for VS Code

See the API changes that would break your clients **while you edit**, before CI
blocks them.

API Guard compares your OpenAPI spec with the one on your main branch and
underlines every change that would break the apps using your API: a removed
field, a new required input, a deleted endpoint. It's the editor front end for
[api-guard](https://github.com/stimpy3/ApiGuard), and it runs the same checks
your CI does, so the editor and the pipeline never disagree.

## What you get

- **A status bar headline.** `API: OK`, or `API: 3 breaking` in red. Click it
  to check again.
- **Squiggles on the spec.** Each breaking change is underlined on its endpoint
  and method (`get:` under `/users`), and listed in the Problems panel.
- **Check on save.** Saving the spec, `api-guard.yaml` or `waivers.yaml` checks
  again.
- **Set up in one click.** *API Guard: Set up* detects your spec, framework and
  CI and writes the setup files. No spec yet? It tells you exactly how to add
  one for your stack (Express, NestJS, Flask, Django, Spring, …).
- **Accept a break on purpose.** On a breaking change, the lightbulb offers
  *Accept this break*. Type your name and a reason, pick an expiry, and the
  waiver is written into `waivers.yaml` with the real fingerprint, ready to
  commit in your pull request.
- **A sidebar.** The headline, each check, every change (click to jump to it),
  waivers, and expired waivers to clean up.

## What it needs

Either one of:

- **Docker** (nothing else to install: the extension runs the
  `sohanbhadalkar/api-guard` image), or
- the **api-guard command** (`pip install api-guard`).

By default it uses the command if installed, otherwise Docker.

Your project needs an OpenAPI spec (`openapi.yaml`, `openapi.json`, …) and a git
history with your main branch, since that's what changes are compared against.

## Settings

| Setting | Default | |
|---|---|---|
| `apiGuard.runner` | `auto` | `auto`, `cli` or `docker` |
| `apiGuard.cliPath` | `api-guard` | The command, for the `cli` runner |
| `apiGuard.dockerImage` | `sohanbhadalkar/api-guard:1` | The image, for the `docker` runner |
| `apiGuard.checkOnSave` | `true` | Check again when the spec or the api-guard files are saved |

## Commands

- **API Guard: Check the API contract**
- **API Guard: Set up API Guard in this project**
- **API Guard: Open the full report**
- **API Guard: Show the log**

Everything about how the checks work, waivers, CI and the AI features is in the
[api-guard README](https://github.com/stimpy3/ApiGuard#readme).
