import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Markdown } from "./Markdown";

describe("markdown link boundaries", () => {
  it("renders a blocked function-shaped URL deliberately without leaving a dangling parenthesis", () => {
    for (const href of ["javascript:alert(1)", "data:text/html,hello", "vbscript:msgbox(1)", "file:///etc/passwd"]) {
      const html = renderToStaticMarkup(<Markdown content={`[Unsafe link](${href})`} />);
      expect(html).not.toContain("<a "); expect(html).toContain("link blocked");
      expect(html).not.toContain("Unsafe link)"); expect(html).not.toContain(href);
    }
  });
  it("preserves legitimate URL parentheses and keeps raw markup escaped", () => {
    const html = renderToStaticMarkup(<Markdown content={'[Topic](https://example.com/wiki/Test_(topic))\n<script>alert(1)</script>\n`[not a link](javascript:alert(1))`'} />);
    expect(html).toContain('href="https://example.com/wiki/Test_(topic)"');
    expect(html).toContain("&lt;script&gt;"); expect(html).not.toContain("<script>");
    expect((html.match(/<a /g) || []).length).toBe(1);
  });
});
