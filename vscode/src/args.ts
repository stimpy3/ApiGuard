// The arguments the editor passes to `api-guard check`. No vscode import.
//
// Every check asks for conformance only if the API is running (--if-running):
// while editing it often isn't, and then conformance is "not checked" rather
// than an error. Nothing to switch on or off.
//
// When api-guard runs in Docker, `localhost` inside the container is the
// container itself, not this machine, so a runtime.url on localhost would
// never answer. It is translated to host.docker.internal, Docker's name for
// the machine running it; api-guard.yaml stays as it is for CI.

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"]);

export function urlForDocker(url: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (!LOCAL_HOSTS.has(parsed.hostname)) {
    return null; // already reachable from the container as written
  }
  parsed.hostname = "host.docker.internal";
  return parsed.toString().replace(/\/$/, url.endsWith("/") ? "/" : "");
}

export function checkArgs(runner: "cli" | "docker", runtimeUrl: string | null): string[] {
  const args = ["check", "--if-running"];
  if (runner === "docker" && runtimeUrl) {
    const translated = urlForDocker(runtimeUrl);
    if (translated) {
      args.push("--url", translated);
    }
  }
  return args;
}
