/** Same-origin, correlated draft handoff. It never sends a prompt or replaces user input. */
interface ComposeState {
  draft: string;
  busy: boolean;
  sending: boolean;
  loading: boolean;
  uncertain: boolean;
  pending: unknown;
}
interface ComposeOptions {
  parent: MessageEventSource;
  origin: string;
  load: string;
  read: () => ComposeState;
  setDraft: (text: string) => void;
  occupied?: (text: string) => void;
  acknowledge: (result: { type: "zeus-chat:compose-result"; load: string; requestId: string; result: "prepared" | "occupied" }) => void;
}
export function createComposeHandler(options: ComposeOptions) {
  const receipts = new Map<string, "prepared" | "occupied">();
  return (event: Pick<MessageEvent, "origin" | "source" | "data">) => {
    const data = event.data;
    if (event.origin !== options.origin || event.source !== options.parent || !options.load ||
        !data || typeof data !== "object" || Array.isArray(data) || data.type !== "zeus-chat:compose" ||
        data.load !== options.load || typeof data.requestId !== "string" || !/^[a-zA-Z0-9-]{16,80}$/.test(data.requestId) ||
        typeof data.text !== "string" || !data.text.trim() || data.text.length > 4000) return;
    let result = receipts.get(data.requestId);
    if (!result) {
      if (receipts.size >= 64) return;
      const state = options.read();
      result = state.draft.length || state.busy || state.sending || state.loading || state.uncertain || state.pending ? "occupied" : "prepared";
      if (result === "prepared") options.setDraft(data.text);
      else options.occupied?.(data.text);
      // Finite per-frame request budget. Old request IDs must never become replayable after eviction.
      receipts.set(data.requestId, result);
    }
    options.acknowledge({ type: "zeus-chat:compose-result", load: options.load, requestId: data.requestId, result });
  };
}
