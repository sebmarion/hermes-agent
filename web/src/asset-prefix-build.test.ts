import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, expect, test } from "vitest";
import { build } from "vite";
import dashboardConfig from "../vite.config";

let fixture: string;
let chunks: { fileName: string; code: string; isEntry: boolean }[];
let entry: string;
let image: string;
let html: string;

beforeAll(async () => {
  fixture = mkdtempSync(path.join(tmpdir(), "hermes-web-assets-"));
  writeFileSync(path.join(fixture, "package.json"), '{"type":"module"}');
  writeFileSync(path.join(fixture, "index.html"), '<script type="module" src="/main.js"></script>');
  writeFileSync(path.join(fixture, "main.js"), 'globalThis.loadPage = () => import("./page.js");');
  // Keep the lazy boundary above the production bundler's small-chunk threshold.
  writeFileSync(path.join(fixture, "page.js"),
    'import "./page.css"; import asset from "./logo.svg"; export { asset }; export const content = ' +
    JSON.stringify("x".repeat(25_000)) + ";");
  writeFileSync(path.join(fixture, "page.css"), ".page { color: red; }");
  writeFileSync(path.join(fixture, "logo.svg"), '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>');

  const result = await build({
    ...dashboardConfig,
    root: fixture,
    configFile: false,
    publicDir: false,
    logLevel: "silent",
    build: { ...dashboardConfig.build, write: false, minify: false, assetsInlineLimit: 0 },
  });
  const output = (Array.isArray(result) ? result : [result]).flatMap((bundle) => {
    if (!("output" in bundle)) throw new Error("Expected a completed dashboard build");
    return bundle.output;
  });
  chunks = output.filter((item) => item.type === "chunk");
  entry = chunks.find((item) => item.isEntry)!.fileName;
  image = output.find((item) => item.fileName.endsWith(".svg"))!.fileName;
  const document = output.find((item) => item.type === "asset" && item.fileName === "index.html");
  if (!document || document.type !== "asset") throw new Error("Missing built entry document");
  html = String(document.source);
});

afterAll(() => {
  if (fixture) rmSync(fixture, { recursive: true, force: true });
});

test.each([
  ["", "/chat"],
  ["", "/chat/session"],
  ["/ai", "/chat"],
  ["/ai", "/chat/session"],
  ["/nested/hermes", "/chat/session"],
])("lazy assets stay on mount %s from route %s", (prefix, route) => {
  const runner = fileURLToPath(new URL("./test-utils/built-assets-runner.mjs", import.meta.url));
  const result = spawnSync(process.execPath, ["--experimental-vm-modules", runner], {
    input: JSON.stringify({ chunks, entry, prefix, route }),
    encoding: "utf8",
    timeout: 10_000,
  });
  expect(result.status, result.stderr).toBe(0);
  const observed = JSON.parse(result.stdout) as {
    preloads: { href: string; rel: string }[];
    asset: string;
  };
  expect(observed.preloads.some((link) => link.rel === "stylesheet")).toBe(true);
  for (const link of observed.preloads) {
    expect(link.href).toMatch(new RegExp("^https://dashboard\\.example" + prefix + "/assets/"));
  }
  expect(new URL(observed.asset, "https://dashboard.example" + prefix + route).href)
    .toBe("https://dashboard.example" + prefix + "/" + image);
});

test("the entry script stays origin-rooted for server prefixing on nested routes", () => {
  const script = html.match(/<script[^>]+src="([^"]+)"/)?.[1];
  expect(script).toBeDefined();
  expect(new URL(script!, "https://dashboard.example/chat/session").pathname).toBe("/" + entry);
});
