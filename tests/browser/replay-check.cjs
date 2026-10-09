// Real browser validation of local fixture review, encoded preview, and manual playout.
const {chromium} = require('playwright');
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const folder = path.resolve(process.argv[2] || '.runtime/multi-camera-replay');
const url = 'http://localhost:21080';
(async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true});
  const errors = [], checks = [];
  const context = await browser.newContext({viewport: {width: 1440, height: 1200}});
  const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
  const status = async () => (await context.request.get(url+'/api/status')).json();
  try {
    await page.goto(url+'/operator');
    await page.getByRole('button', {name:'Replays',exact:true}).click();
    await page.locator('#multi-camera-replay summary').first().click();
    assert(await page.locator('#replay-plan').isVisible());
    checks.push('multi_camera_controls_accessible_under_advanced');
    await page.locator('#replay-plan').fill(fs.readFileSync(path.join(folder,'operator-plan.json'),'utf8'));
    assert(await page.locator('#render-plan').isDisabled());
    await page.locator('#validate-plan').click();
    await page.waitForFunction(() => !document.querySelector('#render-plan').disabled);
    assert.equal(await page.locator('#plan-shots li').count(),2);
    assert.match(await page.locator('#plan-shots').innerText(),/camera-1.*camera-2/s);
    assert.match(await page.locator('#plan-result').innerText(),/Fixture/);
    const before=await status();
    await page.locator('#render-plan').click();
    await page.waitForFunction(async ids => {
      const state=await (await fetch('/api/status')).json();
      return state.replays.some(replay=>!ids.includes(replay.id)) &&
        [...document.querySelectorAll('#replays video')].some(video=>!ids.some(id=>video.src.includes(id)));
    },before.replays.map(replay=>replay.id));
    const after=await status();
    assert.equal(after.program.replay_id,null);
    assert(after.program.frames_written>before.program.frames_written);
    checks.push('review_shows_order_intervals_speeds_reasons_and_fixture_label');
    checks.push('rendering_does_not_start_playback_and_live_frames_advance');
    const preview=page.locator('#replays video').last();
    await preview.scrollIntoViewIfNeeded();
    await preview.evaluate(video => video.play());
    await page.waitForFunction(() => {const v=[...document.querySelectorAll('#replays video')].at(-1);return v.videoWidth===640 && v.currentTime>.2;});
    assert.equal(await preview.evaluate(video=>video.muted),true);
    checks.push('encoded_preview_decodes_in_browser');
    await page.locator('#replays .replay').last().getByRole('button',{name:'Play replay'}).click();
    await page.waitForFunction(() => document.querySelector('#program-status').textContent.includes('Replay playing'));
    await page.locator('.modal-urgent [data-urgent=live]').click();
    await page.waitForFunction(() => document.querySelector('#program-status').textContent.startsWith('Live'));
    checks.push('manual_playback_and_immediate_return');
    const p=JSON.parse(fs.readFileSync(path.join(folder,'operator-plan.json')));p.shots[1].event_start_ms-=100;
    await page.locator('#replay-plan').fill(JSON.stringify(p));
    assert(await page.locator('#render-plan').isDisabled());
    await page.locator('#validate-plan').click();
    await page.waitForFunction(() => document.querySelector('#plan-result').textContent.startsWith('Rejected:'));
    assert.match(await page.locator('#plan-result').innerText(),/Continuous cuts/);
    checks.push('changed_plan_requires_review_and_invalid_cut_explains_rejection');
    await page.locator('#replay-plan').fill(fs.readFileSync(path.join(folder,'operator-plan.json'),'utf8'));
    await page.locator('#validate-plan').click();
    await page.waitForFunction(() => !document.querySelector('#render-plan').disabled);
    await page.screenshot({path:path.join(folder,'operator-replay.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    checks.push('mobile_replay_editor_fits_viewport');
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(folder,'browser-report.json'),JSON.stringify({passed:true,browser:await browser.version(),mode:'Injected synthetic encoded media; no physical phone or provider analysis',checks,page_errors:errors},null,2)+'\n');
  } finally {await browser.close();fs.writeFileSync(path.join(folder,'browser-done'),'done\n');}
})().catch(error=>{console.error(error);process.exit(1);});
