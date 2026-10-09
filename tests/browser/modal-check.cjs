// Modal focus, continuity, failure visibility, and audience presentation with real WebRTC.
const {chromium}=require('playwright');
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path');
const folder=path.resolve(process.argv[2]||'.runtime/modal-polish/ui');
const access=JSON.parse(fs.readFileSync(path.join(folder,'access.json')));
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
 const errors=[],checks=[];
 try {
 for(const [width,height] of [[1440,900],[1280,800],[390,844]]){
  const context=await browser.newContext({viewport:{width,height},reducedMotion:'reduce'});
  context.on('page',p=>p.on('pageerror',e=>errors.push(e.message)));
  const page=await context.newPage();await page.goto(access.operator_url);
  await page.waitForFunction(()=>document.querySelector('#program').getVideoPlaybackQuality().totalVideoFrames>15&&document.querySelectorAll('.graphic-tile').length===24);
  await page.evaluate(()=>{window.savedVideo=document.querySelector('#program');window.savedStream=savedVideo.srcObject;window.framesBefore=savedVideo.getVideoPlaybackQuality().totalVideoFrames;window.reconnects=0;const original=fetch;window.fetch=(...args)=>{if(String(args[0]).includes('/whep')&&args[1]?.method==='POST')reconnects++;return original(...args);};});
  const before=await page.locator('#program').boundingBox();
  for(const name of ['replays','graphics','audio','settings','commands']){
   const selector=name==='commands'?'#chat-help':name==='settings'?'.composer-toolbar [data-panel=settings]':`.program-toolbar [data-panel=${name}]`;
   await page.locator(selector).click();
   assert(await page.locator('#asset-drawer').evaluate(d=>d.matches(':modal')));
   const program=await page.locator('#program').boundingBox();assert.equal(program.width,before.width);assert.equal(program.height,before.height);
   const rect=await page.locator('#asset-drawer').boundingBox();assert(rect.x>=0&&rect.y>=0&&rect.x+rect.width<=width&&rect.y+rect.height<=height);
   for(let i=0;i<8;i++){await page.keyboard.press('Tab');assert(await page.evaluate(()=>document.querySelector('#asset-drawer').contains(document.activeElement)));}
   if(name==='graphics') await page.waitForFunction(()=>{const box=document.querySelector('#graphics-gallery').getBoundingClientRect();return [...document.querySelectorAll('#graphics-gallery img')].filter(img=>{const r=img.getBoundingClientRect();return r.top<box.bottom&&r.bottom>box.top;}).every(img=>img.complete&&img.naturalWidth>0);});
   await page.screenshot({path:path.join(folder,`modal-${name}-${width}.png`)});
   await page.keyboard.press('Escape');assert(await page.locator(selector).evaluate(b=>document.activeElement===b));
  }
  await page.locator('.program-toolbar [data-panel=graphics]').click();await page.locator('#graphic-title').fill('Saved modal draft');
  await page.locator('.modal-feature-tabs [data-panel=replays]').click();await page.locator('#seconds').selectOption('6');
  await page.locator('.modal-feature-tabs [data-panel=graphics]').click();assert.equal(await page.locator('#graphic-title').inputValue(),'Saved modal draft');
  await page.mouse.click(1,1);await page.waitForFunction(()=>!document.querySelector('#asset-drawer').open);
  await page.locator('.program-toolbar [data-panel=replays]').click();assert.equal(await page.locator('#seconds').inputValue(),'6');
  await page.locator('#multi-camera-replay > summary').click();await page.locator('#replay-plan').fill('{');await page.locator('#validate-plan').click();
  assert((await page.locator('#plan-result').innerText()).startsWith('Rejected:'));
  await page.locator('#multi-camera-replay details > summary').first().click();
  await page.locator('#replay-record').fill('{');await page.locator('#submit-replay-record').click();
  await page.locator('#message').waitFor({state:'visible'});assert(await page.locator('#message').evaluate(el=>el.closest('dialog')?.id==='asset-drawer'));
  await page.locator('.modal-feature-tabs [data-panel=settings]').click();
  await page.route('**/api/event/end',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Injected end failure'})}));
  await page.locator('#end').click();await page.locator('#confirm-end').click();
  await page.waitForFunction(()=>document.querySelector('#message').textContent==='Injected end failure');
  assert(await page.locator('#message').isVisible());assert(await page.locator('#message').evaluate(el=>el.closest('dialog')?.id==='end-confirm'));
  await page.keyboard.press('Escape');
  await page.waitForFunction(()=>!document.querySelector('#end-confirm').open&&document.querySelector('#message').closest('dialog')?.id==='asset-drawer');
  assert(await page.locator('#asset-drawer').evaluate(d=>d.open));
  assert(await page.locator('#message').evaluate(el=>el.closest('dialog')?.id==='asset-drawer'));
  await page.locator('#close-drawer').click();
  assert(await page.evaluate(()=>savedVideo===document.querySelector('#program')&&savedStream===savedVideo.srcObject&&savedVideo.getVideoPlaybackQuality().totalVideoFrames>framesBefore&&reconnects===0));
  checks.push({width,height,native_modal:true,focus_trap:true,escape_focus_return:true,backdrop_close:true,drafts_preserved:true,player_unchanged:true,reconnects:0,errors_visible_in_active_modal:true,nested_confirmation_escape:true});
  await page.goto(access.broadcast_url);await page.waitForFunction(()=>document.querySelector('#qr').naturalWidth>0&&document.querySelector('#program').getVideoPlaybackQuality().totalVideoFrames>15);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:path.join(folder,`broadcast-${width}.png`),fullPage:true});
  await page.goto(access.public_url+'/join');await page.waitForFunction(()=>new URL(location.href).searchParams.has('code'));
  assert(await page.locator('#preview-container').isHidden());assert.equal(await page.locator('.program-panel').count(),0);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:path.join(folder,`join-${width}.png`),fullPage:true});
  await context.close();
 }
 assert.deepEqual(errors,[]);
 fs.writeFileSync(path.join(folder,'modal-report.json'),JSON.stringify({passed:true,browser:await browser.version(),checks,page_errors:errors,failure_injection:'End request intercepted with HTTP 503; actual server was not ended'},null,2)+'\n');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
