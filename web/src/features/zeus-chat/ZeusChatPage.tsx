import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { ArrowDown, ArrowLeft, ArrowUp, Check, Copy, History, MessageCircle, Plus, Square, X } from "lucide-react";
import { Markdown } from "@/components/Markdown";
import { copyTextToClipboard } from "@/lib/clipboard";
import { ZeusChatController } from "./controller";
import { RequestPanel } from "./RequestPanel";
import type { ChatItem } from "./model";
import "./chat.css";

function Transcript({ items }: { items: ChatItem[] }) {
  const [copied, setCopied] = useState("");
  const [copyError, setCopyError] = useState("");
  const groups: Array<ChatItem | ChatItem[]> = [];
  for (const item of items) {
    const last = groups.at(-1);
    if (item.role === "tool") { if (Array.isArray(last)) last.push(item); else groups.push([item]); }
    else groups.push(item);
  }
  const copy = async (item: ChatItem) => {
    if (await copyTextToClipboard(item.text)) { setCopied(item.id); setCopyError(""); }
    else setCopyError("Copy is unavailable. Touch and hold the message to select its text.");
  };
  return <>{groups.map(group => Array.isArray(group) ? (
    <details className="zc-tools" key={group[0].id}><summary>Execution details <span>{group.length}</span></summary><ul>{group.map(item => <li key={item.id}>{item.text}</li>)}</ul></details>
  ) : (
    <article key={group.id} className={`zc-message zc-${group.role}`} aria-label={group.role === "user" ? "You" : "Zeus"}>
      {group.role === "user" ? <p>{group.text}</p> : <><Markdown content={group.text} streaming={group.streaming} />{!group.streaming && group.text && <button type="button" className="zc-copy" aria-label={copied === group.id ? "Message copied" : "Copy message"} onClick={() => void copy(group)}>{copied === group.id ? <Check size={16} /> : <Copy size={16} />}</button>}</>}
    </article>
  ))}{copyError && <p role="status" className="zc-muted">{copyError}</p>}</>;
}

