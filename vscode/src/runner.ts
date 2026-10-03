// Running api-guard: the installed command, or the Docker image.
//
// The extension never re-implements a check; it runs the same api-guard CI
// runs, so the editor can't disagree with the pipeline. "auto" prefers an
// installed command and falls back to Docker, which needs nothing but Docker.
// Arguments are passed as an array (execFile, no shell), so paths with spaces
// and user text can't be misread. No vscode import.

import { execFile } from "child_process";

export type RunnerKind = "auto" | "cli" | "docker";

export interface RunnerSettings {
  runner: RunnerKind;
  cliPath: string;
  dockerImage: string;
}

export interface RunOutput {
  code: number;
  stdout: string;
  stderr: string;
  command: string; // for the log
}

export class RunnerMissing extends Error {}

const cache = new Map<string, boolean>();

function exec(file: string, args: string[], cwd: string, timeoutMs: number): Promise<RunOutput> {
  return new Promise((resolve) => {
    execFile(
      file,
      args,
      { cwd, timeout: timeoutMs, maxBuffer: 20 * 1024 * 1024, windowsHide: true },
      (error, stdout, stderr) => {
        const code =
          error && typeof (error as any).code === "number" ? (error as any).code : error ? 127 : 0;
        resolve({ code, stdout: String(stdout), stderr: String(stderr), command: [file, ...args].join(" ") });
      },
    );
  });
}

async function works(file: string, args: string[], cwd: string): Promise<boolean> {
  const key = `${file} ${args.join(" ")}`;
  if (!cache.has(key)) {
    const out = await exec(file, args, cwd, 20_000);
    cache.set(key, out.code === 0);
  }
  return cache.get(key)!;
}

export async function resolveRunner(settings: RunnerSettings, cwd: string): Promise<"cli" | "docker"> {
  if (settings.runner === "cli" || settings.runner === "docker") {
    return settings.runner;
  }
  if (await works(settings.cliPath, ["--help"], cwd)) {
    return "cli";
  }
  if (await works("docker", ["version", "--format", "{{.Server.Version}}"], cwd)) {
    return "docker";
  }
  throw new RunnerMissing(
    "api-guard can't run: neither the `api-guard` command nor a running Docker was found. " +
      "Start Docker Desktop, or `pip install api-guard`.",
  );
}

/** Run `api-guard <args>` in the project folder, by whichever runner applies. */
export async function runApiGuard(
  settings: RunnerSettings,
  projectRoot: string,
  args: string[],
  timeoutMs = 15 * 60_000,
): Promise<RunOutput> {
  const kind = await resolveRunner(settings, projectRoot);
  if (kind === "cli") {
    return exec(settings.cliPath, args, projectRoot, timeoutMs);
  }
  // The project is mounted at /work, the same layout as the CI one-liner, so
  // paths in result.json are the same relative paths CI would report.
  return exec(
    "docker",
    ["run", "--rm", "-v", `${projectRoot}:/work`, "-w", "/work", settings.dockerImage, ...args],
    projectRoot,
    timeoutMs,
  );
}

export function forgetDetection(): void {
  cache.clear();
}
