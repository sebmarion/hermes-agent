// Execute the actual built module graph at a browser URL without network access.
// Only stylesheet load events are supplied by the host; URL construction belongs
// to the generated code and the standard URL implementation.
import { readFileSync } from "node:fs";
import { createContext, SourceTextModule } from "node:vm";

const { chunks, entry, prefix, route } = JSON.parse(readFileSync(0, "utf8"));
const origin = "https://dashboard.example";
const documentUrl = origin + prefix + route;
const links = [];
const document = {
  createElement() {
    const listeners = new Map();
    const link = {
      relList: { supports: () => true },
      addEventListener(name, callback) { listeners.set(name, callback); },
      setAttribute(name, value) { this[name] = value; },
      loaded() { listeners.get("load")?.(); },
    };
    let href = "";
    Object.defineProperty(link, "href", {
      get: () => href,
      set: (value) => { href = new URL(value, documentUrl).href; },
    });
    return link;
  },
  getElementsByTagName: () => links,
  querySelector: () => null,
  head: {
    appendChild(link) {
      links.push(link);
      queueMicrotask(() => link.loaded());
    },
  },
};
const browser = { URL, document, console };
browser.window = browser;
const context = createContext(browser);
const files = new Map(chunks.map(({ fileName, code }) => [fileName, code]));
const modules = new Map();

function moduleFor(url) {
  if (modules.has(url)) return modules.get(url);
  const pathname = new URL(url).pathname;
  if (!pathname.startsWith(prefix + "/")) throw new Error("Module escaped mount: " + url);
  const filename = pathname.slice(prefix.length + 1);
  const code = files.get(filename);
  if (code === undefined) throw new Error("Unknown built module: " + filename);
  const module = new SourceTextModule(code, {
    context,
    identifier: url,
    initializeImportMeta(meta) { meta.url = url; },
    async importModuleDynamically(specifier, referrer) {
      const child = moduleFor(new URL(specifier, referrer.identifier).href);
      if (child.status === "unlinked") await child.link(linker);
      if (child.status === "linked") await child.evaluate();
      return child;
    },
  });
  modules.set(url, module);
  return module;
}

function linker(specifier, referrer) {
  return moduleFor(new URL(specifier, referrer.identifier).href);
}

const main = moduleFor(origin + prefix + "/" + entry);
await main.link(linker);
await main.evaluate();
const page = await context.loadPage();
process.stdout.write(JSON.stringify({
  preloads: links.map(({ href, rel }) => ({ href, rel })),
  asset: page.asset,
}));
