// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ clients: [] as unknown[], request: vi.fn(), history: vi.fn() }));
vi.mock("@hermes/shared", () => {
  class JsonRpcGatewayError extends Error {}
  class JsonRpcGatewayClient {
    connectionState = "idle";
    states = new Set<(state: string) => void>();
    events = new Set<(event: unknown) => void>();
    constructor() { mocks.clients.push(this); }
    onState(fn: (state: string) => void) { this.states.add(fn); fn(this.connectionState); return () => this.states.delete(fn); }
    onEvent(fn: (event: unknown) => void) { this.events.add(fn); return () => this.events.delete(fn); }
    async connect() { this.connectionState = "open"; this.states.forEach(fn => fn("open")); }
    close() { this.connectionState = "closed"; this.states.forEach(fn => fn("closed")); }
    request(method: string, params: Record<string, unknown>) { return mocks.request(method, params); }
  }
  return { JsonRpcGatewayClient, JsonRpcGatewayError };
});
vi.mock("@/lib/api", () => ({ api: { getSessions: mocks.history }, buildWsUrl: async () => "ws://localhost/api/ws" }));
import { ZeusChatController } from "./controller";
import { JsonRpcGatewayError } from "@hermes/shared";
let controller: ZeusChatController;
const settle = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); mocks.request.mockReset(); mocks.history.mockReset();
  mocks.history.mockResolvedValue({ sessions: [], total: 0 });
  mocks.request.mockImplementation(async (method: string) => method === "session.create" ? { session_id: "runtime", stored_session_id: "stored", messages: [] } : { session_id: "runtime", session_key: "stored", messages: [], running: false });
  controller = new ZeusChatController(); controller.start();
});
afterEach(() => controller.stop());

describe("Zeus gateway and draft ownership", () => {
  it("never clears a newer draft or silently forks a conversation whose restore failed", async () => {
    await settle();
    let acknowledge: (value: unknown) => void = () => {};
    mocks.request.mockImplementation((method: string) => method === "session.create" ? Promise.resolve({ session_id: "runtime", stored_session_id: "stored" }) : new Promise(resolve => { acknowledge = resolve; }));
    controller.setDraft("First message"); const sending = controller.send(); await settle();
    controller.setDraft("Next draft typed while sending"); acknowledge({ accepted: true }); await sending;
    expect(controller.getSnapshot().draft).toBe("Next draft typed while sending");
    const submit = mocks.request.mock.calls.find(call => call[0] === "prompt.submit");
    expect(mocks.request.mock.calls.find(call => call[0] === "session.create")?.[1]).toMatchObject({ source: "web", profile: "zeus-os" });
    expect(submit?.[1]).toMatchObject({ profile: "zeus-os", session_id: "runtime", text: "First message" });
    mocks.request.mockRejectedValue(new JsonRpcGatewayError("Session unavailable"));
    await controller.open("existing-stored");
    controller.setDraft("Do not create a replacement"); const before = mocks.request.mock.calls.length;
    await controller.send();
    expect(mocks.request.mock.calls.length).toBe(before);
    expect(controller.getSnapshot().error).toContain("Restore this conversation");
  });
  it("does not replay an uncertain send after reconnect or reload and keeps it tied to its conversation", async () => {
    await settle();
    mocks.request.mockImplementation(async (method: string) => {
      if (method === "session.create") return { session_id: "runtime", stored_session_id: "stored" };
      if (method === "prompt.submit") throw new Error("WebSocket closed before acknowledgement");
      return { session_id: "runtime", session_key: "stored", messages: [{ role: "user", text: "Exactly once" }, { role: "assistant", text: "Received" }], running: false };
    });
    controller.setDraft("Exactly once"); await controller.send();
    expect(controller.getSnapshot().uncertain).toBe(true);
    await controller.reconnect(); await controller.send();
    expect(mocks.request.mock.calls.filter(call => call[0] === "prompt.submit")).toHaveLength(1);
    controller.stop(); controller = new ZeusChatController(); controller.start(); await settle();
    expect(controller.getSnapshot().uncertain).toBe(true); expect(controller.getSnapshot().draft).toBe("Exactly once");
    await controller.send(); expect(mocks.request.mock.calls.filter(call => call[0] === "prompt.submit")).toHaveLength(1);
    controller.newChat(); expect(controller.getSnapshot().uncertain).toBe(false);
    await controller.open("stored"); expect(controller.getSnapshot().uncertain).toBe(true);
    controller.confirmReviewed(); expect(controller.getSnapshot().uncertain).toBe(false);
  });
  it("surfaces the next unresolved approval without granting it implicitly", async () => {
    await settle(); await controller.open("stored");
    const client = mocks.clients.at(-1) as { events: Set<(event: unknown) => void> };
    const pending = { kind: "approval" as const, request_id: "latest", choices: ["once", "deny"], command: "Latest queued action" };
    client.events.forEach(fn => fn({ type: "approval.request", session_id: "runtime", payload: pending }));
    mocks.request.mockImplementation(async (method: string) => method === "approval.respond" ? { resolved: 1 } : { session_id: "runtime", pending_approval: { request_id: "earlier", choices: ["once", "deny"], command: "Another action still needs approval" } });
    await controller.respond(pending, "deny");
    expect(controller.getSnapshot().pending).toMatchObject({ kind: "approval", request_id: "earlier" });
    expect(mocks.request.mock.calls.filter(call => call[0] === "approval.respond").map(call => call[1].choice)).toEqual(["deny"]);
  });

});
