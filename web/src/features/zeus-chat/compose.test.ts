import { describe, expect, it } from "vitest";
import { createComposeHandler } from "./compose";

function fixture() {
  const parent = {} as Window;
  const state = { draft: "", busy: false, sending: false, loading: false, uncertain: false, pending: null as unknown };
  const writes: string[] = [], receipts: Array<{ requestId: string; result: string }> = [];
  const handle = createComposeHandler({ parent, origin: "https://zeus.example", load: "9", read: () => state,
    setDraft: text => { state.draft = text; writes.push(text); }, acknowledge: result => receipts.push(result) });
  const event = (id = "request-0000000001", text = "What needs my attention?") => ({ source: parent, origin: "https://zeus.example", data: { type: "zeus-chat:compose", load: "9", requestId: id, text } });
  return { state, writes, receipts, handle, event };
}
describe("founder question draft handoff", () => {
  it("accepts only a bounded correlated parent request and acknowledges replays without changing the draft", () => {
    const f = fixture();
    for (const event of [{ ...f.event(), source: {} as Window }, { ...f.event(), origin: "https://other.example" },
      { ...f.event(), data: { ...f.event().data, load: "8" } }, { ...f.event(), data: null },
      f.event("bad"), f.event(undefined, " "), f.event(undefined, "x".repeat(4001))]) f.handle(event);
    expect(f.writes).toEqual([]); expect(f.receipts).toEqual([]);
    f.handle(f.event()); expect(f.writes).toEqual(["What needs my attention?"]);
    f.state.draft = "Newer owner draft"; f.handle(f.event(undefined, "Replay must not overwrite"));
    expect(f.state.draft).toBe("Newer owner draft"); expect(f.writes).toHaveLength(1);
    expect(f.receipts.map(r => r.result)).toEqual(["prepared", "prepared"]);
  });
  it("never overwrites input, a restoring session, an uncertain send, a running turn or pending approval", () => {
    for (const occupied of [{ draft: "Existing draft" }, { draft: " " }, { busy: true }, { loading: true }, { sending: true }, { uncertain: true }, { pending: { kind: "approval" } }]) {
      const f = fixture(); Object.assign(f.state, occupied); const before = structuredClone(f.state); f.handle(f.event());
      expect(f.state).toEqual(before); expect(f.writes).toEqual([]); expect(f.receipts[0].result).toBe("occupied");
      Object.assign(f.state, { draft: "", busy: false, loading: false, sending: false, uncertain: false, pending: null });
      f.handle(f.event()); expect(f.writes).toEqual([]); expect(f.receipts.at(-1)?.result).toBe("occupied");
    }
    const f = fixture();
    for (let i = 0; i < 65; i++) { f.state.draft = ""; f.handle(f.event(`request-${String(i).padStart(10, "0")}`)); }
    expect(f.writes).toHaveLength(64); f.handle(f.event("request-0000000000", "old replay")); expect(f.writes).toHaveLength(64);
  });
});
