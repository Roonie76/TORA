import path from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const here = path.dirname(fileURLToPath(import.meta.url));

// The TORA chat UI ships as components that live inside Spendsy, so this package has no app and
// no dev server — only the tests, which must run from this repo and in CI. Before this existed
// the 33 tests could only be run from a Spendsy checkout, which meant they were never run here.
export default defineConfig({
  plugins: [react()],
  resolve: {
    // The components import "@shared/utils/cn", which only resolves inside a Spendsy checkout.
    // src/tests/stubs/cn.js is the same implementation, so the tests run here too.
    // The components import two modules that only resolve inside a Spendsy checkout:
    // "@shared/utils/cn" and Spendsy's own "../api". src/tests/stubs holds stand-ins so the
    // tests run here; scripts/install-into-spendsy.mjs checks both exist in the real target.
    // The components import "@shared/utils/cn", which only resolves inside a Spendsy checkout.
    // src/tests/stubs/cn.js is the same implementation. Spendsy's other dependency, "../api",
    // is met by src/api.js sitting where Spendsy keeps it.
    alias: [
      { find: "@shared/utils/cn", replacement: path.resolve(here, "src/tests/stubs/cn.js") },
    ],
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/tests/setupTests.js",
    include: ["src/tests/**/*.test.{js,jsx}"],
    css: true,
  },
});
