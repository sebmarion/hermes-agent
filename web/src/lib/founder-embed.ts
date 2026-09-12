/** Presentation mode only; never an authorization decision. */
export function isZeusFounderChat(pathname: string, search: string): boolean {
  const q = new URLSearchParams(search);
  return pathname.replace(/\/$/, "") === "/chat" && q.get("embed") === "zeus" && q.get("profile") === "zeus-os";
}