export default function ZeusChatPage() {
  const [controller] = useState(() => new ZeusChatController());
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  const scroller = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const history = useRef<HTMLDialogElement>(null);
  const historySearch = useRef<HTMLInputElement>(null);
  const following = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
  const [search, setSearch] = useState("");
  useEffect(() => {
    controller.start();
    if (window.parent !== window) window.parent.postMessage({ type: "zeus-chat:ready", load: new URLSearchParams(window.location.search).get("zeusLoad") }, window.location.origin);
    return () => controller.stop();
  }, [controller]);
  useLayoutEffect(() => {
    const input = composer.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 144)}px`;
  }, [state.draft, state.pending]);
  useLayoutEffect(() => {
    if (following.current && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [state.items, state.pending, state.loading, state.activity, state.error]);
  useEffect(() => {
    const node = scroller.current;
    if (!node) return;
    const observer = new ResizeObserver(() => { if (following.current) node.scrollTop = node.scrollHeight; });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  const bottom = () => { following.current = true; setShowLatest(false); if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight; };
  const send = () => { bottom(); void controller.send(); };
  const newChat = () => { controller.newChat(); history.current?.close(); bottom(); };
  const openHistory = () => { setSearch(""); history.current?.showModal(); historySearch.current?.focus(); void controller.refreshHistory(); };
  const close = () => {
    if (window.parent !== window) window.parent.postMessage({ type: "zeus-chat:close" }, window.location.origin);
    else window.location.assign("/#view=command");
  };
  const online = state.connection === "open";
  const canSend = online && !state.sending && !state.busy && !state.loading && !state.uncertain && Boolean(state.draft.trim());
  const rows = state.sessions.filter(row => `${row.title || ""} ${row.preview || ""}`.toLowerCase().includes(search.toLowerCase()));
  return (
    <section className="zeus-chat" aria-label="Zeus AI chat" data-zeus-chat="native">
      <header className="zc-header">
        <button type="button" className="zc-icon" aria-label="Back to Zeus OS" title="Back to Zeus OS" onClick={close}><ArrowLeft size={21} /></button>
        <div className="zc-identity"><strong>Zeus</strong><span title={state.title}>{state.title}</span></div>
        <button type="button" className="zc-icon" aria-label="Conversation history" title="Conversation history" onClick={openHistory}><History size={21} /></button>
        <button type="button" className="zc-icon" aria-label="New conversation" title="New conversation" disabled={state.sending} onClick={newChat}><Plus size={22} /></button>
      </header>
      {!online && <div className="zc-connection" role="status"><span>{state.connection === "connecting" || state.connection === "idle" ? "Connecting to Zeus…" : "Connection lost. Reconnecting… Your draft is safe."}</span><button type="button" onClick={() => void controller.reconnect()}>Reconnect</button></div>}
      <div className="zc-scroll" ref={scroller} role="log" aria-label="Conversation" aria-live="off" onScroll={() => {
        const node = scroller.current;
        if (!node) return;
        following.current = node.scrollHeight - node.scrollTop - node.clientHeight < 90;
        setShowLatest(!following.current);
      }}>
        <div className="zc-transcript">
          {state.loading && <p className="zc-muted" role="status">Restoring your conversation…</p>}
          {!state.loading && !state.items.length && <div className="zc-welcome"><div className="zc-emblem"><MessageCircle size={28} /></div><h1>What shall we work on?</h1><p>Ask a question, investigate an issue, or get something done.</p><div className="zc-suggestions">{["What needs my attention?", "What changed today?", "Help me improve a project"].map(text => <button type="button" key={text} onClick={() => { controller.setDraft(text); composer.current?.focus(); }}>{text}<ArrowUp size={16} /></button>)}</div></div>}
          <Transcript items={state.items} />
          {state.busy && !state.pending && <div className="zc-activity" role="status"><span className="zc-working" aria-hidden="true" />{state.activity || "Working…"}</div>}
          {state.error && <div className="zc-error" role="alert"><p>{state.error}</p>{state.storedId && <button type="button" disabled={state.sending} onClick={() => void controller.open(state.storedId!, true)}>Refresh conversation</button>}{state.uncertain && online && <button type="button" disabled={state.loading || state.sending} onClick={controller.confirmReviewed}>I have checked the conversation</button>}</div>}
        </div>
      </div>
      <div className="zc-bottom">
        {showLatest && <button type="button" className="zc-latest" onClick={bottom}><ArrowDown size={16} />Jump to latest</button>}
        {state.pending && <RequestPanel key={`${state.pending.request_id}:${state.pending.questions?.[0]?.qid || ""}`} request={state.pending} disabled={!online || state.sending} respond={controller.respond} />}
        {!state.pending && <form className="zc-composer" aria-label="Message Zeus" onSubmit={event => { event.preventDefault(); send(); }}>
          <label className="zc-sr" htmlFor="zeus-message">Message Zeus</label>
          <textarea id="zeus-message" ref={composer} rows={1} placeholder="Message Zeus…" value={state.draft} maxLength={100000} spellCheck autoCapitalize="sentences" autoCorrect="on" enterKeyHint="enter" onChange={event => controller.setDraft(event.target.value)} onKeyDown={event => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229 && (event.ctrlKey || event.metaKey || window.matchMedia("(pointer: fine)").matches)) { event.preventDefault(); if (canSend) send(); }
          }} />
          {state.busy ? <button type="button" className="zc-send zc-stop" aria-label="Stop response" title="Stop response" disabled={!online || state.sending} onClick={() => void controller.interrupt()}><Square size={17} fill="currentColor" /></button> : <button type="submit" className="zc-send" aria-label="Send message" title="Send message" disabled={!canSend}><ArrowUp size={22} /></button>}
        </form>}
        <p className="zc-caption">{state.sending ? "Sending…" : state.pending ? "Zeus is waiting for your response." : "Zeus can make mistakes. Your existing approval rules still apply."}</p>
      </div>
      <dialog className="zc-history" ref={history} aria-labelledby="zeus-history-title">
        <header><h2 id="zeus-history-title">Conversations</h2><button type="button" className="zc-icon" aria-label="Close conversation history" onClick={() => history.current?.close()}><X size={21} /></button></header>
        <button type="button" className="zc-new" disabled={state.sending} onClick={newChat}><Plus size={19} />New conversation</button>
        <label className="zc-sr" htmlFor="zeus-history-search">Search conversations</label><input id="zeus-history-search" ref={historySearch} type="search" placeholder="Search conversations" value={search} onChange={event => setSearch(event.target.value)} />
        {state.historyError && <div className="zc-error" role="alert">History could not be loaded. <button type="button" onClick={() => void controller.refreshHistory()}>Retry</button></div>}
        <div className="zc-history-list">{rows.map(row => <button type="button" key={row.id} className={row.id === state.storedId ? "selected" : ""} disabled={!online || state.sending} onClick={() => { history.current?.close(); bottom(); void controller.open(row.id); }}><strong>{row.title || row.preview || "Conversation"}</strong><span>{new Date((row.last_active || row.started_at) * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" })}{row.is_active ? " · Active" : ""}</span></button>)}{!rows.length && !state.historyError && <p className="zc-muted">{search ? "No matching conversations in the loaded history." : "Your conversations will appear here."}</p>}</div>
        {state.sessions.length < state.total && <button type="button" className="zc-more" onClick={() => void controller.refreshHistory(true)}>Load older conversations</button>}
      </dialog>
    </section>
  );
}
