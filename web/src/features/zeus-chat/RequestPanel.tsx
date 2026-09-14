import { useState } from "react";
import type { RequestCard } from "./model";

interface Props {
  request: RequestCard;
  disabled: boolean;
  respond: (request: RequestCard, value: string, questionId?: string) => Promise<void>;
}
export function RequestPanel({ request, disabled, respond }: Props) {
  const [answer, setAnswer] = useState("");
  const question = request.questions?.[0];
  const secure = request.kind === "sudo" || request.kind === "secret";
  const submit = (value: string) => { setAnswer(""); void respond(request, value, question?.qid); };
  if (request.kind === "approval") return (
    <section className="zc-request" aria-label="Approval required">
      <strong>Your approval is needed</strong>
      <p>{request.description || request.reason || "Zeus is waiting before taking this action."}</p>
      {request.command && <details><summary>Review the action</summary><pre>{request.command}</pre></details>}
      <div className="zc-request-actions">
        <button type="button" disabled={disabled} onClick={() => submit("deny")}>Do not allow</button>
        {request.choices?.includes("once") && <button type="button" className="zc-primary" disabled={disabled} onClick={() => submit("once")}>Allow once</button>}
      </div>
    </section>
  );
  return (
    <form className="zc-request" aria-label={secure ? "Secure reply" : "Zeus needs your reply"} onSubmit={event => { event.preventDefault(); if (answer.trim()) submit(answer); }}>
      <label htmlFor="zeus-request-answer">{question?.question || request.question || request.prompt || (secure ? "Zeus needs a secure value to continue" : "Zeus needs your reply")}</label>
      {secure ? <><p>This value is sent securely to the existing execution service, not added to the chat or saved in this browser.</p><input id="zeus-request-answer" type="password" autoComplete="off" value={answer} onChange={event => setAnswer(event.target.value)} disabled={disabled} /></> : <>
        {(question?.choices || request.choices)?.length ? <div className="zc-choices">{(question?.choices || request.choices || []).map(choice => <button type="button" key={choice} disabled={disabled} onClick={() => setAnswer(current => (question?.multi_select || request.multi_select) && current ? `${current}, ${choice}` : choice)}>{choice}</button>)}</div> : null}
        <textarea id="zeus-request-answer" rows={2} value={answer} onChange={event => setAnswer(event.target.value)} placeholder="Your reply…" disabled={disabled} />
      </>}
      <div className="zc-request-actions"><button type="button" disabled={disabled} onClick={() => submit("")}>Cancel request</button><button type="submit" className="zc-primary" disabled={disabled || !answer.trim()}>Send reply</button></div>
    </form>
  );
}
