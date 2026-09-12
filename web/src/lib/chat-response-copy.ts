/** Copy is a scoped READ, never input submitted to the agent. */
export interface ChatCopyBinding { id: string; profile: string; channel: string }
export function lastAssistantReply(raw: unknown, binding: ChatCopyBinding): string {
  if (!raw || typeof raw !== "object") throw new Error("Reply data is unavailable.");
  const data = raw as { session_id?: unknown; profile?: unknown; messages?: unknown };
  if (data.session_id !== binding.id || (binding.profile && data.profile !== binding.profile))
    throw new Error("The conversation changed. Please try copying again.");
  if (!Array.isArray(data.messages)) throw new Error("Reply data is unavailable.");
  // The existing paged messages API returns its latest window chronologically.
  for (const row of [...data.messages].reverse()) {
    if (row && typeof row === "object" && row.role === "assistant" &&
        typeof row.content === "string" && row.content.trim() && !row.tool_calls?.length)
      return row.content;
  }
  throw new Error("There is no completed assistant reply to copy yet.");
}
export async function copyBoundReply(binding: ChatCopyBinding,
  read: (id: string, profile: string) => Promise<unknown>,
  write: (text: string) => Promise<boolean>, current: () => boolean): Promise<boolean> {
  const response = await read(binding.id, binding.profile);
  if (!current()) return false;
  const text = lastAssistantReply(response, binding);
  if (!current()) return false;
  return write(text);
}

/** IPC session IDs and stored transcript IDs are deliberately different. */
export function storedCopySessionId(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const info=payload as {stored_session_id?:unknown};
  // The event channel already isolates this PTY. Profile authorization belongs
  // to the subsequent /sessions read, which is explicitly scoped by profile;
  // rejecting here on display metadata can strand a valid conversation.
  return typeof info.stored_session_id === "string" && info.stored_session_id.length > 0 && info.stored_session_id.length <= 200 ? info.stored_session_id : null;
}
