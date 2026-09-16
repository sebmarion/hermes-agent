import { JsonRpcGatewayClient, JsonRpcGatewayError, type ConnectionState } from "@hermes/shared";
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
  pending_clarify?: Record<string, unknown>;
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
  private stopped = false;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;
  private reconnectFlight: Promise<void> | null = null;
  private retries = 0;
  private disposers: Array<() => void> = [];
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  private patch(patch: Partial<ChatSnapshot>) { this.state = { ...this.state, ...patch }; this.listeners.forEach(listener => listener()); }

  start() {
    this.stopped = false;
    this.lifecycle++;
    this.client = new JsonRpcGatewayClient({ requestIdPrefix: "zeus-chat-", requestTimeoutMs: 30000 });
    this.disposers.push(this.client.onState(connection => {
      this.patch({ connection });
      if ((connection === "closed" || connection === "error") && !this.stopped) this.scheduleReconnect();
    }));
    this.disposers.push(this.client.onEvent(event => {
      // A resumed desktop conversation may request UI panes that this chat deliberately does not expose.
      // Fail those read/GUI bridges explicitly; never auto-approve or pretend to perform an action.
      const unsupported: Record<string, [string, string]> = {
        "terminal.read.request": ["terminal.read.respond", "text"],
        "preview.read.request": ["preview.read.respond", "text"],
        "preview.act.request": ["preview.act.respond", "text"],
        "window.read.request": ["window.read.respond", "text"],
        "tour.request": ["tour.respond", "text"],
        "mcp.setup.request": ["mcp.setup.respond", "result"],
      };
      const bridge = unsupported[event.type];
      const payload = event.payload as { request_id?: string } | undefined;
      if (bridge && this.runtimeId && event.session_id === this.runtimeId && payload?.request_id) {
        const message = "Desktop pane controls are not available in Zeus web chat. Use standard server tools or ask the user in chat.";
        void this.client.request(bridge[0], { session_id: this.runtimeId, profile: PROFILE, request_id: payload.request_id, [bridge[1]]: JSON.stringify({ ok: false, error: message }) }).catch(error => this.patch({ error: errorText(error) }));
        return;
      }
      if (event.type === "session.info" && event.session_id === this.runtimeId && !this.state.modelChanging) {
        const info = event.payload as SessionResult["info"];
        if (info?.model && !info.lazy) this.patch({ model: info.model, provider: info.provider || this.state.provider, modelUncertain: false });
      }
      if (event.type === "message.start" && event.session_id === this.runtimeId && this.state.modelDeferred) this.patch({ modelDeferred: false, modelNotice: "Using the selected model for this reply." });
      const next = reduceEvent(this.state, event, this.runtimeId);
      if (next !== this.state) this.patch(next);
      if (event.type === "approval.request" && event.session_id === this.runtimeId) {
        const request = this.state.pending;
        if (request) void this.client.request("approval.received", { session_id: this.runtimeId, profile: PROFILE, request_id: request.request_id }).catch(() => {});
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
    this.patch({ modelChanging: false, modelUncertain: this.state.modelUncertain || this.modelSwitchSubmitted });
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
    this.generation++; this.runtimeId = null; this.remember(null);
    this.patch({ ...emptyConversation(), storedId: null, draft: readDraft(null), title: "New conversation", model: "", provider: "", modelUncertain: false, modelNotice: "", modelDeferred: false, loading: false, uncertain: false });
  };
  open = async (id: string, reconnect = false) => {
    if ((this.state.sending || this.state.modelChanging) && !reconnect) return;
    const generation = ++this.generation;
    const same = this.state.storedId === id;
    if (reconnect && this.state.modelChanging) this.patch({ modelChanging: false, modelUncertain: true });
    this.runtimeId = null;
    const row = this.state.sessions.find(session => session.id === id);
    this.patch({ ...(same ? {} : { ...emptyConversation(), model: "", provider: "", modelUncertain: false, modelNotice: "", modelDeferred: false }), storedId: id, title: row?.title || row?.preview || "Conversation", loading: true, draft: same ? this.state.draft : readDraft(id), uncertain: unconfirmed(id), error: "" });
    this.remember(id);
    try {
      const result = await this.client.request<SessionResult>("session.resume", { session_id: id, profile: PROFILE, source: "web", cols: 96 });
      if (generation !== this.generation || this.stopped) return;
      this.runtimeId = result.session_id;
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
      const pending = result.pending_approval ? { ...result.pending_approval, kind: "approval" } as RequestCard : result.pending_clarify ? { ...result.pending_clarify, kind: "clarify" } as RequestCard : null;
      this.patch({ storedId, items, loading: false, busy: Boolean(result.running), activity: pending ? "Needs your reply" : result.running ? "Working…" : "", pending, error: failed ? result.inflight?.error || "Zeus could not finish the previous response." : unconfirmed(storedId) ? "A previous send was not confirmed. Check this conversation before sending the saved draft again." : "", model: result.info?.model || this.state.model, provider: result.info?.provider || this.state.provider, ...(!result.info?.lazy && result.info?.model ? { modelUncertain: false } : {}) });
    } catch (error) { if (generation === this.generation && !this.stopped) this.patch({ loading: false, error: `Conversation could not be restored: ${errorText(error)}` }); }
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
      // A newly created gateway returns lazy profile defaults until its scheduled agent
      // build completes. Poll only read-back; never replay config.set or send a probe prompt.
      let verified: SessionResult | undefined;
      for (let attempt = 0; attempt < 20; attempt++) {
        if (generation !== this.generation || this.stopped) throw new Error("The conversation changed during model verification.");
        verified = await this.client.request<SessionResult>("session.activate", { profile: PROFILE, session_id: sessionId, omit_messages: true });
        if (!verified.info?.lazy) break;
        await new Promise(resolve => setTimeout(resolve, 250));
      }
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
    if (!storedId || !this.runtimeId) return;
    try {
      const result = await this.client.request<SessionResult>("session.resume", { session_id: storedId, profile: PROFILE, source: "web", omit_messages: true });
      if (generation !== this.generation || this.stopped || this.state.pending) return;
      const pending = result.pending_approval ? { ...result.pending_approval, kind: "approval" } as RequestCard : result.pending_clarify ? { ...result.pending_clarify, kind: "clarify" } as RequestCard : null;
      if (pending) this.patch({ pending, busy: true, activity: "Needs your reply" });
    } catch (error) { if (generation === this.generation && !this.stopped) this.patch({ error: `Could not check remaining requests: ${errorText(error)}` }); }
  }
  respond = async (request: RequestCard, value: string, questionId?: string) => {
    if (this.state.sending || !this.runtimeId || this.state.pending?.request_id !== request.request_id) return;
    this.patch({ sending: true, error: "" });
    try {
      const key = { approval: "choice", clarify: "answer", sudo: "password", secret: "value" }[request.kind];
      const result = await this.client.request<{ resolved?: number }>(`${request.kind}.respond`, {
        session_id: this.runtimeId, profile: PROFILE, request_id: request.request_id, [key]: value,
        ...(questionId ? { question_id: questionId } : {}),
      });
      if (request.kind === "approval" && !result.resolved) throw new Error("This approval has expired or was already answered. Refresh the conversation.");
      const remaining = questionId ? request.questions?.filter(q => q.qid !== questionId) : [];
      this.patch({ pending: remaining?.length ? { ...request, questions: remaining } : null, activity: remaining?.length ? "Needs your reply" : "Working…" });
      if (!remaining?.length) await this.refreshPending();
    } catch (error) { this.patch({ error: `Reply was not confirmed: ${errorText(error)}` }); }
    finally { this.patch({ sending: false }); }
  };
}
