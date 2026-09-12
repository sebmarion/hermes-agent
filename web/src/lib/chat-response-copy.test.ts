import {describe,it,expect,vi} from 'vitest';
import {copyBoundReply,lastAssistantReply} from './chat-response-copy';
const binding={id:'session-qa',profile:'zeus-os',channel:'channel-qa'};
const reply={session_id:'session-qa',profile:'zeus-os',messages:[{role:'user',content:'not the reply'},{role:'assistant',content:'earlier'},{role:'assistant',content:'Exact \nreply ✓'}]};
describe('read-only bound reply copy',()=>{
 it('preserves raw content from the latest assistant, not the user echo',()=>expect(lastAssistantReply(reply,binding)).toBe('Exact \nreply ✓'));
 it('rejects a different conversation or profile',()=>{for(const patch of [{session_id:'other'},{profile:'other'}])expect(()=>lastAssistantReply({...reply,...patch},binding)).toThrow();});
 it('rejects missing and malformed messages rather than announcing copied',()=>{for(const value of [null,{}, {...reply,messages:null},{...reply,messages:[{role:'user',content:'echo'}]}])expect(()=>lastAssistantReply(value,binding)).toThrow();});
 it('does not replace the clipboard after a scope change',async()=>{const write=vi.fn();expect(await copyBoundReply(binding,async()=>reply,write,()=>false)).toBe(false);expect(write).not.toHaveBeenCalled();});
 it('propagates clipboard failure instead of claiming success',async()=>{const write=vi.fn(async()=>false);expect(await copyBoundReply(binding,async()=>reply,write,()=>true)).toBe(false);expect(write).toHaveBeenCalledWith('Exact \nreply ✓');});
 it('does not treat pending tool calls as completed assistant replies',()=>{expect(lastAssistantReply({...reply,messages:[...reply.messages,{role:'assistant',content:'working',tool_calls:[{}]}]},binding)).toBe('Exact \nreply ✓');});
});
