# API Guard for VS Code

See the API changes that would break your clients **while you edit**, before CI
blocks them.

API Guard compares your OpenAPI spec with the one on your main branch and
underlines every change that would break the apps using your API: a removed
field, a new required input, a deleted endpoint. It's the editor front end for
[api-guard](https://github.com/stimpy3/ApiGuard), and it runs the same checks
your CI does, so the editor and the pipeline never disagree.

## The API Guard panel

Click **API Guard** in the status bar (or run *API Guard: Open API Guard*). One
window, four tabs:

- **Overview.** The headline (OK, BLOCKED or can't check), each check, and every
  change. *Show in spec* jumps to it; *Accept this break* opens a small form
  (your name, a reason, an expiry) and writes the waiver into `waivers.yaml`
  with the real fingerprint, ready to commit in your pull request. Accepted
  breaks and expired waivers are listed too.
- **Set up.** `api-guard init`, visually. It shows what it found (your spec,
  framework, main branch, CI) and a preview of every file before anything is
  written. Pick the CI, press *Set up API Guard*. No spec yet? It shows the
  steps for your stack (Express, NestJS, Flask, Django, Spring, …) with copy
  buttons, and *Check again* when you're done.
- **Ask.** Ask our agent about past builds: "why did the last build fail?",
  "which waivers expire soon?". It runs on **your free Groq key**: create one at
  console.groq.com/keys and paste it into VS Code's prompt. VS Code keeps it
  encrypted on your machine, and it reaches api-guard only as an environment
  variable. Each answer shows which tools the agent used, its confidence and
  any warnings. It reads; it can't change anything.
- **Reviews.** Reviews saved in the project. Start one from a Jenkins build
  number, then approve it (and *Add to waivers.yaml*), reject it, or ask the
  agent a question first. Rules decide what's blocked; only a person approves.

## Around the editor

- **A status bar headline.** `API: OK`, or `API: 3 breaking` in red. Click it
  for the panel.
- **Squiggles on the spec.** Each breaking change is underlined on its endpoint
  and method (`get:` under `/users`), and listed in the Problems panel. The
  lightbulb offers *Accept this break*.
- **Check on save.** Saving the spec, `api-guard.yaml` or `waivers.yaml` checks
  again.
- **Conformance when your API is running.** If `api-guard.yaml` has a
  `runtime.url` and your API is up, its real responses are checked against the
  spec too. If it isn't running, that check just shows as *not checked*.
- **A sidebar.** The headline, each check, every change (click to jump to it),
  waivers, and expired waivers to clean up.

## What it needs

Either one of:

- **Docker** (nothing else to install: the extension runs the
  `sohanbhadalkar/api-guard` image), or
- the **api-guard command** (`pip install api-guard`).

By default it uses the command if installed, otherwise Docker. *Ask* and
*Reviews* use the `sohanbhadalkar/api-guard:1-ai` image (or the command with
`pip install 'api-guard[ai]'`), plus a free Groq key for the agent and a
reachable Jenkins for build questions.

Your project needs an OpenAPI spec (`openapi.yaml`, `openapi.json`, …) and a git
history with your main branch, since that's what changes are compared against.

## Settings

| Setting | Default | |
|---|---|---|
| `apiGuard.runner` | `auto` | `auto`, `cli` or `docker` |
| `apiGuard.cliPath` | `api-guard` | The command, for the `cli` runner |
| `apiGuard.dockerImage` | `sohanbhadalkar/api-guard:1` | The image, for the `docker` runner |
| `apiGuard.checkOnSave` | `true` | Check again when the spec or the api-guard files are saved |
| `apiGuard.aiDockerImage` | `sohanbhadalkar/api-guard:1-ai` | The image for Ask and Reviews, with the docker runner |
| `apiGuard.jenkinsUrl` | `http://localhost:8081` | Where Jenkins is, for Ask and reviewing a build (`localhost` is reached from Docker automatically) |
| `apiGuard.jenkinsJob` | (empty) | The Jenkins job path, e.g. `my-api/job/main` for a multibranch job |

## Commands

- **API Guard: Open API Guard** (the panel)
- **API Guard: Check the API contract**
- **API Guard: Set up API Guard in this project** (the panel's Set up tab)
- **API Guard: Ask about past builds** (the panel's Ask tab)
- **API Guard: Pending reviews** (the panel's Reviews tab)
- **API Guard: Open the full report**
- **API Guard: Show the log**

Everything about how the checks work, waivers, CI and the AI features is in the
[api-guard README](https://github.com/stimpy3/ApiGuard#readme).
