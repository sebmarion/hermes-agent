import { JsonRpcGatewayClient, JsonRpcGatewayError, type ConnectionState, type ServerRequest } from "@hermes/shared";
import { api, buildWsUrl, type SessionInfo } from "@/lib/api";
import { choiceKey, modelSwitchValue, parseModelOptions, type ModelChoice, type ModelOptions, type ModelSwitchResult } from "./model-controls";
import { emptyConversation, historyItems, reduceEvent, type Conversation, type RequestCard } from "./model";

interface SessionResult {
  session_id: string;
  stored_session_id?: string;
  resumed?: string;
  session_key?: string;
  messages?: unknown[];
  running?: boolean;
  pending_approval?: Record<string, unknown>;
  info?: { model?: string; provider?: string; lazy?: boolean };
  inflight?: { assistant?: string; user?: string; status?: string; streaming?: boolean; error?: string };
}
export interface ChatSnapshot extends Conversation {
  connection: ConnectionState;
  sessions: SessionInfo[];
  total: number;
  historyError: string;
  loading: boolean;
  sending: boolean;
  uncertain: boolean;
  storedId: string | null;
  title: string;
  model: string;
  provider: string;
  modelChanging: boolean;
  modelUncertain: boolean;
  modelNotice: string;
  modelDeferred: boolean;
  draft: string;
}
const PROFILE = "zeus-os";
const ACTIVE_KEY = "zeus-chat:active:v1";
const deliveryKey = (id: string) => `zeus-chat:unconfirmed:v1:${id}`;
function unconfirmed(id: string | null): boolean {
  try { return Boolean(id && sessionStorage.getItem(deliveryKey(id))); } catch { return false; }
}
function markDelivery(id: string | null, pending: boolean) {
  if (!id) return;
  try { if (pending) sessionStorage.setItem(deliveryKey(id), "1"); else sessionStorage.removeItem(deliveryKey(id)); } catch { /* In-memory uncertainty still prevents resubmission. */ }
}
const draftKey = (id: string | null) => `zeus-chat:draft:v1:${id || "new"}`;
function readDraft(id: string | null): string {
  try { return sessionStorage.getItem(draftKey(id)) || ""; } catch { return ""; }
}
const errorText = (error: unknown) => error instanceof Error ? error.message : "Something went wrong. Please try again.";
const stringValue = (value: unknown): string => typeof value === "string" ? value : "";
const stringList = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

/** One foreground view; the existing gateway remains the execution and persistence authority. */
export class ZeusChatController {
  private state: ChatSnapshot = {
    ...emptyConversation(), connection: "idle", sessions: [], total: 0, historyError: "", loading: false,
    sending: false, uncertain: false, storedId: null, title: "New conversation", model: "", provider: "", modelChanging: false, modelUncertain: false, modelNotice: "", modelDeferred: false, draft: readDraft(null),
  };
  private listeners = new Set<() => void>();
  private client = new JsonRpcGatewayClient({ requestIdPrefix: "zeus-chat-", requestTimeoutMs: 30000 });
  private runtimeId: string | null = null;
  private modelChoices: ModelChoice[] = [];
  private modelSwitchSubmitted = false;
  private generation = 0;
  private lifecycle = 0;
  private resumeGeneration: number | null = null;
  private stopped = false;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;
  private reconnectFlight: Promise<void> | null = null;
  private retries = 0;
  private disposers: Array<() => void> = [];
  private serverRequests = new Map<string, ServerRequest>();
  private serverRequestCards = new Map<string, RequestCard>();
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  private patch(patch: Partial<ChatSnapshot>) { this.state = { ...this.state, ...patch }; this.listeners.forEach(listener => listener()); }

  private parkServerRequest(request: ServerRequest, card: RequestCard) {
    this.serverRequests.set(request.id, request);
    this.serverRequestCards.set(request.id, card);
    const current = this.state.pending;
    const replacesSameApproval = current?.kind === "approval" && card.kind === "approval" && current.request_id === card.request_id;
    if (!current || current.server_request_id === request.id || replacesSameApproval) {
      this.patch({ pending: card, busy: true, activity: "Needs your reply" });
    }
  }

  private finishServerRequest(id: string) {
    this.serverRequests.delete(id);
    this.serverRequestCards.delete(id);
    if (this.state.pending?.server_request_id !== id) return;
    const next = this.serverRequestCards.values().next().value as RequestCard | undefined;
    this.patch({
      pending: next ?? null,
      activity: next ? "Needs your reply" : "Working…",
    });
  }

