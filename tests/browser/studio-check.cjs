// Real browser and viewer pixels. Uses the isolated studio_check.py server only.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),jsQR=require('jsqr');
const folder=path.resolve(process.argv[2]||'.runtime/autonomous-studio'),url=process.env.BREADCAST_STUDIO_TEST_URL||'http://localhost:22080';
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
 const errors=[],checks=[];
 const context=await browser.newContext({viewport:{width:1440,height:900}});
 context.on('page',p=>p.on('pageerror',e=>errors.push(e.message)));
 const page=await context.newPage();
 const state=async()=>(await context.request.get(url+'/api/status')).json();
 const action=async(op,args={})=>{
   const s=await state(),slots=new Set([args.slot||s.program.primary_slot]);
   const r=s.replays.find(r=>r.id===args.replay_id);if(r)r.validation.plan.shots.forEach(shot=>slots.add(Number(shot.source_id.replace('camera-',''))));
   const req={id:crypto.randomUUID(),op,args};
   req.expected={run_id:s.control.run_id,context_revision:s.control.context_revision,control_revision:s.control.control_revision,program_revision:s.program.revision,sources:[...slots].sort().flatMap(slot=>{const c=s.cameras.find(c=>c.slot===slot);return c?[{slot,source_path:c.source_path,epoch:c.epoch}]:[];})};
   const response=await context.request.post(url+'/api/actions',{data:req});const record=await response.json();assert(response.ok(),JSON.stringify(record));assert.notEqual(record.state,'Rejected',JSON.stringify(record));return record;
 };
 async function wait(check,message,timeout=30000){const start=performance.now();while(performance.now()-start<timeout){if(await check())return;await new Promise(r=>setTimeout(r,50));}throw Error(message);}
 async function feature(name){
  const scope=await page.locator('#asset-drawer').evaluate(d=>d.open)?'.modal-feature-tabs':'.program-toolbar';
  await page.locator(`${scope} [data-panel="${name}"]`).click();
 }
 async function visibleComplete(){
  const bounds=await page.evaluate(()=>['#program','#cameras','#takeover','#return','#graphics-clear'].map(id=>{const r=document.querySelector(id).getBoundingClientRect();return{id,x:r.x,y:r.y,bottom:r.bottom,right:r.right};}));
  for(const b of bounds)assert(b.y>=0&&b.bottom<=page.viewportSize().height&&b.right<=page.viewportSize().width,JSON.stringify(b));
  assert(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight),'Desktop page scroll');
 }
 try{
  await page.goto(url+'/operator');await page.waitForFunction(()=>document.querySelector('#program').getVideoPlaybackQuality().totalVideoFrames>20);
  for(const [width,height] of [[1440,900],[1280,800]]){
   await page.setViewportSize({width,height});await visibleComplete();
   await page.screenshot({path:path.join(folder,`studio-${width}.png`)});
  }checks.push({id:'UX1',passed:true});
  // The video and its box share one rectangle. Controls overlay the media.
  const geometry=await page.evaluate(()=>{
    const rect=el=>{const r=el.getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right};};
    return {video:rect(document.querySelector('#program')),panel:rect(document.querySelector('.program-panel')),crew:rect(document.querySelector('#crew-panel')),controls:[...document.querySelectorAll('.program-toolbar button')].filter(b=>getComputedStyle(b).display!=='none').map(rect),nativeControls:document.querySelector('#program').controls,statusVisible:document.querySelector('#program-status').getBoundingClientRect().width>1};
  });
  assert(Math.abs(geometry.video.width-geometry.panel.width)<1&&Math.abs(geometry.video.height-geometry.panel.height)<1);
  assert(Math.abs(geometry.video.width/geometry.video.height-16/9)<.01);assert(geometry.crew.width>=400);
  for(const b of geometry.controls)assert(b.x>=geometry.video.x&&b.y>=geometry.video.y&&b.right<=geometry.video.right&&b.bottom<=geometry.video.bottom);
  assert.equal(geometry.nativeControls,false);assert.equal(geometry.statusVisible,false);
  await page.locator('#takeover').focus();await page.locator('#studio-tooltip').waitFor({state:'visible'});assert((await page.locator('#studio-tooltip').innerText()).includes('control'));await page.keyboard.press('Escape');
  const audioBefore=await state();const wasMuted=await page.locator('#program').evaluate(v=>v.muted);
  await page.locator('#monitor-audio').click();assert.equal(await page.locator('#program').evaluate(v=>v.muted),!wasMuted);
  assert((await page.locator('#studio-tooltip').innerText()).includes('browser only'));
  const audioAfter=await state();assert.equal(audioAfter.program.revision,audioBefore.program.revision);assert.equal(audioAfter.program.audio_source_path,audioBefore.program.audio_source_path);assert.equal(audioAfter.program.audio_muted,audioBefore.program.audio_muted);
  await page.locator('#monitor-audio').click();assert.equal(await page.locator('#program').evaluate(v=>v.muted),wasMuted);
  await page.locator('#program-fullscreen').click();await page.waitForFunction(()=>document.fullscreenElement===document.querySelector('.monitor-frame')&&document.querySelector('#program-fullscreen').getAttribute('aria-label')==='Exit fullscreen');
  await page.locator('#program-fullscreen').click();await page.waitForFunction(()=>!document.fullscreenElement&&document.querySelector('#program-fullscreen').getAttribute('aria-label')==='Enter fullscreen');
  checks.push({id:'PROGRAM_OVERLAY',passed:true,geometry,tooltip_labels:true,local_audio_only:true,fullscreen:true});

  const camera=slot=>page.locator(`.camera-tile[data-slot="${slot}"]`);
  // Camera controls sit above a filled preview; tooltips escape the scrolling strip.
  const layout=await page.locator('.camera-tile').evaluateAll(cards=>cards.map(card=>{
    const image=card.querySelector('img').getBoundingClientRect(),picture=card.querySelector('.camera-picture').getBoundingClientRect();
    return {width:image.width,height:image.height,pictureWidth:picture.width,pictureHeight:picture.height,controlsBottom:card.querySelector('.camera-toolbar').getBoundingClientRect().bottom,imageTop:image.top};
  }));
  assert.equal(layout.length,5);for(const b of layout){assert(Math.abs(b.width-b.pictureWidth)<1&&Math.abs(b.height-b.pictureHeight)<1);assert(b.controlsBottom<=b.imageTop+1);}
  await camera(2).locator('.camera-info').focus();await page.locator('#studio-tooltip').waitFor({state:'visible'});
  assert((await page.locator('#studio-tooltip').innerText()).includes('Camera 2'));
  let tip=await page.locator('#studio-tooltip').boundingBox();assert(tip.x>=0&&tip.x+tip.width<=page.viewportSize().width&&tip.y+tip.height<=page.viewportSize().height);
  await page.keyboard.press('Escape');assert(await page.locator('#studio-tooltip').isHidden());
  await camera(1).locator('.camera-info').hover();await page.locator('#studio-tooltip').hover();assert(await page.locator('#studio-tooltip').isVisible());
  await page.locator('#chat-input').click();assert(await page.locator('#studio-tooltip').isHidden());
  await camera(2).locator('.audio').click();
  await wait(async()=>{const s=await state();return s.program.audio_slot===2&&!s.program.audio_muted&&await camera(2).locator('.audio').getAttribute('aria-pressed')==='true';},'Camera 2 microphone selected');
  assert.equal(await page.locator('.camera-tile .audio[aria-pressed="true"]').count(),1);
  assert.equal(await camera(1).locator('.audio').getAttribute('aria-pressed'),'false');
  await camera(2).locator('.audio').click();
  await wait(async()=>(await state()).program.audio_muted&&await camera(2).locator('.audio').getAttribute('aria-pressed')==='false','Mute selected microphone');
  assert.equal(await page.locator('.camera-tile .audio[aria-pressed="true"]').count(),0);
  await camera(3).locator('.audio').click();
  await wait(async()=>{const s=await state();return s.program.audio_slot===3&&!s.program.audio_muted&&await camera(3).locator('.audio').getAttribute('aria-pressed')==='true';},'Camera 3 microphone selected');
  assert.equal(await page.locator('.camera-tile .audio[aria-pressed="true"]').count(),1);
  await camera(1).locator('.audio').click();await wait(async()=>(await state()).program.audio_slot===1&&await camera(1).locator('.audio').getAttribute('aria-pressed')==='true','Restore microphone');
  checks.push({id:'CAMERA_CONTROLS',passed:true,filled_preview:true,controls_above_video:true,exclusive_microphone:true,mute_all:true,tooltip_keyboard_hover_escape:true});

  await page.evaluate(()=>{window.savedPlayer=document.querySelector('#program');window.savedStream=savedPlayer.srcObject;window.beforeFrames=savedPlayer.getVideoPlaybackQuality().totalVideoFrames;window.whepPosts=0;const original=window.fetch;window.fetch=(...args)=>{if(String(args[0]).includes('/whep')&&args[1]?.method==='POST')window.whepPosts++;return original(...args);};});
  const beforeModal=await page.locator('#program').boundingBox();
  await feature('replays');assert(await page.locator('#asset-drawer').evaluate(d=>d.matches(':modal')));
  const afterModal=await page.locator('#program').boundingBox();assert.deepEqual(afterModal,beforeModal);
  await page.locator('#seconds').selectOption('6');
  for(let n=0;n<18;n++){await page.keyboard.press('Tab');assert(await page.evaluate(()=>document.querySelector('#asset-drawer').contains(document.activeElement)));}
  await page.keyboard.press('Escape');assert.equal(await page.evaluate(()=>document.activeElement.dataset.panel),'replays');
  await feature('replays');assert.equal(await page.locator('#seconds').inputValue(),'6');
  await page.mouse.click(2,2);await page.waitForFunction(()=>!document.querySelector('#asset-drawer').open);
  await action('resume');await page.waitForFunction(()=>document.querySelector('#takeover').getAttribute('aria-pressed')==='false');
  await feature('audio');await page.locator('.modal-urgent [data-urgent="takeover"]').click();
  await wait(async()=>(await state()).control.crew_paused,'Urgent control in modal');
  assert(await page.locator('#asset-drawer').evaluate(d=>d.open));
  await page.locator('#close-drawer').click();
  checks.push({id:'FEATURE_MODALS',passed:true,native_modal:true,focus_trap:true,escape_focus_return:true,backdrop_close:true,draft_preserved:true,video_geometry_unchanged:true,urgent_control_available:true});
  await page.locator('#chat-input').fill('/prepare 1 6 0.5');
  await feature('replays');await visibleComplete();
  await page.locator('#seconds').selectOption('6');await page.locator('#speed').selectOption('0.5');
  await feature('graphics');await page.locator('#graphic-title').fill('Draft name');
  await feature('audio');await page.locator('#close-drawer').click();
  await page.locator('#collapse-crew').click();await page.locator('#collapse-crew').click();
  await feature('replays');assert.equal(await page.locator('#seconds').inputValue(),'6');
  await feature('graphics');assert.equal(await page.locator('#graphic-title').inputValue(),'Draft name');
  assert.equal(await page.locator('#chat-input').inputValue(),'/prepare 1 6 0.5');
  await page.locator('#close-drawer').click();
  await page.locator('#chat-input').fill('Replay that goal');await page.locator('#chat-send').click();
  await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('goal identification'));
  assert.equal(await page.evaluate(()=>savedPlayer===document.querySelector('#program')&&savedStream===savedPlayer.srcObject&&savedPlayer.getVideoPlaybackQuality().totalVideoFrames>beforeFrames&&whepPosts===0),true);
  checks.push({id:'UX2_G2',passed:true,reconnects:0});
  // Compact conversation, attachments, and keyboard composer use real server records.
  await page.waitForFunction(()=>document.querySelector('.human-message')?.textContent==='Replay that goal');
  assert.equal(await page.locator('#chat-input').inputValue(),'');
  assert(await page.locator('#chat-send').isDisabled());
  assert.equal(await page.locator('#crew-history').getAttribute('open'),null);
  assert(await page.locator('#crew-history-items .action-card').count()>0);
  const attachment=page.locator('.replay-attachment').last();
  assert.equal(await attachment.getAttribute('open'),null);
  await attachment.locator('summary').click();
  await attachment.locator('video').waitFor({state:'visible'});
  await attachment.locator('video').evaluate(v=>new Promise(resolve=>{if(v.readyState)resolve();else v.addEventListener('loadedmetadata',resolve,{once:true});}));
  const previewBounds=await attachment.locator('video').boundingBox();assert(previewBounds.height<=147&&previewBounds.width<=261);
  await attachment.locator('summary').click();
  await page.locator('#chat-help').click();assert(await page.locator('#command-help').isVisible());
  await page.locator('#close-drawer').click();
  await page.waitForFunction(()=>!document.querySelector('#asset-drawer').open&&document.querySelector('#chat-help').getAttribute('aria-expanded')==='false');
  await page.locator('#chat-input').fill('/camera 2');await page.keyboard.press('Shift+Enter');
  assert((await page.locator('#chat-input').inputValue()).includes('\n'));
  await page.locator('#chat-input').fill('/camera 2');await page.keyboard.press('Enter');
  await wait(async()=>(await state()).control.actions.some(a=>a.input_text==='/camera 2'),'Command stored on server');
  await page.reload();await page.waitForFunction(()=>[...document.querySelectorAll('.human-message')].some(p=>p.textContent==='/camera 2'));
  await page.setViewportSize({width:1440,height:900});await visibleComplete();
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await page.screenshot({path:path.join(folder,'crew-chat-1440.png')});
  await feature('graphics');
  checks.push({id:'CREW_UX',passed:true,collapsed_history:true,compact_preview:true,enter_to_send:true,shift_enter_newline:true,server_message_restored:true});

  await page.locator('#close-drawer').click();assert.equal(await page.evaluate(()=>document.activeElement.dataset.panel),'graphics');
  const publicView=await context.newPage();await publicView.goto(url+'/');
  await publicView.waitForFunction(()=>document.querySelector('#qr').naturalWidth>0);
  const qr=await publicView.locator('#qr').evaluate(img=>{const canvas=document.createElement('canvas');canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;const c=canvas.getContext('2d');c.drawImage(img,0,0);return{width:canvas.width,height:canvas.height,data:Array.from(c.getImageData(0,0,canvas.width,canvas.height).data)};});
  assert.equal(jsQR(Uint8ClampedArray.from(qr.data),qr.width,qr.height).data,await publicView.locator('#join-link').getAttribute('href'));
  assert.equal(await publicView.locator('[data-join-nav]').getAttribute('href'),await publicView.locator('#join-link').getAttribute('href'));
  await publicView.close();
  const tab=await context.newPage();await tab.goto(url+'/operator');
  await action('resume');await tab.waitForFunction(()=>document.querySelector('#takeover').getAttribute('aria-pressed')==='false');
  await page.waitForFunction(()=>document.querySelector('#takeover').getAttribute('aria-pressed')==='false');
  await page.locator('#takeover').click();await tab.waitForFunction(()=>document.querySelector('#takeover').getAttribute('aria-pressed')==='true');
  await tab.reload();await tab.waitForFunction(()=>document.querySelector('#takeover').getAttribute('aria-pressed')==='true');
  assert.equal(await tab.locator('#takeover').getAttribute('aria-label'),'Release control');
  assert.equal(await page.locator('#mode, #crew-availability, #capacity, .crew-heading, #crew-empty').count(),0);
  checks.push({id:'C2_C6',passed:true,two_tabs:true});
  const retryBefore=await state();let lostRequest, responseLost=false;
  await page.route('**/api/actions',async route=>{
    lostRequest=route.request().postDataJSON();await route.fetch();await route.abort('failed');responseLost=true;await page.unroute('**/api/actions');
  });
  await page.getByRole('button',{name:'Take camera 1 live',exact:true}).click();
  await page.waitForFunction(()=>sessionStorage.getItem('breadcast-pending-action')!==null);
  await wait(async()=>responseLost,'Response was lost after server execution');
  const retryRecord=await context.request.get(url+'/api/actions/'+lostRequest.id);assert.equal(retryRecord.status(),200);
  await page.reload();await page.waitForFunction(()=>sessionStorage.getItem('breadcast-pending-action')===null);
  const retryAfter=await state();assert.equal(retryAfter.program.revision,retryBefore.program.revision+1);
  assert.equal(retryAfter.control.control_revision,retryBefore.control.control_revision+1);
  checks.push({id:'C4',passed:true,response_aborted_after_server_execution:true,action_id:lostRequest.id});
  // Preview cannot commit score or airtime. It stays in the prepared graphics boundary.
  const before=await state();await context.request.post(url+'/api/graphics/preview',{data:{op:'cue',preset:'score-wide',score:{home:'Preview',home_score:9}}});
  const after=await state();assert.equal(after.program.revision,before.program.revision);assert.equal(after.program.graphics.score.home_score,null);
  const spoof=await context.request.post(url+'/api/actions',{data:{id:'spoof-web',op:'graphics',args:{graphics:{op:'score',score:{confirmed:true,home_score:9}}},origin:'crew'}});assert.equal(spoof.status(),409);
  checks.push({id:'G1',passed:true});
  const viewer=await context.newPage();await viewer.goto(url+'/');await viewer.bringToFront();await viewer.waitForFunction(()=>document.querySelector('#program').getVideoPlaybackQuality().totalVideoFrames>20);
  async function amber(){return viewer.locator('#program').evaluate(video=>{const canvas=document.createElement('canvas');canvas.width=640;canvas.height=360;const c=canvas.getContext('2d');c.drawImage(video,0,0,640,360);const data=c.getImageData(18,12,110,20).data;let n=0;for(let i=0;i<data.length;i+=4)if(data[i]>140&&data[i+1]>75&&data[i+1]<220&&data[i+2]<130&&data[i]-data[i+1]>30)n++;return n/(data.length/4);});}
  const asset=(await state()).replays.find(r=>r.eligible);assert(asset);
  await action('graphics',{graphics:{op:'clear-all'}});await action('replay',{replay_id:asset.id});await wait(async()=>{const fraction=await amber(); return fraction>.02;},'Viewer replay label',7000);
  const started=performance.now();const returned=await action('live');
  await wait(async()=>{const s=await state();return !!s.program.applied_commands[String(returned.program_revision)]&&s.program.actual==='LIVE';},'Controller return');
  const controllerObserved=performance.now()-started;
  await wait(async()=>await amber()<.005,'Viewer live label');const viewerObserved=performance.now()-started;
  const s=await state();const applied=s.program.applied_commands[String(returned.program_revision)].monotonic_s;
  // Server record contains created_at in UTC and exact monotonic encoder receipt separately.
  const receipt=await context.request.get(url+'/api/actions/'+returned.id);assert(receipt.ok());const result=await receipt.json();const controllerExact=(applied-result.created_monotonic_s)*1000;
  checks.push({id:'M3',passed:true,request_to_controller_observed_ms:controllerObserved,action_created_to_encoder_ms:controllerExact,request_to_viewer_pixels_ms:viewerObserved,controller_applied_monotonic_s:applied,ack:'encoder frame submission; viewer pixels measured separately'});
  assert(controllerObserved<1000);
  await page.setViewportSize({width:390,height:844});await page.locator('#close-drawer').click().catch(()=>{});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  const urgent=await page.locator('#takeover').boundingBox();assert(urgent.y>=0&&urgent.y+urgent.height<844);
  await feature('replays');await page.locator('#seconds').focus();await page.keyboard.press('Tab');assert(await page.evaluate(()=>document.activeElement.id==='speed'));
  await page.keyboard.press('Escape');assert.equal(await page.evaluate(()=>document.activeElement.dataset.panel),'replays');
  await page.screenshot({path:path.join(folder,'studio-390.png'),fullPage:true});checks.push({id:'UX3',passed:true});
  await page.getByRole('button',{name:'Crew',exact:true}).click();
  await camera(5).locator('.camera-info').scrollIntoViewIfNeeded();await camera(5).locator('.camera-info').click();
  await page.locator('#studio-tooltip').waitFor({state:'visible'});tip=await page.locator('#studio-tooltip').boundingBox();
  assert(tip.x>=0&&tip.x+tip.width<=390&&tip.y>=0&&tip.y+tip.height<=844);
  await page.keyboard.press('Escape');
  await camera(5).locator('.remove').click();await wait(async()=>(await state()).occupied===4&&await camera(5).count()===0,'Remove only camera 5');
  assert.equal(await camera(5).count(),0);assert.equal((await state()).program.encoder_pid,s.program.encoder_pid);
  checks.push({id:'CAMERA_REMOVE',passed:true,slot:5,mobile_tooltip_fits:true,encoder_unchanged:true});

  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(folder,'browser-report.json'),JSON.stringify({passed:true,browser:await browser.version(),checks,page_errors:errors},null,2)+'\n');
 }catch(error){await page.screenshot({path:path.join(folder,'browser-failure.png'),fullPage:true}).catch(()=>{});fs.writeFileSync(path.join(folder,'browser-report.json'),JSON.stringify({passed:false,checks,page_errors:errors,error:String(error)},null,2)+'\n');throw error;}finally{await browser.close();fs.writeFileSync(path.join(folder,'browser-done'),'done\n');}
})().catch(error=>{console.error(error);process.exit(1);});
