import { describe, expect, it } from "vitest";
import { parseSnapshotReply } from "./snapshot";
const answer = { question: "What is our software MRR?", mode: "snapshot", text: "Money: €99.00.", state: "partial", facts: ["Coverage: 2/3"], limitations: ["Not cash"], source: "Billing", path: "sales.aggregate", checkedAt: "2026-09-14T20:00:00Z" };
const reply = () => ({ type: "zeus-chat:snapshot-result", load: "1", requestId: "qa", answer: structuredClone(answer) });
describe("snapshot reply contract", () => {
  it("accepts correlated display-only evidence and preserves uncertainty", () => { const r = reply(); Object.assign(r.answer, { html: "<script>bad</script>", action: "spend" }); const parsed = parseSnapshotReply(r, "qa", "1"); expect(parsed).toEqual(answer); expect(parsed?.state).toBe("partial"); });
  it("rejects malformed, stale-request, cross-generation and unbounded replies", () => { for (const r of [null, [], {}, { ...reply(), answer: { ...answer, mode: ["snapshot"] } }, { ...reply(), answer: { ...answer, state: ["current"] } }, { ...reply(), requestId: "old" }, { ...reply(), load: "2" }, { ...reply(), answer: { ...answer, facts: [null] } }, { ...reply(), answer: { ...answer, state: "healthy" } }, { ...reply(), answer: { ...answer, checkedAt: "2026-09-14T20:00:00" } }, { ...reply(), answer: { ...answer, text: "x".repeat(4001) } }]) expect(parseSnapshotReply(r, "qa", "1")).toBeNull(); });
});