  private clearLiveRequests() {
    this.serverRequests.clear();
    this.serverRequestCards.clear();
  }

  private handleServerRequest = (request: ServerRequest): boolean => {
    const params = request.params;
    const sessionId = stringValue(params.session_id);
    const owned = this.runtimeId
      ? sessionId === this.runtimeId
      : Boolean(request.replayed && this.resumeGeneration !== null && sessionId);
    if (!owned) {
      request.decline?.("Zeus web chat is not showing this session.");
      return false;
    }

    if (["terminal.read", "preview.read", "preview.act", "window.read", "tour"].includes(request.method)) {
      const message = "Desktop pane controls are not available in Zeus web chat. Use standard server tools or ask the user in chat.";
      request.respond({ value: JSON.stringify({ ok: false, error: message }) });
      return true;
    }

    if (request.method === "approval") {
      const requestId = stringValue(params.request_id);
      if (!requestId) {
        request.fail(-32602, "approval request is missing request_id");
        return true;
      }
      const card: RequestCard = {
        ...params,
        kind: "approval",
        request_id: requestId,
        server_request_id: request.id,
        choices: stringList(params.choices),
        command: stringValue(params.command),
        description: stringValue(params.description),
      };
      this.parkServerRequest(request, card);
      void this.client.request("approval.received", {
        session_id: sessionId, profile: PROFILE, request_id: requestId,
      }).catch(error => {
        if (this.serverRequests.has(request.id)) this.patch({ error: "Approval acknowledgement failed: " + errorText(error) });
      });
      return true;
    }

    if (request.method === "clarify") {
      const questions = (Array.isArray(params.questions) ? params.questions : [])
        .map(raw => raw && typeof raw === "object" ? raw as Record<string, unknown> : {})
        .filter(question => stringValue(question.qid) && stringValue(question.question).trim())
        .map(question => ({
          qid: stringValue(question.qid),
          question: stringValue(question.question).trim(),
          choices: stringList(question.choices),
          multi_select: question.multi_select === true,
        }));
      if (!questions.length) {
        request.respond({});
        return true;
      }
      const answers = params.answers && typeof params.answers === "object"
        ? Object.fromEntries(Object.entries(params.answers as Record<string, unknown>)
            .filter((entry): entry is [string, string | null] => typeof entry[1] === "string" || entry[1] === null))
        : {};
      this.parkServerRequest(request, {
        kind: "clarify", request_id: request.id, server_request_id: request.id, questions, answers,
      });
      return true;
    }

    if (request.method === "sudo") {
      this.parkServerRequest(request, {
        kind: "sudo", request_id: request.id, server_request_id: request.id,
        command: stringValue(params.command),
      });
      return true;
    }

    if (request.method === "secret") {
      this.parkServerRequest(request, {
        kind: "secret", request_id: request.id, server_request_id: request.id,
        prompt: stringValue(params.prompt),
      });
      return true;
    }

    return false;
  };

