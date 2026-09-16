/** Real production bundle + real Zeus shell; synthetic RPC only. Requires a broker CDP lease. */
import {chromium} from '/home/seb/projects/ignition/node_modules/playwright/index.mjs';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
if(!process.env.ZEUS_BROWSER_MANAGED||!process.env.ZEUS_BROWSER_CDP)throw Error('Managed Zeus browser lease required');
if(!process.env.ZEUS_CHAT_SHELL||!process.env.ZEUS_CHAT_EVIDENCE)throw Error('Explicit candidate shell and evidence paths required');
const repo=fileURLToPath(new URL('../../',import.meta.url)),shell=process.env.ZEUS_CHAT_SHELL,out=process.env.ZEUS_CHAT_EVIDENCE;
const dist=path.join(repo,'hermes_cli/web_dist');await fs.mkdir(out,{recursive:true});
// Only GET metadata may reach the real service. No production RPC, prompt, or write reaches it.
const tokenHTML=await (await fetch('http://127.0.0.1:9120/')).text();
const token=tokenHTML.match(/__HERMES_SESSION_TOKEN__="([^"]+)"/)?.[1];
if(!token)throw Error('Existing read-only dashboard auth unavailable');
const transcript=new Map([['history',Array.from({length:24},(_,i)=>({role:i%2?'assistant':'user',text:i%2?`Saved reply ${i}: ${'Readable chat. '.repeat(15)}`:`Saved question ${i}`}))]]);
let sessions=[{id:'history',title:'[QA fixture] Saved conversation',preview:'Earlier messages',source:'desktop',profile:'zeus-os',last_active:1700000000,started_at:1700000000,is_active:false,message_count:24}];
const calls=[],results=[],errors=[],requests=[],timers=[];let mode='answer',count=0,currentSocket=null,currentRuntime=null;
const runtimeToStored=new Map();
const mime={'.html':'text/html','.css':'text/css','.js':'text/javascript','.json':'application/json','.svg':'image/svg+xml','.png':'image/png','.woff2':'font/woff2'};
const server=http.createServer(async(req,res)=>{
 try{
  const u=new URL(req.url,'http://localhost'),p=u.pathname;
  if(req.method!=='GET'&&req.method!=='HEAD'){res.writeHead(405);res.end();return;}
  if(p==='/ai/api/sessions'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({sessions,total:sessions.length,limit:50,offset:0}));return;}
  if(p.startsWith('/ai/api/')){
   const response=await fetch('http://127.0.0.1:9120'+p.slice(3)+u.search,{headers:{'X-Hermes-Session-Token':token},signal:AbortSignal.timeout(8000)});
   res.writeHead(response.status,{'Content-Type':response.headers.get('Content-Type')||'application/json'});res.end(Buffer.from(await response.arrayBuffer()));return;
  }
  if(p==='/status/workspace.json'){res.setHeader('Content-Type','application/json');res.end(await fs.readFile('/var/www/zeus-home/status/workspace.json'));return;}
  if(p.startsWith('/control/')){res.writeHead(503,{'Content-Type':'application/json'});res.end('{"error":"Isolated UI fixture; production writes disabled"}');return;}
  let root=shell,rel=p==='/'?'index.html':p.slice(1),isChat=false;
  if(p.startsWith('/ai/')){root=dist;rel=p.slice(4);if(!rel||rel==='chat'){rel='index.html';isChat=true;}}
  const file=path.resolve(root,rel);if(!file.startsWith(path.resolve(root)+path.sep)){res.writeHead(404);res.end();return;}
  let data=await fs.readFile(file);
  if(isChat)data=Buffer.from(data.toString().replace('</head>',`<script>window.__HERMES_SESSION_TOKEN__="${token}";window.__HERMES_BASE_PATH__="/ai";window.__HERMES_DASHBOARD_EMBEDDED_CHAT__=true;window.__HERMES_AUTH_REQUIRED__=false;</script></head>`));
  res.setHeader('Content-Type',mime[path.extname(file)]||'application/octet-stream');res.setHeader('Cache-Control','no-store');res.end(data);
 }catch{res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const base=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.connectOverCDP(process.env.ZEUS_BROWSER_CDP);
const context=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,reducedMotion:'reduce'});
const frameEvent=(ws,type,sid,payload)=>{try{ws.send(JSON.stringify({jsonrpc:'2.0',method:'event',params:{type,session_id:sid,payload}}));}catch{}};
const later=(fn,ms)=>timers.push(setTimeout(fn,ms));
const complete=(ws,sid,text)=>{const stored=runtimeToStored.get(sid);if(stored)transcript.get(stored)?.push({role:'assistant',text});frameEvent(ws,'message.complete',sid,{text});};
await context.routeWebSocket('**/ai/api/ws*',ws=>{
 currentSocket=ws;frameEvent(ws,'gateway.ready',undefined,{replay_epoch:'isolated-qa'});
 ws.onMessage(raw=>{
  const call=JSON.parse(raw);if(!call.method)return;calls.push({method:call.method,params:call.params});
  const p=call.params||{},reply=result=>ws.send(JSON.stringify({jsonrpc:'2.0',id:call.id,result}));
  if(call.method==='session.create'){
   const stored=`qa-${++count}`,sid=`runtime-${count}`;runtimeToStored.set(sid,stored);currentRuntime=sid;transcript.set(stored,[]);
   sessions.unshift({id:stored,title:p.title,source:'desktop',profile:'zeus-os',preview:p.title,last_active:Date.now()/1000,started_at:Date.now()/1000,is_active:false,message_count:0});
   reply({session_id:sid,stored_session_id:stored,messages:[]});return;
  }
  if(call.method==='session.resume'){
   const sid=`resumed-${p.session_id}`;currentRuntime=sid;runtimeToStored.set(sid,p.session_id);
   reply({session_id:sid,session_key:p.session_id,messages:transcript.get(p.session_id)||[],running:false,info:{}});return;
  }
  if(call.method==='prompt.submit'){
   const sid=p.session_id,stored=runtimeToStored.get(sid);currentRuntime=sid;transcript.get(stored)?.push({role:'user',text:p.text});
   if(mode==='disconnect'){mode='answer';transcript.get(stored)?.push({role:'assistant',text:'Received once before the connection dropped.'});later(()=>ws.close(),50);return;}
   later(()=>reply({accepted:true}),300);
   later(()=>frameEvent(ws,'message.start',sid,{}),350);
   if(mode==='approval'){later(()=>frameEvent(ws,'approval.request',sid,{request_id:'approve-qa',choices:['once','deny'],command:'Read-only QA fixture action',reason:'Review this test action before it proceeds.'}),400);return;}
   if(mode==='clarify'){later(()=>frameEvent(ws,'clarify.request',sid,{request_id:'clarify-qa',question:'Which project should I review?',choices:['Example A','Example B']}),400);return;}
   if(mode==='hold')return;
   later(()=>frameEvent(ws,'message.delta',sid,{text:'I checked the current details.'}),420);
   later(()=>frameEvent(ws,'message.interim',sid,{text:'I checked the current details.',already_streamed:true}),500);
   later(()=>frameEvent(ws,'tool.start',sid,{tool_id:'qa-tool',name:'read_file',args:{secret:'never-render-this'}}),550);
   later(()=>frameEvent(ws,'tool.complete',sid,{tool_id:'qa-tool',name:'read_file'}),600);
   later(()=>complete(ws,sid,'**Here is the answer.**\n\nA normal conversation, with details out of the way.\n\n```text\n'+ 'long-code-'.repeat(55)+'\n```\n\n[Unsafe link](javascript:alert(1))'),950);return;
  }
  if(call.method==='session.interrupt'){reply({interrupted:true});later(()=>complete(ws,p.session_id,'Stopped at your request.'),60);return;}
  if(call.method==='approval.respond'){reply({resolved:1});later(()=>complete(ws,p.session_id,'Your explicit choice was received.'),60);return;}
  if(call.method==='clarify.respond'){reply({ok:true});later(()=>complete(ws,p.session_id,'Your reply was received.'),60);return;}
  if(call.method==='session.events.since'){reply({events:[],truncated:false,replay_epoch:'isolated-qa'});return;}
  reply({ok:true});
 });
});
const page=await context.newPage();page.setDefaultTimeout(12000);page.on('pageerror',error=>errors.push(error.message));page.on('request',req=>requests.push(new URL(req.url()).pathname));
let chat;
async function check(name,fn){try{await fn();results.push({name,pass:true});console.log('PASS',name);}catch(error){results.push({name,pass:false,error:error.message});console.log('FAIL',name,error.message.slice(0,350));await page.screenshot({path:path.join(out,`failure-${results.length}.png`)}).catch(()=>{});}}
async function newSend(text,nextMode='answer'){mode=nextMode;await chat.getByRole('button',{name:'New conversation',exact:true}).first().click();await chat.locator('#zeus-message').fill(text);await chat.getByRole('button',{name:'Send message',exact:true}).click();}
try{
 await page.goto(base+'/#view=ai',{waitUntil:'domcontentloaded'});
 chat=page.frameLocator('#zeus-ai-frame');await chat.locator('[data-zeus-chat="native"]').waitFor({timeout:60000});
 await check('Native chat loads inside the real Zeus shell, without a terminal',async()=>{assert.equal(await chat.locator('.xterm,canvas').count(),0);assert.equal(await chat.locator('#zeus-message').count(),1);assert.ok(!requests.some(url=>url.includes('/api/pty')));});
 await page.screenshot({path:path.join(out,'mobile-empty.png')});
 for(const [width,height] of [[320,568],[390,844],[390,420],[844,390],[768,1024],[1440,900]])await check(`${width}x${height}: no horizontal overflow; composer fits viewport`,async()=>{
  await page.setViewportSize({width,height});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const f=page.frames().find(f=>f.url().includes('/ai/chat'));
  const dims=await f.evaluate(()=>{const root=document.querySelector('.zeus-chat'),input=document.querySelector('.zc-composer');return {width:innerWidth,scroll:root.scrollWidth,bottom:input.getBoundingClientRect().bottom,height:innerHeight,top:input.getBoundingClientRect().top};});
  assert.ok(dims.scroll<=dims.width+1,JSON.stringify(dims));assert.ok(dims.bottom<=dims.height+1&&dims.top>=0,JSON.stringify(dims));
  const frameBox=await page.locator('#zeus-ai-frame').boundingBox();assert.ok(frameBox.y>=-1&&frameBox.y+frameBox.height<=height+1,JSON.stringify(frameBox));
  const composerBox=await chat.locator('#zeus-message').boundingBox();assert.ok(composerBox.y>=0&&composerBox.y+composerBox.height<=height+1,JSON.stringify(composerBox));
 });
 await page.setViewportSize({width:390,height:844});

 await check('Executive question reaches the real native composer once, without a prompt or execution',async()=>{
  const before=calls.filter(c=>c.method==='prompt.submit').length;
  await chat.getByRole('button',{name:'Back to Zeus OS',exact:true}).click();
  await page.locator('.founder-axis[data-id="money"]').click();await page.locator('[data-action="founder-deep"]').click();
  await chat.locator('#zeus-message').filter({visible:true}).waitFor();
  await page.waitForFunction(()=>document.querySelector('#zeus-ai-frame').contentDocument?.querySelector('#zeus-message')?.value==='What is our software MRR?');
  assert.equal(calls.filter(c=>c.method==='prompt.submit').length,before);
  await chat.locator('#zeus-message').fill('');
 });
 await check('A handoff never destroys the existing draft and has an explicit usable recovery',async()=>{
  const before=calls.filter(c=>c.method==='prompt.submit').length;
  await chat.locator('#zeus-message').fill('An existing owner draft');
  await chat.getByRole('button',{name:'Back to Zeus OS',exact:true}).click();
  await page.locator('[data-action="founder-ask"][data-id="yesterday"]').click();await page.locator('[data-action="founder-deep"]').click();
  await chat.locator('.zc-incoming-question').waitFor();assert.equal(await chat.locator('#zeus-message').inputValue(),'An existing owner draft');
  assert.equal(await chat.getByRole('button',{name:'Use question',exact:true}).isDisabled(),true);
  for(const [width,height] of [[390,420],[844,390]]){await page.setViewportSize({width,height});await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));const box=await chat.locator('#zeus-message').boundingBox();assert.ok(box.y>=0&&box.y+box.height<=height+1,JSON.stringify(box));}
  await page.setViewportSize({width:390,height:844});
  await chat.locator('#zeus-message').fill('');await chat.getByRole('button',{name:'Use question',exact:true}).click();
  assert.equal(await chat.locator('#zeus-message').inputValue(),'What made money yesterday?');assert.equal(await chat.locator('.zc-incoming-question').count(),0);
  assert.equal(calls.filter(c=>c.method==='prompt.submit').length,before);await chat.locator('#zeus-message').fill('');
 });
 await check('Instant company answers use the parent snapshot without any model prompt and preserve the composer',async()=>{
  const before=calls.filter(c=>c.method==='prompt.submit').length;
  await chat.locator('#zeus-message').fill('Preserve this draft');
  await chat.getByRole('button',{name:'Instant company answers',exact:true}).click();
  try{
   await chat.getByRole('button',{name:'What made money yesterday?',exact:true}).click();
   await chat.locator('.zc-quick-answer').waitFor();const answer=await chat.locator('.zc-quick-answer').innerText();
   assert.match(answer,/(CURRENT|PARTIAL|UNKNOWN|UNAVAILABLE|STALE) · No model call/);assert.ok((await chat.locator('.zc-quick-answer h3').innerText()).trim().length>0);
   assert.equal(calls.filter(c=>c.method==='prompt.submit').length,before);
  }finally{if(await chat.getByRole('button',{name:'Close instant answers',exact:true}).isVisible().catch(()=>false))await chat.getByRole('button',{name:'Close instant answers',exact:true}).click();}
  assert.equal(await chat.locator('#zeus-message').inputValue(),'Preserve this draft');await chat.locator('#zeus-message').fill('');
 });
 await check('Instant answers fit dark and small-phone viewports and expose actual evidence',async()=>{
  await page.emulateMedia({colorScheme:'dark',reducedMotion:'reduce'});await page.setViewportSize({width:320,height:568});
  await chat.getByRole('button',{name:'Instant company answers',exact:true}).click();
  await chat.getByRole('button',{name:'Give me the company briefing.',exact:true}).click();await chat.locator('.zc-quick-answer').waitFor();
  await chat.locator('.zc-quick-answer summary').click();assert.match(await chat.locator('.zc-quick-answer').innerText(),/Barcelona|Timestamp unavailable/);
  const f=page.frames().find(f=>f.url().includes('/ai/chat'));
  assert.ok(await f.evaluate(()=>{const n=document.querySelector('.zc-quick-dialog');return n.scrollWidth<=n.clientWidth+1&&n.getBoundingClientRect().width<=innerWidth;}));
  await page.screenshot({path:path.join(out,'instant-answers-320-dark.png')});await chat.getByRole('button',{name:'Close instant answers',exact:true}).click();
  await page.emulateMedia({colorScheme:'light',reducedMotion:'reduce'});await page.setViewportSize({width:390,height:844});
 });
 await check('Global dashboard notices cannot cover the immersive chat',async()=>{await page.locator('#snapshot-notice').evaluate(el=>{el.textContent='QA status source unavailable';el.hidden=false;});assert.equal(await page.locator('#snapshot-notice').isVisible(),false);});
 await check('Mobile Enter inserts a newline rather than accidentally sending',async()=>{const before=calls.filter(c=>c.method==='prompt.submit').length;await chat.locator('#zeus-message').fill('First line');await chat.locator('#zeus-message').press('Enter');assert.equal(await chat.locator('#zeus-message').inputValue(),'First line\n');assert.equal(calls.filter(c=>c.method==='prompt.submit').length,before);});
 await check('Streaming, new draft during send, collapsed tools, and safe Markdown',async()=>{
  await chat.locator('#zeus-message').fill('Please review this example');await chat.getByRole('button',{name:'Send message',exact:true}).click();await chat.locator('#zeus-message').fill('Draft for my next message');
  await chat.getByText('Here is the answer.',{exact:false}).waitFor();
  assert.equal(await chat.locator('#zeus-message').inputValue(),'Draft for my next message');assert.equal(await chat.locator('.zc-tools').getAttribute('open'),null);assert.equal(await chat.locator('a[href^="javascript:"]').count(),0);assert.match(await chat.locator('.markdown-blocked-link').innerText(),/Unsafe link.*link blocked/);assert.doesNotMatch(await chat.locator('.zc-transcript').innerText(),/Unsafe link\)/);assert.ok(!(await chat.locator('.zc-transcript').innerText()).includes('never-render-this'));
  const f=page.frames().find(f=>f.url().includes('/ai/chat'));assert.equal(await f.evaluate(()=>document.querySelector('.zc-scroll').scrollWidth<=innerWidth+1),true);
  assert.ok(await chat.locator('pre').evaluate(el=>el.scrollWidth<=el.clientWidth+1),'Long code wraps within the transcript');
  await page.screenshot({path:path.join(out,'mobile-conversation.png')});
 });
 await check('Approval is visible and never answered automatically',async()=>{await newSend('Approval test','approval');await chat.getByRole('button',{name:'Allow once',exact:true}).waitFor();assert.equal(calls.filter(c=>c.method==='approval.respond').length,0);await chat.getByRole('button',{name:'Do not allow',exact:true}).click();await chat.getByText('Your explicit choice was received.',{exact:true}).waitFor();assert.deepEqual(calls.filter(c=>c.method==='approval.respond').map(c=>c.params.choice),['deny']);});
 await check('Clarification is answered through a normal form',async()=>{await newSend('Question test','clarify');await chat.getByText('Which project should I review?',{exact:true}).waitFor();assert.equal(await chat.locator('#zeus-message').count(),0,'Only the reply form should be shown while Zeus is asking a question');await chat.getByRole('button',{name:'Example A',exact:true}).click();await chat.getByRole('button',{name:'Send reply',exact:true}).click();await chat.getByText('Your reply was received.',{exact:true}).waitFor();assert.equal(calls.find(c=>c.method==='clarify.respond').params.answer,'Example A');});
 await check('Stop uses the gateway interrupt and waits for completion',async()=>{await newSend('Stop test','hold');await chat.getByRole('button',{name:'Stop response',exact:true}).click();await chat.getByText('Stopped at your request.',{exact:true}).waitFor();assert.equal(calls.filter(c=>c.method==='session.interrupt').length,1);});
 await check('History restores messages; scrolling up is not hijacked by streaming',async()=>{
  await chat.getByRole('button',{name:'Conversation history',exact:true}).click();await chat.getByRole('button',{name:'[QA fixture] Saved conversation',exact:false}).click();await chat.getByText('Saved question 0',{exact:true}).waitFor();
  const f=page.frames().find(f=>f.url().includes('/ai/chat'));await f.evaluate(()=>{const n=document.querySelector('.zc-scroll');n.scrollTop=0;n.dispatchEvent(new Event('scroll'));});
  frameEvent(currentSocket,'message.delta',currentRuntime,{text:'A new update at the bottom.'});await chat.getByRole('button',{name:'Jump to latest',exact:true}).waitFor();assert.equal(await f.evaluate(()=>document.querySelector('.zc-scroll').scrollTop),0);
  await chat.getByRole('button',{name:'Jump to latest',exact:true}).click();assert.ok(await f.evaluate(()=>document.querySelector('.zc-scroll').scrollTop>0));frameEvent(currentSocket,'message.complete',currentRuntime,{text:'A new update at the bottom.'});
 });
 await check('Lost acknowledgement is reconciled without automatically resending, including after reload',async()=>{
  const before=calls.filter(c=>c.method==='prompt.submit').length;await newSend('Exactly one delivery','disconnect');await chat.getByText('Received once before the connection dropped.',{exact:true}).waitFor();assert.equal(calls.filter(c=>c.method==='prompt.submit').length,before+1);assert.equal(await chat.locator('#zeus-message').inputValue(),'Exactly one delivery');
  await page.reload({waitUntil:'domcontentloaded'});chat=page.frameLocator('#zeus-ai-frame');await chat.getByText('Received once before the connection dropped.',{exact:true}).waitFor();assert.equal(calls.filter(c=>c.method==='prompt.submit').length,before+1);assert.equal(await chat.getByRole('button',{name:'Send message',exact:true}).isDisabled(),true);
 });
 await check('Back exits immersive chat and returns focus; unrelated window messages cannot close chat',async()=>{
  await page.evaluate(()=>window.postMessage({type:'zeus-chat:close'},location.origin));assert.equal(await page.locator('#ai-section').isVisible(),true);
  await chat.getByRole('button',{name:'Back to Zeus OS',exact:true}).click();await page.locator('#ai-section').waitFor({state:'hidden'});assert.equal(await page.evaluate(()=>document.body.classList.contains('zeus-chat-open')),false);assert.equal(await page.evaluate(()=>document.activeElement?.id),'ask-zeus-open');
 });
 await check('A failed chat bootstrap keeps a working exit and cannot strand the phone',async()=>{
  await page.route('**/ai/chat?**',route=>route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><html><body>Intentionally missing native bootstrap</body></html>'}));
  await page.goto(base+'/?qa-missing-native=1#view=ai',{waitUntil:'domcontentloaded'});
  await page.locator('#ai-status').waitFor({state:'visible'});await page.locator('#ai-status').getByRole('button',{name:'Back to Zeus OS',exact:true}).click();
  await page.locator('#ai-section').waitFor({state:'hidden'});assert.equal(await page.evaluate(()=>document.body.classList.contains('zeus-chat-open')),false);
  await page.unroute('**/ai/chat?**');
 });
 await check('Every execution RPC remains Zeus-scoped, and no PTY or browser errors occur',async()=>{for(const call of calls.filter(c=>['session.create','session.resume','prompt.submit','session.interrupt','approval.respond','clarify.respond'].includes(c.method)))assert.equal(call.params.profile,'zeus-os');assert.ok(!requests.some(url=>url.includes('/api/pty')));assert.deepEqual(errors,[]);});
}finally{
 for(const timer of timers)clearTimeout(timer);
 await fs.writeFile(path.join(out,'results.json'),JSON.stringify({testedAt:new Date().toISOString(),results,errors,rpcCounts:Object.fromEntries([...new Set(calls.map(c=>c.method))].map(method=>[method,calls.filter(c=>c.method===method).length])),productionWrites:0,physicalIOS:false},null,2));
 await context.close();await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
}
if(results.some(r=>!r.pass))process.exitCode=1;
