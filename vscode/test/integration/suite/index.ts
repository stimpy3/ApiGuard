// Mocha entry point that VS Code calls inside the test editor.

import * as path from "path";
import Mocha from "mocha";

export function run(): Promise<void> {
  const mocha = new Mocha({ ui: "tdd", timeout: 60_000, color: false });
  mocha.addFile(path.resolve(__dirname, "editor.itest.js"));
  return new Promise((resolve, reject) => {
    mocha.run((failures) => (failures ? reject(new Error(`${failures} editor test(s) failed`)) : resolve()));
  });
}
