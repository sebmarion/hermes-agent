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

const fixtureChoices = { model: "fixture-default", provider: "qa", providers: [{ slug: "qa", name: "QA Provider", models: ["fixture-default", "fixture-fast", "fixture-expensive"] }] };
it("selects a catalogue model with an explicit session pin, preserves draft/history and confirms expensive picks", async () => {
  await settle();
  let activeModel = "fixture-default";
  mocks.request.mockImplementation(async (method: string, params: Record<string, unknown>) => {
    if (method === "model.options") return fixtureChoices;
    if (method === "session.resume" || method === "session.activate") return { session_id: "runtime", session_key: "stored", messages: [{ role: "user", text: "Existing history" }], info: { model: activeModel, provider: "qa" } };
    if (method === "config.set" && params.confirm_expensive_model) activeModel = "fixture-expensive";
    if (method === "config.set") return params.confirm_expensive_model ? { key: "model", value: "fixture-expensive", scope: "session" } : { key: "model", value: "fixture-expensive", confirm_required: true, confirm_message: "Review the higher cost" };
    return {};
  });
  await controller.open("stored"); controller.setDraft("Keep my unsent question");
  const options = await controller.loadModels(); const selected = options.choices[2];
  const warning = await controller.changeModel(selected);
  expect(warning.confirm_required).toBe(true); expect(controller.getSnapshot().model).toBe("fixture-default");
  expect(controller.getSnapshot().draft).toBe("Keep my unsent question");
  await controller.changeModel(selected, true);
  expect(controller.getSnapshot()).toMatchObject({ model: "fixture-expensive", provider: "qa", modelChanging: false, draft: "Keep my unsent question", storedId: "stored" });
  expect(controller.getSnapshot().items[0].text).toBe("Existing history");
  expect(mocks.request.mock.calls.filter(call => call[0] === "prompt.submit")).toHaveLength(0);
  const changes = mocks.request.mock.calls.filter(call => call[0] === "config.set");
  expect(changes).toHaveLength(2);
  expect(changes[0][1]).toMatchObject({ profile: "zeus-os", session_id: "runtime", key: "model", value: "fixture-expensive --provider qa --session", confirm_expensive_model: false });
  expect(changes[1][1].confirm_expensive_model).toBe(true);
});
it("never replays an unconfirmed model switch or sends through it, and only authoritative resume clears uncertainty", async () => {
  await settle();
  mocks.request.mockImplementation(async (method: string) => {
    if (method === "model.options") return fixtureChoices;
    if (method === "session.create") return { session_id: "runtime", stored_session_id: "stored", info: { model: "fixture-default", provider: "qa" } };
    if (method === "config.set") throw new Error("Lost model acknowledgement");
    return { session_id: "runtime", session_key: "stored", messages: [], info: { model: "fixture-fast", provider: "qa" } };
  });
  controller.setDraft("Unsent draft");const options = await controller.loadModels();
  await expect(controller.changeModel(options.choices[1])).rejects.toThrow("Lost model acknowledgement");
  expect(controller.getSnapshot()).toMatchObject({ modelChanging: false, modelUncertain: true, draft: "Unsent draft" });
  await controller.send();await expect(controller.changeModel(options.choices[1])).rejects.toThrow();
  expect(mocks.request.mock.calls.filter(call => call[0] === "prompt.submit")).toHaveLength(0);
  expect(mocks.request.mock.calls.filter(call => call[0] === "config.set")).toHaveLength(1);
  await controller.open("stored", true);
  expect(controller.getSnapshot()).toMatchObject({ model: "fixture-fast", modelUncertain: false, draft: "Unsent draft" });
  expect(mocks.request.mock.calls.filter(call => call[0] === "config.set")).toHaveLength(1);
});

it("rejects a misleading acknowledgement and releases pre-submission model spinners on restart", async () => {
  await settle();
  mocks.request.mockImplementation(async (method: string) => {
    if (method === "model.options") return fixtureChoices;
    if (method === "session.create") return { session_id: "runtime", stored_session_id: "stored" };
    if (method === "config.set") return { key: "model", value: "fixture-fast", scope: "session" };
    return { session_id: "runtime", session_key: "stored", info: { model: "fixture-fast", provider: "WRONG_PROVIDER" } };
  });
  const options = await controller.loadModels();
  await expect(controller.changeModel(options.choices[1])).rejects.toThrow("did not confirm");
  expect(controller.getSnapshot().modelUncertain).toBe(true);
  expect(controller.getSnapshot().model).not.toBe("fixture-fast");
  controller.newChat();
  let create: (value: unknown) => void = () => {};
  mocks.request.mockImplementation((method: string) => method === "model.options" ? Promise.resolve(fixtureChoices) : new Promise(resolve => { create = resolve; }));
  await controller.loadModels();const operation = controller.changeModel(options.choices[1]);await settle();
  expect(controller.getSnapshot().modelChanging).toBe(true);controller.stop();
  create({ session_id: "orphan", stored_session_id: "orphan-stored" });await expect(operation).rejects.toThrow("conversation changed");
  controller.start();await settle();expect(controller.getSnapshot().modelChanging).toBe(false);
});
it("a concurrent remote response queues the verified choice for next reply without presenting it as already active", async () => {
  await settle();
  mocks.request.mockImplementation(async (method: string) => {
    if (method === "model.options") return fixtureChoices;
    if (method === "session.create") return { session_id: "runtime", stored_session_id: "stored" };
    if (method === "config.set") return { key: "model", value: "fixture-fast", scope: "session", deferred: true };
    return { session_id: "runtime", session_key: "stored", running: true, info: { model: "fixture-fast", provider: "qa" } };
  });
  const options = await controller.loadModels();await controller.changeModel(options.choices[1]);
  expect(controller.getSnapshot()).toMatchObject({ modelDeferred: true, model: "fixture-fast", modelUncertain: false });
  expect(controller.getSnapshot().modelNotice).toContain("current response is unchanged");
});

