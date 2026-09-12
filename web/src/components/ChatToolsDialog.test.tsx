// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import { ChatToolsDialog } from './ChatToolsDialog';
it('uses native modality only while open and releases body state on teardown', async () => {
 Object.assign(globalThis,{IS_REACT_ACT_ENVIRONMENT:true});
 const proto=HTMLDialogElement.prototype;const savedShow=Object.getOwnPropertyDescriptor(proto,'showModal'),savedClose=Object.getOwnPropertyDescriptor(proto,'close');
 const show=vi.fn(function(this:HTMLDialogElement){this.open=true;});
 const close=vi.fn(function(this:HTMLDialogElement){this.open=false;});
 Object.defineProperties(proto,{showModal:{configurable:true,value:show},close:{configurable:true,value:close}});
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 try {
  document.body.style.overflow='auto';
  await act(async()=>root.render(<ChatToolsDialog open={false} onClose={()=>{}} label="Tools"><button>New chat</button></ChatToolsDialog>));
  expect(show).not.toHaveBeenCalled();
  await act(async()=>root.render(<ChatToolsDialog open onClose={()=>{}} label="Tools"><button>New chat</button></ChatToolsDialog>));
  expect(show).toHaveBeenCalledOnce();expect(host.querySelector('dialog')?.open).toBe(true);expect(document.body.style.overflow).toBe('hidden');
  await act(async()=>root.unmount());expect(close).toHaveBeenCalledOnce();expect(document.body.style.overflow).toBe('auto');
 }finally {host.remove();if(savedShow)Object.defineProperty(proto,'showModal',savedShow);else Reflect.deleteProperty(proto,'showModal');if(savedClose)Object.defineProperty(proto,'close',savedClose);else Reflect.deleteProperty(proto,'close');document.body.style.overflow='';}
});
