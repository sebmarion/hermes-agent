export interface SnapshotAnswer {
  question: string;
  mode: "snapshot" | "analysis";
  text: string;
  state: "current" | "partial" | "unknown" | "unavailable" | "stale";
  facts: string[];
  limitations: string[];
  source: string;
  path: string;
  checkedAt: string | null;
  expiresAt?: string | null;
}
export function parseSnapshotReply(value: unknown, requestId: string, load: string): SnapshotAnswer | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const envelope = value as Record<string, unknown>;
  if (envelope.type !== "zeus-chat:snapshot-result" || envelope.requestId !== requestId || envelope.load !== load || !load) return null;
  const raw = envelope.answer;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const a = raw as Record<string, unknown>;
  for (const key of ["question", "text", "source", "path"]) if (typeof a[key] !== "string" || (a[key] as string).length > 4000) return null;
  if (typeof a.mode !== "string" || typeof a.state !== "string" || !["snapshot", "analysis"].includes(a.mode) || !["current", "partial", "unknown", "unavailable", "stale"].includes(a.state)) return null;
  for (const key of ["facts", "limitations"]) if (!Array.isArray(a[key]) || a[key].length > 32 || a[key].some((v: unknown) => typeof v !== "string" || v.length > 4000)) return null;
  if (a.checkedAt !== null && (typeof a.checkedAt !== "string" || !/(Z|[+-]\d{2}:\d{2})$/.test(a.checkedAt) || !Number.isFinite(Date.parse(a.checkedAt)))) return null;
  if (a.expiresAt !== undefined && a.expiresAt !== null) {
    if (typeof a.expiresAt !== "string" || !/(Z|[+-]\d{2}:\d{2})$/.test(a.expiresAt) || !Number.isFinite(Date.parse(a.expiresAt)) || typeof a.checkedAt !== "string") return null;
    const interval = Date.parse(a.expiresAt) - Date.parse(a.checkedAt);
    if (interval <= 0 || interval > 900000) return null;
  }
  // Whitelist display-only fields. Never accept executable routes, HTML or actions from a reply.
  return { question: a.question as string, text: a.text as string, source: a.source as string, path: a.path as string,
    mode: a.mode as SnapshotAnswer["mode"], state: a.state as SnapshotAnswer["state"], facts: a.facts as string[],
    limitations: a.limitations as string[], checkedAt: a.checkedAt as string | null,
    ...(a.expiresAt === undefined ? {} : { expiresAt: a.expiresAt as string | null }) };
}

/** Preserve the producer deadline: 60-second executive answers and bounded billing clocks differ. */
export function snapshotExpiry(answer: SnapshotAnswer): number | null {
  if (!answer.checkedAt || ["stale", "unavailable"].includes(answer.state)) return null;
  return answer.expiresAt ? Date.parse(answer.expiresAt) : Date.parse(answer.checkedAt) + 180000;
}
