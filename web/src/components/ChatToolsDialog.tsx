import { useEffect, useRef, type ReactNode } from "react";

/** Native modality keeps closed tools out of focus/accessibility navigation.
 * The browser handles nested dialogs, focus containment, and focus return. */
export function ChatToolsDialog({ open, onClose, label, children }: {
  open: boolean; onClose: () => void; label: string; children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog || !open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.showModal();
    return () => { dialog.close(); document.body.style.overflow = previousOverflow; };
  }, [open]);
  return <dialog ref={ref} id="chat-side-panel" aria-label={label}
    onCancel={onClose}
    onClick={e => { if (e.target === e.currentTarget) {
      const r = e.currentTarget.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) onClose();
    } }}
    className="fixed inset-y-0 left-auto right-0 m-0 ml-auto h-dvh max-h-dvh w-80 max-w-full min-w-0 flex-col overflow-hidden border-0 border-l border-current/20 bg-background-base text-midground p-0 open:flex backdrop:bg-black/60 [&_button]:min-h-[44px]">
    {children}
  </dialog>;
}
