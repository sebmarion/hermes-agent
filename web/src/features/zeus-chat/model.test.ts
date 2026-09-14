import { describe, expect, it } from "vitest";
import { emptyConversation, historyItems, reduceEvent } from "./model";

describe("native Zeus transcript contracts", () => {
  it("keeps streamed conversation readable, separates tools, and never leaks other sessions or reasoning", () => {
    let state = emptyConversation();
    const event = (type: string, payload: Record<string, unknown> = {}) => { state = reduceEvent(state, { type, session_id: "zeus-test", payload }, "zeus-test"); };
    event("message.start"); event("reasoning.delta", { text: "private reasoning must not render" });
    event("message.delta", { text: "I am checking ", rendered: "\u001b[31mterminal" });
    event("message.delta", { text: "the result." });
    event("message.interim", { text: "I am checking the result.", already_streamed: true });
    event("tool.start", { tool_id: "t1", name: "read_file", args: { secret: "not transcript material" } });
    event("tool.complete", { tool_id: "t1", name: "read_file" });
    event("message.delta", { text: "Here is " }); event("message.delta", { text: "the answer." });
    event("message.complete", { text: "Here is the answer." });
    expect(state.busy).toBe(false);
    expect(state.items.map(item => item.text)).toEqual(["I am checking the result.", "read file · finished", "Here is the answer."]);
    expect(state.items.filter(item => item.streaming)).toHaveLength(0);
    expect(reduceEvent(state, { type: "message.delta", session_id: "another-chat", payload: { text: "wrong chat" } }, "zeus-test")).toBe(state);
    expect(historyItems([{ role: "system", text: "private" }, { role: "user", display_kind: "hidden", text: "scaffold" }, { role: "assistant", reasoning: "private" }, { role: "user", text: "Hello" }, { role: "assistant", text: "Hi" }]).map(item => item.text)).toEqual(["Hello", "Hi"]);
  });
  it("keeps approval requests explicit and treats failed turns as errors, not success", () => {
    let state = reduceEvent(emptyConversation(), { type: "approval.request", session_id: "s", payload: { request_id: "a", command: "reviewed action", choices: ["once", "deny"] } }, "s");
    expect(state.pending).toMatchObject({ kind: "approval", request_id: "a" });
    expect(state.busy).toBe(true);
    state = reduceEvent(state, { type: "approval.expire", session_id: "s", payload: { request_id: "other" } }, "s");
    expect(state.pending).not.toBeNull();
    state = reduceEvent(state, { type: "approval.expire", session_id: "s", payload: { request_id: "a" } }, "s");
    expect(state.pending).toBeNull();
    state = reduceEvent(state, { type: "message.complete", session_id: "s", payload: { text: "Partial response", status: "error", error: "Connection failed" } }, "s");
    expect(state.error).toBe("Connection failed"); expect(state.busy).toBe(false);
  });
});
