import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Search, X } from "lucide-react";
import type { ChatSnapshot, ZeusChatController } from "./controller";
import { choiceKey, type ModelChoice, type ModelOptions } from "./model-controls";
import "./model-selector.css";

export function ModelSelector({ controller, state }: { controller: ZeusChatController; state: ChatSnapshot }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const request = useRef(0);
  const [options, setOptions] = useState<ModelOptions | null>(null);
  const [selected, setSelected] = useState<ModelChoice | null>(null);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const unavailable = state.connection !== "open" || state.loading || state.sending || state.busy || !!state.pending || state.uncertain || state.modelChanging || state.modelUncertain;
  useEffect(() => { if (state.connection === "open" && !state.storedId && !state.model) void controller.loadModels().catch(() => {}); }, [controller, state.connection, state.storedId, state.model]);
  useEffect(() => () => { request.current++; }, []);
  const load = async (refresh = false) => {
    const id = ++request.current; setLoading(true); setError(""); setConfirmation(""); setSelected(null);
    try { const data = await controller.loadModels(refresh); if (id === request.current) setOptions(data); }
    catch (cause) { if (id === request.current) { setOptions(null); setError(cause instanceof Error ? cause.message : "Models could not be loaded."); } }
    finally { if (id === request.current) setLoading(false); }
  };
  const close = () => { if (controller.getSnapshot().modelChanging) return; request.current++; dialog.current?.close(); trigger.current?.focus(); };
  const open = () => { setFilter(""); dialog.current?.showModal(); void load(); };
  const apply = async (confirmed = false) => {
    if (!selected || unavailable) return;
    setError("");
    try {
      const result = await controller.changeModel(selected, confirmed);
      if (result.confirm_required) { setConfirmation(result.confirm_message || result.warning || "Confirm this model's cost and compatibility before switching."); return; }
      close();
    } catch (cause) { setConfirmation(""); setError(cause instanceof Error ? cause.message : "The model change was not confirmed."); }
  };
  const rows = (options?.choices || []).filter(row => `${row.model} ${row.providerName}`.toLowerCase().includes(filter.toLowerCase()));
  return <div className="zc-model-bar">
    <button ref={trigger} type="button" className="zc-model-trigger" aria-label={`Change model${state.model ? `: ${state.model}` : ""}`} aria-haspopup="dialog" disabled={unavailable && !state.modelUncertain} onClick={open} title={state.modelChanging ? "Changing model…" : "Change model for this conversation"}>
      <span>{state.modelDeferred ? "Next model" : "Model"}</span><strong>{state.modelChanging ? "Changing…" : state.modelUncertain ? "Check model" : state.model || "Choose model"}</strong><ChevronDown size={15} />
    </button>
    {state.modelNotice && <span className="zc-model-notice" role="status">{state.modelNotice}{state.modelUncertain && state.storedId && <button type="button" disabled={state.loading || state.modelChanging} onClick={() => void controller.open(state.storedId!, true)}>Refresh conversation</button>}</span>}
    <dialog ref={dialog} className="zc-model-dialog" aria-labelledby="zc-model-title" onCancel={event => { event.preventDefault(); close(); }}>
      <header><div><h2 id="zc-model-title">Choose a model</h2><p>This conversation only. Your history and draft stay here.</p></div><button type="button" className="zc-icon" aria-label="Close model selector" disabled={state.modelChanging} onClick={close}><X size={21} /></button></header>
      <div className="zc-model-search"><Search size={18} aria-hidden="true" /><label className="zc-sr" htmlFor="zc-model-search">Search models</label><input id="zc-model-search" type="search" placeholder="Search models or providers" value={filter} onChange={event => { setFilter(event.target.value); setConfirmation(""); }} /></div>
      <div className="zc-model-list" aria-label="Available models" aria-busy={loading}>
        {loading ? <p role="status">Loading connected models…</p> : rows.length ? rows.map(row => <button type="button" key={choiceKey(row)} aria-pressed={!!selected && choiceKey(selected) === choiceKey(row)} disabled={state.modelChanging} onClick={() => { setSelected(row); setConfirmation(""); setError(""); }}><span><strong>{row.model}</strong><small>{row.providerName}{row.model === state.model && row.provider === state.provider ? " · Current" : ""}</small></span>{selected && choiceKey(selected) === choiceKey(row) && <Check size={19} />}</button>) : <p>{filter ? "No models match your search." : "No connected models are available. Retry to refresh the catalogue."}</p>}
      </div>
      <div className="zc-model-feedback">
        {selected?.warning && !confirmation && <p>{selected.warning}</p>}
        {confirmation && <div role="alert"><strong>Confirm model change</strong><p>{confirmation}</p></div>}
        {error && <p role="alert">{error}</p>}
        {state.busy && <p>Finish or stop the current response before switching models.</p>}
        {state.modelUncertain && <p>Close this selector and refresh the conversation to verify its model before sending.</p>}
      </div>
      <footer><button type="button" disabled={loading || state.modelChanging} onClick={() => void load(true)}>Refresh models</button><button type="button" disabled={state.modelChanging} onClick={close}>Cancel</button><button type="button" className="zc-model-apply" disabled={!selected || loading || unavailable} onClick={() => void apply(!!confirmation)}>{state.modelChanging ? "Changing…" : confirmation ? "Confirm switch" : "Use model"}</button></footer>
    </dialog>
  </div>;
}
