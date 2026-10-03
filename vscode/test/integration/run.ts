// Launches a real VS Code with the extension loaded, on a copy of the fixture
// project, and runs the suite inside it: `npm run test:editor`.

import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { runTests } from "@vscode/test-electron";

async function main(): Promise<void> {
  const extensionDevelopmentPath = path.resolve(__dirname, "../../..");
  const fixtures = path.join(extensionDevelopmentPath, "test", "fixtures");

  // A throwaway copy: the tests write waivers.yaml and reports into it.
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "api-guard-ext-"));
  fs.cpSync(path.join(fixtures, "project"), workspace, { recursive: true });
  const fake = path.join(fixtures, "fake-api-guard.js");
  fs.chmodSync(fake, 0o755);
  fs.mkdirSync(path.join(workspace, ".vscode"), { recursive: true });
  fs.writeFileSync(
    path.join(workspace, ".vscode", "settings.json"),
    JSON.stringify({ "apiGuard.runner": "cli", "apiGuard.cliPath": fake, "apiGuard.checkOnSave": false }),
  );

  await runTests({
    extensionDevelopmentPath,
    extensionTestsPath: path.resolve(__dirname, "suite"),
    launchArgs: [workspace, "--disable-extensions", "--disable-workspace-trust"],
  });
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
