import { useEffect, useRef, useState } from "react";
import { X, ArrowUpRight, Zap } from "lucide-react";
import { parseSnapshotReply, snapshotExpiry, type SnapshotAnswer } from "./snapshot";

interface QuickAnswersProps {
  prepare: (question: string) => void;
}
/** Read-only company answers, visibly separate from server-owned conversation history. */
export function QuickAnswers({ prepare }: QuickAnswersProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const request = useRef("");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<SnapshotAnswer | null>(null);
  const [error, setError] = useState("");
  const [waiting, setWaiting] = useState(false);
  const load = new URLSearchParams(window.location.search).get("zeusLoad") || "";
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (event.origin !== location.origin || event.source !== window.parent || !request.current) return;
      const result = parseSnapshotReply(event.data, request.current, load);
      if (!result) return;
      clearTimeout(timer.current); request.current = ""; setWaiting(false); setAnswer(result);
    };
    window.addEventListener("message", receive);
    return () => { window.removeEventListener("message", receive); clearTimeout(timer.current); request.current = ""; };
  }, [load]);
  useEffect(() => {
    const deadline = answer ? snapshotExpiry(answer) : null;
    if (!answer || deadline === null) return;
    const expiry = setTimeout(() => setAnswer(current => current && current === answer ? { ...current, state: "stale", text: "This snapshot has expired. Ask again for current evidence.", facts: [] } : current), Math.max(0, deadline - Date.now()));
    return () => clearTimeout(expiry);
  }, [answer]);
  const ask = (text: string) => {
    if (!text.trim()) return;
    clearTimeout(timer.current); setQuestion(text); setAnswer(null); setError(""); setWaiting(true);
    const requestId = crypto.randomUUID(); request.current = requestId;
    window.parent.postMessage({ type: "zeus-chat:snapshot", load, requestId, question: text.slice(0, 1000) }, location.origin);
    timer.current = setTimeout(() => { if (request.current !== requestId) return; request.current = ""; setWaiting(false); setError("The company snapshot could not be read. Your conversation is unaffected."); }, 2500);
  };
  const close = () => { clearTimeout(timer.current); request.current = ""; setWaiting(false); dialog.current?.close(); };
  if (window.parent === window || !load) return null;
  return <>
    <button type="button" className="zc-icon" aria-label="Instant company answers" title="Instant company answers" onClick={() => { dialog.current?.showModal(); input.current?.focus(); }}><Zap size={21} /></button>
    <dialog className="zc-quick-dialog" ref={dialog} aria-labelledby="zc-quick-title" onCancel={close}>
      <header><div><span className="zc-muted">READ-ONLY · COMPANY SNAPSHOT</span><h2 id="zc-quick-title">Answers without the wait.</h2></div><button type="button" className="zc-icon" aria-label="Close instant answers" onClick={close}><X size={21} /></button></header>
      <form onSubmit={event => { event.preventDefault(); ask(question); }}><label htmlFor="zc-quick-question" className="zc-sr">Executive question</label><input id="zc-quick-question" ref={input} value={question} maxLength={1000} placeholder="What needs my attention?" onChange={event => setQuestion(event.target.value)} /><button type="submit" disabled={!question.trim()}>Ask</button></form>
      <div className="zc-quick-suggestions">{["Give me the company briefing.", "What needs my attention?", "What is our software MRR?", "What made money yesterday?"].map(text => <button type="button" key={text} onClick={() => ask(text)}>{text}</button>)}</div>
      <div role="status" aria-live="polite">{waiting && <p>Reading the current snapshot…</p>}{error && <p className="zc-error">{error}</p>}</div>
      {answer && <section className="zc-quick-answer" aria-label="Snapshot answer"><span className="zc-muted">{answer.state.toUpperCase()} · {answer.mode === "snapshot" ? "No model call" : "Further analysis needed"}</span><h3>{answer.text}</h3>{answer.facts.map((text, i) => <p key={i}>{text}</p>)}{answer.limitations.map((text, i) => <p className="zc-muted" key={i}>{text}</p>)}<details><summary>Source &amp; evidence</summary><p>{answer.source}</p><p>{answer.checkedAt ? new Date(answer.checkedAt).toLocaleString("en-GB", { timeZone: "Europe/Madrid" }) + " · Barcelona" : "Timestamp unavailable"}</p><p>{answer.path || "Source resolution required"}</p></details><button type="button" className="zc-quick-continue" onClick={() => { prepare(answer.question); close(); }}>Continue in AI <ArrowUpRight size={16} /></button></section>}
      <p className="zc-muted">This is a live snapshot preview, not a saved conversation. Nothing is executed. Continuing prepares a draft for your review.</p>
    </dialog>
  </>;
}