it("waits for the real runtime metadata without resending a switch or a prompt when new-session resume is only a lazy default", async () => {
  await settle();let activations = 0;
  mocks.request.mockImplementation(async (method: string) => {
    if (method === "model.options") return fixtureChoices;
    if (method === "session.create") return { session_id: "runtime", stored_session_id: "stored", info: { model: "fixture-default", lazy: true } };
    if (method === "config.set") return { key: "model", value: "fixture-fast", scope: "session" };
    if (method === "session.activate") { activations++; return { session_id: "runtime", session_key: "stored", info: activations === 1 ? { model: "fixture-default", lazy: true } : { model: "fixture-fast", provider: "qa" } }; }
    throw new Error("Unexpected request: " + method);
  });
  const options = await controller.loadModels();await controller.changeModel(options.choices[1]);
  expect(controller.getSnapshot()).toMatchObject({ model: "fixture-fast", provider: "qa", modelUncertain: false, modelChanging: false });
  expect(activations).toBe(2);
  expect(mocks.request.mock.calls.filter(call => call[0] === "config.set")).toHaveLength(1);
  expect(mocks.request.mock.calls.filter(call => call[0] === "prompt.submit")).toHaveLength(0);
});

it("restores the chosen model before the first prompt even when lazy resume reports the profile default", async () => {
  await settle();
  mocks.request.mockImplementation(async (method: string) => {
    if (method === "session.resume") return { session_id: "runtime", stored_session_id: "stored", messages: [], info: { model: "fixture-default", lazy: true } };
    if (method === "session.activate") return { session_id: "runtime", session_key: "stored", running: false, info: { model: "fixture-fast", provider: "qa" } };
    throw new Error("Unexpected request: " + method);
  });
  await controller.open("stored");
  expect(controller.getSnapshot()).toMatchObject({ storedId: "stored", model: "fixture-fast", provider: "qa", modelUncertain: false, loading: false });
  expect(mocks.request.mock.calls.filter(call => call[0] === "config.set" || call[0] === "prompt.submit")).toHaveLength(0);
});

it("does not combine an incomplete recovery response with a stale provider or unblock sends", async () => {
  await settle();
  mocks.request.mockImplementation(async (method: string) => {
    if (method === "model.options") return fixtureChoices;
    if (method === "session.create") return { session_id: "runtime", stored_session_id: "stored", info: { model: "fixture-default", provider: "qa" } };
    if (method === "config.set") throw new Error("Lost acknowledgement");
    return { session_id: "runtime", session_key: "stored", messages: [], info: { model: "fixture-fast" } };
  });
  const options = await controller.loadModels();
  await expect(controller.changeModel(options.choices[1])).rejects.toThrow("Lost acknowledgement");
  const client = mocks.clients.at(-1) as { events: Set<(event: unknown) => void> };
  const emit = (info: unknown) => client.events.forEach(fn => fn({ type: "session.info", session_id: "runtime", payload: info }));
  emit({ model: "fixture-fast" });
  expect(controller.getSnapshot()).toMatchObject({ model: "fixture-default", provider: "qa", modelUncertain: true });
  await controller.open("stored", true);
  expect(controller.getSnapshot()).toMatchObject({ model: "fixture-default", provider: "qa", modelUncertain: true });
  controller.setDraft("Not yet safe to send"); await controller.send();
  expect(mocks.request.mock.calls.filter(call => call[0] === "prompt.submit")).toHaveLength(0);
  emit({ model: "fixture-fast", provider: "qa" });
  expect(controller.getSnapshot()).toMatchObject({ model: "fixture-fast", provider: "qa", modelUncertain: false });
});

it("keeps an abandoned unconfirmed switch out of a new chat across a stop/start lifecycle", async () => {
  await settle();let creates = 0;
  mocks.request.mockImplementation(async (method: string) => {
    if (method === "model.options") return fixtureChoices;
    if (method === "session.create") { creates++; return { session_id: `runtime-${creates}`, stored_session_id: `stored-${creates}`, info: { model: "fixture-default", provider: "qa" } }; }
    if (method === "config.set") throw new Error("Lost acknowledgement");
    if (method === "prompt.submit") return { accepted: true };
    throw new Error("Unexpected request: " + method);
  });
  const options = await controller.loadModels();
  await expect(controller.changeModel(options.choices[1])).rejects.toThrow("Lost acknowledgement");
  controller.newChat(); controller.stop(); controller.start(); await settle();
  expect(controller.getSnapshot()).toMatchObject({ storedId: null, modelUncertain: false, modelChanging: false });
  controller.setDraft("A separate conversation"); await controller.send();
  const submits = mocks.request.mock.calls.filter(call => call[0] === "prompt.submit");
  expect(submits).toHaveLength(1); expect(submits[0][1].session_id).toBe("runtime-2");
  expect(mocks.request.mock.calls.filter(call => call[0] === "config.set")).toHaveLength(1);
});
