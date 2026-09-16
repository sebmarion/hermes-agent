import { describe, expect, it } from "vitest";
import { parseSnapshotReply, snapshotExpiry } from "./snapshot";
const at = "2026-09-15T08:00:00Z";
const payload = (expiresAt?: unknown) => ({ type: "zeus-chat:snapshot-result", load: "1", requestId: "clock", answer: {
  question: "What made money yesterday?", mode: "snapshot", state: "partial", text: "Covered subtotal, not a company total.",
  facts: ["Coverage: 1/7"], limitations: ["Six sources missing"], source: "Executive data", path: "executiveIntelligence.answers.money_yesterday", checkedAt: at,
  ...(expiresAt === undefined ? {} : { expiresAt }),
} });
describe("source-specific quick-answer expiry", () => {
  it("uses the executive sixty-second deadline, not the old universal three minutes", () => {
    const a = parseSnapshotReply(payload("2026-09-15T08:01:00Z"), "clock", "1");
    expect(a).not.toBeNull();expect(snapshotExpiry(a!)).toBe(Date.parse(at) + 60000);
  });
  it("preserves a declared fifteen-minute billing deadline and a conservative old-shell fallback", () => {
    const a = parseSnapshotReply(payload("2026-09-15T08:15:00Z"), "clock", "1");
    expect(snapshotExpiry(a!)).toBe(Date.parse(at) + 900000);
    expect(snapshotExpiry(parseSnapshotReply(payload(), "clock", "1")!)).toBe(Date.parse(at) + 180000);
  });
  it("rejects nonfinite, malformed, naive, reversed and unbounded expiry metadata", () => {
    for (const value of [true, 60000, {}, [], "not-a-date", "2026-09-15T08:01:00", at, "2026-09-15T07:59:00Z", "2026-09-15T08:15:00.001Z"])
      expect(parseSnapshotReply(payload(value), "clock", "1")).toBeNull();
  });
});

it("unknown headline with known facts still has a bounded expiry", () => {
 const r = payload("2026-09-15T08:01:00Z"); r.answer.state="unknown"; r.answer.facts=["POS: 12.34 covered subtotal"];
 const a=parseSnapshotReply(r,"clock","1")!;
 expect(snapshotExpiry(a)).toBe(Date.parse(at)+60000);
 expect(snapshotExpiry({...a,state:"stale"})).toBeNull();
 expect(snapshotExpiry({...a,state:"unavailable"})).toBeNull();
});