  start() {
    this.stopped = false;
    this.lifecycle++;
    this.client = new JsonRpcGatewayClient({ requestIdPrefix: "zeus-chat-", requestTimeoutMs: 30000 });
    this.disposers.push(this.client.onState(connection => {
      this.patch({ connection });
      if ((connection === "closed" || connection === "error") && !this.stopped) this.scheduleReconnect();
    }));
    this.disposers.push(this.client.onRequest(this.handleServerRequest));
    this.disposers.push(this.client.onEvent(event => {
      if (event.type === "session.info" && event.session_id === this.runtimeId && !this.state.modelChanging) {
        const info = event.payload as SessionResult["info"];
        if (info?.model && info.provider && !info.lazy) { this.modelSwitchSubmitted = false; this.patch({ model: info.model, provider: info.provider, modelUncertain: false }); }
      }
      if (event.type === "message.start" && event.session_id === this.runtimeId && this.state.modelDeferred) this.patch({ modelDeferred: false, modelNotice: "Using the selected model for this reply." });
      const next = reduceEvent(this.state, event, this.runtimeId);
      if (next !== this.state) this.patch(next);
      if (event.type === "request.cancel" && event.session_id === this.runtimeId) {
        const cancelled = event.payload as { id?: string };
        if (typeof cancelled.id === "string") this.finishServerRequest(cancelled.id);
      }
      if (event.type === "message.complete" && event.session_id === this.runtimeId) void this.refreshHistory();
    }));
    let stored: string | null = null;
    try { stored = localStorage.getItem(ACTIVE_KEY); } catch { /* Storage may be denied in private mode. */ }
    if (stored) this.patch({ storedId: stored, draft: readDraft(stored), loading: true, uncertain: unconfirmed(stored) });
    void this.reconnect();
    void this.refreshHistory();
    window.addEventListener("online", this.wake);
    document.addEventListener("visibilitychange", this.wake);
  }
  stop() {
    this.stopped = true; this.lifecycle++; this.generation++;
    this.clearLiveRequests();
    this.resumeGeneration = null;
    this.patch({ modelChanging: false, modelUncertain: this.state.modelUncertain || this.modelSwitchSubmitted, pending: null });
    this.reconnectFlight = null;
    clearTimeout(this.retryTimer); this.retryTimer = undefined;
    this.disposers.forEach(dispose => dispose()); this.disposers = [];
    window.removeEventListener("online", this.wake);
    document.removeEventListener("visibilitychange", this.wake);
    this.client.close();
  }
  private wake = () => { if (!document.hidden && this.client.connectionState !== "open") void this.reconnect(); };
  private scheduleReconnect() {
    if (this.retryTimer || this.stopped) return;
    this.retryTimer = setTimeout(() => { this.retryTimer = undefined; void this.reconnect(); }, Math.min(15000, 1000 * 2 ** this.retries++));
  }
  reconnect = (): Promise<void> => {
    if (this.reconnectFlight) return this.reconnectFlight;
    const lifecycle = this.lifecycle;
    const client = this.client;
    const flight = (async () => {
      try {
        const url = await buildWsUrl("/api/ws");
        if (this.stopped || lifecycle !== this.lifecycle) return;
        await client.connect(url);
        if (this.stopped || lifecycle !== this.lifecycle) return;
        this.retries = 0;
        if (this.state.storedId) await this.open(this.state.storedId, true);
        else this.patch({ loading: false, error: "" });
      } catch (error) {
        if (!this.stopped && lifecycle === this.lifecycle) { this.patch({ loading: false, error: errorText(error) }); this.scheduleReconnect(); }
      }
    })().finally(() => { if (this.reconnectFlight === flight) this.reconnectFlight = null; });
    this.reconnectFlight = flight;
    return flight;
  };

