import type { GatewayEvent } from "@hermes/shared";

export interface ChatItem {
  id: string;
  role: "user" | "assistant" | "tool";
  text: string;
  streaming?: boolean;
}
export interface Question { qid?: string; question: string; choices?: string[]; multi_select?: boolean }
export interface RequestCard {
  kind: "approval" | "clarify" | "sudo" | "secret";
  request_id: string;
  question?: string;
  multi_select?: boolean;
  questions?: Question[];
  choices?: string[];
  command?: string;
  description?: string;
  reason?: string;
  prompt?: string;
}
export interface Conversation {
  items: ChatItem[];
  busy: boolean;
  activity: string;
  pending: RequestCard | null;
  error: string;
}
export const emptyConversation = (): Conversation => ({ items: [], busy: false, activity: "", pending: null, error: "" });
const text = (value: unknown): string => typeof value === "string" ? value : "";

/** Display data only; system prompts, reasoning and hidden scaffolding never become chat bubbles. */
export function historyItems(messages: unknown[]): ChatItem[] {
  return messages.flatMap((raw, index): ChatItem[] => {
    if (!raw || typeof raw !== "object") return [];
    const m = raw as Record<string, unknown>;
    if (m.display_kind === "hidden" || m.role === "system") return [];
    if (m.role === "tool") return [{ id: `history-${index}`, role: "tool", text: text(m.name) || text(m.tool_name) || "Tool activity" }];
    if (m.role !== "user" && m.role !== "assistant") return [];
    const body = text(m.text) || text(m.content);
    if (!body.trim()) return [];
    return [{ id: `history-${index}`, role: m.role, text: body }];
  });
}
function assistant(items: ChatItem[], body: string, append: boolean, streaming: boolean): ChatItem[] {
  const last = items.at(-1);
  if (last?.role === "assistant" && last.streaming) {
    return [...items.slice(0, -1), { ...last, text: append ? last.text + body : body || last.text, streaming }];
  }
  if (!body && !streaming) return items;
  if (!append && !streaming && last?.role === "assistant" && last.text === body) return items;
  return [...items, { id: `reply-${items.length}`, role: "assistant", text: body, streaming }];
}
/** Events for another conversation are never allowed to alter the foreground transcript. */
export function reduceEvent(state: Conversation, event: GatewayEvent, sessionId: string | null): Conversation {
  if (!sessionId || event.session_id !== sessionId) return state;
  const p = (event.payload && typeof event.payload === "object" ? event.payload : {}) as Record<string, unknown>;
  const body = text(p.text);
  if (event.type === "message.start") return { ...state, busy: true, activity: "Thinking…", error: "" };
  if (event.type === "message.delta") return { ...state, busy: true, activity: "Writing…", items: assistant(state.items, body, true, true) };
  if (event.type === "message.interim") return { ...state, items: assistant(state.items, body, false, false) };
  if (event.type === "message.complete") return {
    ...state, items: assistant(state.items, body, false, false), busy: false, activity: "", pending: null,
    error: p.status === "error" ? text(p.error) || "Zeus could not finish this response." : "",
  };
  if (event.type === "thinking.delta" || event.type === "reasoning.delta") return { ...state, busy: true, activity: "Thinking…" };
  if (event.type === "status.update") return { ...state, activity: body || "Working…" };
  if (event.type === "tool.start" || event.type === "tool.complete") {
    const id = `tool-${text(p.tool_id) || state.items.length}`;
    const name = (text(p.name) || "Tool").replaceAll("_", " ");
    const item: ChatItem = { id, role: "tool", text: `${name}${event.type === "tool.start" ? " · working" : " · finished"}` };
    const index = state.items.findIndex(row => row.id === id);
    const items = state.items.map(row => row.streaming ? { ...row, streaming: false } : row);
    if (index >= 0) items[index] = item; else items.push(item);
    return { ...state, items, activity: event.type === "tool.start" ? `Using ${name}…` : "Working…" };
  }
  const requestKind = { "approval.request": "approval", "clarify.request": "clarify", "sudo.request": "sudo", "secret.request": "secret" } as const;
  if (event.type in requestKind && text(p.request_id)) {
    const kind = requestKind[event.type as keyof typeof requestKind];
    return { ...state, busy: true, activity: "Needs your reply", pending: { ...p, kind, request_id: text(p.request_id) } as RequestCard };
  }
  if (event.type.endsWith(".expire") && p.request_id === state.pending?.request_id) return { ...state, pending: null, activity: "Working…" };
  if (event.type === "error") return { ...state, busy: false, activity: "", error: text(p.message) || body || "The AI connection reported an error." };
  return state;
}
