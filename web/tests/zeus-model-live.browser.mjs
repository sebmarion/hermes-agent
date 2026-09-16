/** Opt-in isolated QA against the real gateway; never changes profile defaults or owner chats. */
import {chromium} from '/home/seb/projects/ignition/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';import path from 'node:path';import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';import {createHash} from 'node:crypto';
assert(process.env.ZEUS_BROWSER_MANAGED&&process.env.ZEUS_BROWSER_CDP,'Managed browser required');
const out=process.env.ZEUS_MODEL_EVIDENCE;assert(out&&path.isAbsolute(out),'Evidence path required');
const mode=process.env.ZEUS_MODEL_QA||'inspect';assert(['inspect','switch','published'].includes(mode));
const repo=fileURLToPath(new URL('../../',import.meta.url)),dist=path.join(repo,'hermes_cli/web_dist');
const origin='https://zeus.tailfad2e3.ts.net:3300',config='/home/seb/.hermes/profiles/zeus-os/config.yaml';
const sha=data=>createHash('sha256').update(data).digest('hex');const configBefore=sha(await fs.readFile(config));
await fs.mkdir(out,{recursive:true,mode:0o700});
const browser=await chromium.connectOverCDP(process.env.ZEUS_BROWSER_CDP),context=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,reducedMotion:'reduce'});
const report={startedAt:new Date().toISOString(),mode,physicalIOS:false,errors:[],rpc:[],sessionReadbacks:[],socketPaths:[],networkFailures:[],httpWrites:[],pass:false};
if(mode!=='published'){
 // Browser-fulfilled fixture documents lose the server IP address classification. Grant
 // this disposable context access to the already-authorized Zeus origin only;
 // normal published-origin verification requires no permission override.
 await context.grantPermissions(['local-network-access'],{origin});
 const original=await (await context.request.get(origin+'/ai/chat?profile=zeus-os&embed=zeus')).text();
 const boot=[...original.matchAll(/<script\b[^>]*>[\s\S]*?<\/script>/g)].map(match=>match[0]).filter(script=>script.includes('__HERMES_'));
 assert(boot.some(script=>script.includes('__HERMES_SESSION_TOKEN__')),'Existing authenticated bootstrap missing');
 const html=(await fs.readFile(path.join(dist,'index.html'),'utf8')).replace('</head>',boot.join('')+'</head>');
 await context.route('**/ai/chat?**',route=>route.fulfill({status:200,contentType:'text/html',body:html}));
 await context.route('**/ai/assets/**',async route=>{
  const rel=new URL(route.request().url()).pathname.slice('/ai/'.length),file=path.resolve(dist,rel);assert(file.startsWith(dist+path.sep));
  const type=file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.woff2')?'font/woff2':'application/octet-stream';
  await route.fulfill({status:200,contentType:type,body:await fs.readFile(file)});
 });
}
const page=await context.newPage();page.setDefaultTimeout(30000);
page.on('pageerror',error=>report.errors.push(error.message));
page.on('requestfailed',req=>{const u=new URL(req.url());report.networkFailures.push({host:u.host,path:u.pathname,error:req.failure()?.errorText});});
page.on('request',request=>{if(!['GET','HEAD'].includes(request.method()))report.httpWrites.push({method:request.method(),path:new URL(request.url()).pathname});});
page.on('websocket',ws=>{ws.on('framereceived',frame=>{try{const data=JSON.parse(frame.payload),r=data.result;if(r?.session_id)report.sessionReadbacks.push({runtime:r.session_id,stored:r.stored_session_id,key:r.session_key,resumed:r.resumed,running:r.running,info:{model:r.info?.model,provider:r.info?.provider,lazy:r.info?.lazy}});}catch{}});const url=new URL(ws.url());report.socketPaths.push({host:url.host,path:url.pathname,queryKeys:[...url.searchParams.keys()],tokenLength:url.searchParams.get('token')?.length});ws.on('framesent',frame=>{try{const data=JSON.parse(frame.payload);if(data.method)report.rpc.push({method:data.method,...(data.method==='config.set'?{key:data.params.key,value:data.params.value,profile:data.params.profile}: {})});}catch{}});});
try{
 await page.goto(origin+'/?qa-model-selector=1#view=ai',{waitUntil:'domcontentloaded'});
 let chat=page.frameLocator('#zeus-ai-frame');await chat.locator('#zeus-message').waitFor({timeout:60000});
 assert.equal(await chat.locator('.zc-assistant').count(),0,'QA must use a new isolated conversation');
 if(mode!=='inspect')await chat.locator('#zeus-message').fill('[MODEL SELECTOR QA] Isolated model switching acceptance');
 await chat.getByRole('button',{name:/^Change model/}).click();await chat.locator('.zc-model-list button').first().waitFor();
 report.initialModel=await chat.locator('.zc-model-trigger').innerText();report.available=await chat.locator('.zc-model-list button').allTextContents();
 await page.screenshot({path:path.join(out,'model-options-390.png')});
 if(mode==='inspect'){
  await chat.getByRole('button',{name:'Close model selector',exact:true}).click();
 }else{
  const target=process.env.ZEUS_MODEL_TARGET;assert(target,'Explicit target model required for live switch QA');
  const option=chat.locator('.zc-model-list button').filter({has:chat.getByText(target,{exact:true})});assert.equal(await option.count(),1,'Target must be an unambiguous connected model');
  await option.click();await chat.getByRole('button',{name:'Use model',exact:true}).click();
  await Promise.race([chat.locator('.zc-model-dialog').waitFor({state:'hidden',timeout:60000}),chat.locator('.zc-model-feedback [role=alert]').waitFor({timeout:60000}).then(async()=>{throw Error(await chat.locator('.zc-model-feedback').innerText());})]);
  report.selectedModel=await chat.locator('.zc-model-trigger').innerText();assert(report.selectedModel.includes(target));
  assert.equal(report.rpc.filter(row=>row.method==='prompt.submit').length,0,'Selecting a model must not send a prompt');
  const prompt='[MODEL SELECTOR READ-ONLY QA] Reply exactly ZEUS_MODEL_SELECTOR_OK. Do not use tools, inspect files, change anything, or contact anyone.';
  await chat.locator('#zeus-message').fill(prompt);const started=Date.now();await chat.getByRole('button',{name:'Send message',exact:true}).click();
  await chat.locator('.zc-assistant').filter({hasText:'ZEUS_MODEL_SELECTOR_OK'}).waitFor({timeout:180000});
  await chat.getByRole('button',{name:'Send message',exact:true}).waitFor({state:'visible',timeout:180000});
  report.responseMs=Date.now()-started;report.sessionId=await chat.locator('body').evaluate(()=>localStorage.getItem('zeus-chat:active:v1'));
  await chat.locator('#zeus-message').fill('Retain my model and draft after reload');
  await page.reload({waitUntil:'domcontentloaded'});chat=page.frameLocator('#zeus-ai-frame');
  await chat.locator('.zc-assistant').filter({hasText:'ZEUS_MODEL_SELECTOR_OK'}).waitFor({timeout:60000});
  report.resumedModel=await chat.locator('.zc-model-trigger').innerText();assert(report.resumedModel.includes(target));
  assert.equal(await chat.locator('#zeus-message').inputValue(),'Retain my model and draft after reload');
  assert.equal(await chat.locator('body').evaluate(()=>localStorage.getItem('zeus-chat:active:v1')),report.sessionId);
  await page.screenshot({path:path.join(out,'model-switch-resumed-390.png')});
  assert.equal(report.rpc.filter(row=>row.method==='prompt.submit').length,1);
 }
 for(const row of report.rpc.filter(row=>row.method==='config.set')){assert.equal(row.key,'model');assert.equal(row.profile,'zeus-os');assert.match(row.value,/ --session$/);assert.ok(!row.value.includes('--global'));}
 const permitted=new Set(['session.create','session.resume','session.events.since','gateway.ping','model.options','config.set','prompt.submit','session.activate']);
 assert.deepEqual(report.rpc.filter(row=>!permitted.has(row.method)),[]);
 assert.deepEqual(report.errors,[]);assert.deepEqual(report.httpWrites,[]);
 assert.equal(sha(await fs.readFile(config)),configBefore,'Profile default/config must be byte-identical');
 report.profileConfigUnchanged=true;report.pass=true;
}catch(error){report.error=error.message;report.bootstrap=await page.frames().find(frame=>frame.url().includes('/ai/chat'))?.evaluate(()=>({base:window.__HERMES_BASE_PATH__,tokenLength:window.__HERMES_SESSION_TOKEN__?.length,authRequired:window.__HERMES_AUTH_REQUIRED__,origin:location.origin})).catch(()=>null);process.exitCode=1;await page.screenshot({path:path.join(out,'failure.png')}).catch(()=>{});}
finally{report.completedAt=new Date().toISOString();await fs.writeFile(path.join(out,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));await context.close();await browser.close();}