  refreshHistory = async (more = false) => {
    try {
      const result = await api.getSessions(50, more ? this.state.sessions.length : 0, PROFILE, "recent");
      if (this.stopped) return;
      const rows = more ? [...this.state.sessions, ...result.sessions] : result.sessions;
      const sessions = [...new Map(rows.map(row => [row.id, row])).values()];
      const current = sessions.find(row => row.id === this.state.storedId);
      this.patch({ sessions, total: result.total, historyError: "", ...(current ? { title: current.title || current.preview || "Conversation" } : {}) });
    } catch (error) { if (!this.stopped) this.patch({ historyError: errorText(error) }); }
  };
  setDraft = (draft: string) => {
    this.patch({ draft });
    try { if (draft) sessionStorage.setItem(draftKey(this.state.storedId), draft); else sessionStorage.removeItem(draftKey(this.state.storedId)); } catch { /* Keep the in-memory draft. */ }
  };
  private remember(id: string | null) {
    try { if (id) localStorage.setItem(ACTIVE_KEY, id); else localStorage.removeItem(ACTIVE_KEY); } catch { /* Server history remains available. */ }
  }
  newChat = () => {
    if (this.state.sending || this.state.modelChanging) return;
    this.clearLiveRequests();
    this.resumeGeneration = null;
    this.generation++; this.runtimeId = null; this.modelSwitchSubmitted = false; this.remember(null);
    this.patch({ ...emptyConversation(), storedId: null, draft: readDraft(null), title: "New conversation", model: "", provider: "", modelUncertain: false, modelNotice: "", modelDeferred: false, loading: false, uncertain: false });
  };
  open = async (id: string, reconnect = false) => {
    if ((this.state.sending || this.state.modelChanging) && !reconnect) return;
    const generation = ++this.generation;
    const same = this.state.storedId === id;
    if (!same) this.modelSwitchSubmitted = false;
    if (reconnect && this.state.modelChanging) this.patch({ modelChanging: false, modelUncertain: true });
    this.clearLiveRequests();
    this.runtimeId = null;
    this.resumeGeneration = generation;
    const row = this.state.sessions.find(session => session.id === id);
    this.patch({ ...(same ? { pending: null } : { ...emptyConversation(), model: "", provider: "", modelUncertain: false, modelNotice: "", modelDeferred: false }), storedId: id, title: row?.title || row?.preview || "Conversation", loading: true, modelUncertain: true, draft: same ? this.state.draft : readDraft(id), uncertain: unconfirmed(id), error: "" });
    this.remember(id);
    try {
      let result = await this.client.request<SessionResult>("session.resume", { session_id: id, profile: PROFILE, source: "web", cols: 96 });
      if (this.resumeGeneration === generation) this.resumeGeneration = null;
      if (generation !== this.generation || this.stopped) { this.clearLiveRequests(); return; }
      this.runtimeId = result.session_id;
      for (const [requestId, live] of this.serverRequests) {
        if (stringValue(live.params.session_id) !== result.session_id) this.finishServerRequest(requestId);
      }
      if (result.info?.lazy || !result.info?.model || !result.info.provider) {
        this.patch({ modelUncertain: true });
        const runtime = await this.readModelRuntime(result.session_id, generation);
        if (generation !== this.generation || this.stopped) return;
        if (runtime.session_id !== result.session_id || runtime.info?.lazy || !runtime.info?.model || !runtime.info?.provider) throw new Error("The restored model is not yet verified. Refresh this conversation before sending.");
        result = { ...result, info: runtime.info, running: runtime.running ?? result.running };
      }
      const storedId = result.stored_session_id || result.session_key || result.resumed || id;
      this.remember(storedId);
      const items = historyItems(result.messages || []);
      const failed = result.inflight?.status === "error";
      if ((result.running || failed) && result.inflight?.user && items.findLast(item => item.role === "user")?.text !== result.inflight.user) {
        items.push({ id: "resumed-user", role: "user", text: result.inflight.user });
      }
      if ((result.running || failed) && result.inflight?.assistant && items.at(-1)?.text !== result.inflight.assistant) {
        items.push({ id: "resumed-stream", role: "assistant", text: result.inflight.assistant, streaming: Boolean(result.running) && !failed });
      }
      if (result.info?.model && result.info?.provider && !result.running) this.modelSwitchSubmitted = false;
      const pending = this.state.pending ?? (result.pending_approval ? { ...result.pending_approval, kind: "approval" } as RequestCard : null);
      this.patch({ storedId, items, loading: false, busy: Boolean(result.running) || Boolean(pending), activity: pending ? "Needs your reply" : result.running ? "Working…" : "", pending, error: failed ? result.inflight?.error || "Zeus could not finish the previous response." : unconfirmed(storedId) ? "A previous send was not confirmed. Check this conversation before sending the saved draft again." : "", ...(!result.info?.lazy && result.info?.model && result.info.provider ? { model: result.info.model, provider: result.info.provider, modelUncertain: false } : {}) });
    } catch (error) {
      if (this.resumeGeneration === generation) this.resumeGeneration = null;
      if (generation === this.generation && !this.stopped) {
        this.clearLiveRequests(); this.runtimeId = null;
        this.patch({ loading: false, pending: null, modelUncertain: true, modelNotice: "The restored model is not verified. Refresh this conversation before sending.", error: "Conversation could not be restored: " + errorText(error) });
      }
    }
  };
  private async ensureSession(title: string) {
    if (this.runtimeId) return;
    if (this.state.storedId) throw new Error("Restore this conversation before changing its model.");
    const generation = this.generation;
    const created = await this.client.request<SessionResult>("session.create", { profile: PROFILE, source: "web", title, close_on_disconnect: false });
    if (generation !== this.generation || this.stopped) throw new Error("The conversation changed. No model change was submitted.");
    if (!created.session_id || !created.stored_session_id) throw new Error("The server did not return a persistent conversation identity.");
    this.runtimeId = created.session_id;
    this.remember(created.stored_session_id);
    this.patch({ storedId: created.stored_session_id, title, model: created.info?.model || this.state.model, provider: created.info?.provider || this.state.provider });
    try { sessionStorage.removeItem(draftKey(null)); } catch { /* Draft stays in memory. */ }
    this.setDraft(this.state.draft);
  }
  loadModels = async (refresh = false): Promise<ModelOptions> => {
    if (this.state.connection !== "open") throw new Error("Reconnect to Zeus to load models.");
    const generation = this.generation;
    const raw = await this.client.request("model.options", { profile: PROFILE, ...(this.runtimeId ? { session_id: this.runtimeId } : {}), refresh, include_unconfigured: false });
    if (generation !== this.generation || this.stopped) throw new Error("The conversation changed. Reopen the model selector.");
    const options = parseModelOptions(raw); this.modelChoices = options.choices;
    if (options.model && !this.state.modelChanging && (!this.state.storedId || !this.state.model)) this.patch({ model: options.model, provider: options.provider });
    return { ...options, model: this.state.model || options.model, provider: this.state.provider || options.provider };
  };
  private async readModelRuntime(sessionId: string, generation: number): Promise<SessionResult> {
    // Unpersisted resume payloads contain profile defaults. The existing activation
    // read returns the real runtime; waiting never repeats a write or sends a prompt.
    let result: SessionResult = { session_id: sessionId };
    for (let attempt = 0; attempt < 20; attempt++) {
      if (generation !== this.generation || this.stopped) throw new Error("The conversation changed during model verification.");
      result = await this.client.request<SessionResult>("session.activate", { profile: PROFILE, session_id: sessionId, omit_messages: true });
      if (!result.info?.lazy) return result;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    return result;
  }
  changeModel = async (choice: ModelChoice, confirmed = false): Promise<ModelSwitchResult> => {
    if (this.state.modelChanging || this.state.sending || this.state.busy || this.state.pending || this.state.loading || this.state.uncertain || this.state.modelUncertain || this.state.connection !== "open") throw new Error("Wait for the response to finish, or reconnect and refresh the conversation first.");
    if (!this.modelChoices.some(row => choiceKey(row) === choiceKey(choice))) throw new Error("Reload the model list before choosing this model.");
    const value = modelSwitchValue(choice), generation = this.generation;
    this.patch({ modelChanging: true, modelNotice: "" });
    this.modelSwitchSubmitted = false;
    let submitted = false;
    try {
      await this.ensureSession(this.state.draft.trim().slice(0, 80) || "New conversation");
      const sessionId = this.runtimeId;
      if (!sessionId) throw new Error("The conversation is unavailable.");
      submitted = true; this.modelSwitchSubmitted = true;
      const result = await this.client.request<ModelSwitchResult>("config.set", { profile: PROFILE, session_id: sessionId, key: "model", value, confirm_expensive_model: confirmed });
      if (generation !== this.generation || this.stopped) throw new Error("Reconnect to verify the model. The change will not be replayed.");
      if (result.confirm_required) { this.modelSwitchSubmitted = false; return result; }
      if (result.key !== "model" || result.value !== choice.model || result.scope !== "session") throw new Error("The model change was not verified. Refresh the conversation before sending.");
      const verified = await this.readModelRuntime(sessionId, generation);
      if (generation !== this.generation || this.stopped || verified?.session_id !== sessionId || verified.info?.lazy || verified.info?.model !== choice.model || verified.info?.provider !== choice.provider) throw new Error("The session did not confirm the selected model and provider. Refresh the conversation before sending.");
      this.modelSwitchSubmitted = false;
      this.patch({ model: verified.info.model, provider: verified.info.provider, busy: this.state.busy || !!verified.running, modelUncertain: false, modelDeferred: !!result.deferred, modelNotice: result.deferred ? "Queued for the next reply; the current response is unchanged." : "Model changed for this conversation only." });
      return result;
    } catch (error) {
      if (generation === this.generation && !this.stopped) this.patch({ modelUncertain: submitted, modelNotice: submitted ? "Model change unconfirmed. Refresh the conversation before sending; it will not be retried automatically." : "The model was not changed." });
      throw error;
    } finally { if (generation === this.generation && !this.stopped) this.patch({ modelChanging: false }); }
  };
  send = async () => {
    const originalDraft = this.state.draft;
    const text = originalDraft.trim();
    if (!text || this.state.modelChanging || this.state.modelUncertain || this.state.sending || this.state.busy || this.state.loading || this.state.uncertain || this.state.connection !== "open") return;
    if (this.state.storedId && !this.runtimeId) {
      this.patch({ error: "Restore this conversation before sending. No new conversation has been created." });
      return;
    }
    this.patch({ sending: true, error: "" });
    const optimisticId = `user-${Date.now()}`;
    let submitted = false;
    try {
      await this.ensureSession(text.slice(0, 80));
      this.patch({ busy: true, activity: "Thinking…", items: [...this.state.items, { id: optimisticId, role: "user", text }] });
      submitted = true;
      markDelivery(this.state.storedId, true);
      await this.client.request("prompt.submit", { session_id: this.runtimeId, profile: PROFILE, text });
      markDelivery(this.state.storedId, false);
      if (this.state.draft === originalDraft) this.setDraft("");
      void this.refreshHistory();
    } catch (error) {
      const uncertain = submitted && !(error instanceof JsonRpcGatewayError);
      this.patch({ busy: uncertain, uncertain, activity: uncertain ? "Checking delivery…" : "", error: uncertain ? "Connection lost before delivery was confirmed. Your message will not be sent again automatically. Reconnect and check the conversation before sending again." : errorText(error) });
      if (!uncertain) markDelivery(this.state.storedId, false);
      if (!uncertain && submitted) this.patch({ items: this.state.items.filter(item => item.id !== optimisticId) });
    } finally { this.patch({ sending: false }); }
  };
  confirmReviewed = () => { markDelivery(this.state.storedId, false); this.patch({ uncertain: false, error: "" }); };
  interrupt = async () => {
    if (!this.runtimeId || this.state.connection !== "open") return;
    this.patch({ activity: "Stopping…" });
    try { await this.client.request("session.interrupt", { session_id: this.runtimeId, profile: PROFILE }); }
    catch (error) { this.patch({ error: `Stop was not confirmed: ${errorText(error)}` }); }
  };
  private async refreshPending() {
    const generation = this.generation, storedId = this.state.storedId;
    if (!storedId || !this.runtimeId || this.state.pending) return;
    try {
      const result = await this.client.request<SessionResult>("session.resume", { session_id: storedId, profile: PROFILE, source: "web", omit_messages: true });
      if (generation !== this.generation || this.stopped || this.state.pending) return;
      const pending = result.pending_approval ? { ...result.pending_approval, kind: "approval" } as RequestCard : null;
      if (pending) this.patch({ pending, busy: true, activity: "Needs your reply" });
    } catch (error) { if (generation === this.generation && !this.stopped) this.patch({ error: `Could not check remaining requests: ${errorText(error)}` }); }
  }
  respond = async (request: RequestCard, value: string, questionId?: string) => {
    if (this.state.sending || !this.runtimeId || this.state.pending?.request_id !== request.request_id) return;
    this.patch({ sending: true, error: "" });
    try {
      const live = request.server_request_id ? this.serverRequests.get(request.server_request_id) : undefined;

      if (request.kind === "approval") {
        if (live?.method === "approval") {
          live.respond({ choice: value });
          this.finishServerRequest(live.id);
        } else {
          const result = await this.client.request<{ resolved?: number }>("approval.respond", {
            session_id: this.runtimeId, profile: PROFILE, request_id: request.request_id, choice: value,
          });
          if (!result.resolved) throw new Error("This approval has expired or was already answered. Refresh the conversation.");
          if (this.state.pending?.request_id === request.request_id) this.patch({ pending: null, activity: "Working…" });
        }
        await this.refreshPending();
        return;
      }

      if (!live) throw new Error("This request has expired or was already answered. Refresh the conversation.");

      if (request.kind === "clarify") {
        if (live.method !== "clarify") throw new Error("The pending request changed. Refresh the conversation.");
        if (!questionId) {
          live.respond({});
          this.finishServerRequest(live.id);
          return;
        }
        const result = await this.client.request<{ status?: string; remaining?: string[] }>("clarify.lock", {
          session_id: this.runtimeId, profile: PROFILE, request_id: live.id,
          question_id: questionId, answer: value.trim() ? value : null,
        });
        if (result.status === "expired") {
          this.finishServerRequest(live.id);
          return;
        }
        const remaining = Array.isArray(result.remaining) ? result.remaining : [];
        if (!remaining.length) {
          this.finishServerRequest(live.id);
          return;
        }
        const answers = { ...(request.answers ?? {}), [questionId]: value.trim() ? value : null };
        const questions = request.questions?.filter(question => question.qid && remaining.includes(question.qid)) ?? [];
        const updated = { ...request, answers, questions };
        this.serverRequestCards.set(live.id, updated);
        this.patch({ pending: updated, activity: "Needs your reply" });
        return;
      }

      if ((request.kind === "sudo" && live.method !== "sudo") || (request.kind === "secret" && live.method !== "secret")) {
        throw new Error("The pending request changed. Refresh the conversation.");
      }
      live.respond({ value });
      this.finishServerRequest(live.id);
    } catch (error) {
      this.patch({ error: "Reply was not confirmed: " + errorText(error) });
    } finally {
      this.patch({ sending: false });
    }
  };
}
