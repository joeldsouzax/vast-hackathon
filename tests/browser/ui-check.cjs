// Real browser checks for direct page access, QR decoding, loading states, and layout.
const {chromium} = require('playwright');
const jsQR = require('jsqr');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const runtime = path.resolve(process.argv[2] || path.join(__dirname, '../../.runtime/ui-review'));
const access = JSON.parse(fs.readFileSync(path.join(runtime, 'access.json')));
const executablePath = process.env.CHROME_PATH || (process.platform === 'darwin'
  ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : '/usr/bin/chromium');
(async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  const errors = [], checks = [], contexts = [];
  async function context(viewport = {width: 1440, height: 1000}) {
    const c = await browser.newContext({viewport}); contexts.push(c);
    c.on('page', p => p.on('pageerror', error => errors.push(error.message))); return c;
  }
  function record(name, extra = {}) { checks.push({name, ...extra}); console.log(name); }
  async function branding(page) {
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.locator('.brand img').evaluate(image => image.complete && image.naturalWidth > 0), true);
    assert.equal(await page.evaluate(() => /\bexperiment\b/i.test(document.body.innerText)), false);
    assert.equal(await page.evaluate(() => document.fonts.check('16px Outfit') && document.fonts.check('16px "DM Sans"')), true);
  }
  async function decode(page) {
    await page.waitForFunction(() => {const image = document.querySelector('#qr'); return !image.hidden && image.naturalWidth > 0;});
    const pixels = await page.locator('#qr').evaluate(image => {
      const canvas = document.createElement('canvas'); canvas.width = image.clientWidth; canvas.height = image.clientHeight;
      canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
      return {width: canvas.width, height: canvas.height, natural: image.naturalWidth,
        data: Array.from(canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data)};
    });
    const decoded = jsQR(Uint8ClampedArray.from(pixels.data), pixels.width, pixels.height);
    assert(decoded, 'Rendered QR must decode');
    assert.equal(decoded.data, await page.locator('#join-link').getAttribute('href'));
    return {width: pixels.width, height: pixels.height, natural_width: pixels.natural, payload_matches_join_link: true};
  }
  try {
    assert.equal(access.operator_key, undefined);
    assert.equal(access.viewer_key, undefined);
    assert.equal(new URL(access.operator_url).hash, '');
    assert.equal(new URL(access.broadcast_url).hash, '');
    const publicContext = await context(), publicPage = await publicContext.newPage();
    await publicContext.addInitScript(() => {
      if (!['http:', 'https:'].includes(location.protocol)) return;
      sessionStorage.setItem('breadcast-operator-key', 'old-key');
      sessionStorage.setItem('breadcast-viewer-key', 'old-key');
    });
    await publicPage.goto(access.public_url + '/#old-key');
    await decode(publicPage);
    await publicPage.waitForFunction(() => document.querySelector('#program').videoWidth === 640);
    await branding(publicPage);
    assert.equal(await publicPage.locator('input[type=password], #login').count(), 0);
    assert.equal(new URL(publicPage.url()).hash, '');
    assert.deepEqual(await publicPage.evaluate(() => [sessionStorage.getItem('breadcast-operator-key'), sessionStorage.getItem('breadcast-viewer-key')]), [null, null]);
    for (const endpoint of ['/api/viewer', '/api/qr', '/api/status', '/api/preview/program']) {
      assert.equal((await publicContext.request.get(access.public_url + endpoint)).status(), 200);
    }
    await publicPage.getByRole('link', {name: 'Studio', exact: true}).click();
    await publicPage.waitForFunction(() => document.querySelector('#program').videoWidth === 640);
    await branding(publicPage);
    assert.equal(await publicPage.locator('#workspace').isVisible(), true);
    assert.equal(await publicPage.locator('#end').isVisible(), false);
    assert.equal(await publicPage.locator('input[type=password], #login').count(), 0);
    assert.equal(await publicPage.locator('#camera-empty').isVisible(), true);
    assert.equal(await publicPage.locator('#render').isDisabled(), true);
    const current = await (await publicContext.request.get(access.public_url + '/api/status')).json();
    assert.equal((await publicContext.request.post(access.public_url + '/api/program', {data: {action: 'holding', revision: current.program.revision, expected:{run_id:current.control.run_id,context_revision:current.control.context_revision,control_revision:current.control.control_revision,program_revision:current.program.revision,sources:[]}}})).status(), 200);
    await publicPage.screenshot({path: path.join(runtime, 'operator-fixed.png'), fullPage: true});
    record('both_pages_and_operator_api_open_without_keys');
    record('old_access_fragments_and_saved_keys_are_removed');
    const viewerContext = await context(), viewer = await viewerContext.newPage();
    await viewer.goto(access.broadcast_url);
    const qr = await decode(viewer);
    await viewer.waitForFunction(() => document.querySelector('#program').getVideoPlaybackQuality().totalVideoFrames > 30);
    assert.equal(await viewer.locator('#broadcast .program-actions, #broadcast .asset-tabs, input[type=password], #login').count(), 0);
    assert.equal((await viewerContext.request.post(access.public_url + '/media/camera/' + 'a'.repeat(32) + '/whep', {data: ''})).status(), 403);
    assert.equal((await viewerContext.request.post(access.public_url + '/media/program/whip', {data: ''})).status(), 403);
    await viewer.screenshot({path: path.join(runtime, 'broadcast-fixed.png'), fullPage: true});
    record('broadcast_has_video_and_decodable_qr_only', qr);
    await viewer.getByRole('link', {name: 'Studio', exact: true}).click();
    assert.equal(await viewer.locator('#workspace').isVisible(), true);
    assert.equal(await viewer.locator('#qr').count(), 0);
    await viewer.getByRole('link', {name: 'Broadcast', exact: true}).click();
    await viewer.locator('#broadcast').waitFor({state: 'visible'});
    await decode(viewer);
    record('tabs_switch_pages_without_login');
    for (const [width, height] of [[1440,900],[1280,800]]) {
      await viewer.setViewportSize({width,height});await publicPage.setViewportSize({width,height});
      const nav = async page => page.locator('.page-tabs').evaluate(el => {const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};});
      assert.deepEqual(await nav(viewer),await nav(publicPage));
      for (const page of [viewer, publicPage]) {
        const frame=await page.locator('.program-panel').boundingBox(), video=await page.locator('#program').boundingBox();
        assert(Math.abs(frame.width-video.width)<1&&Math.abs(frame.height-video.height)<1);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight),true);
      }
    }
    assert.equal(await publicPage.locator('#mode, #capacity, #crew-availability, .crew-heading, #crew-empty').count(),0);
    assert.equal(await publicPage.locator('#crew-actions').innerText(),'');
    const joinPage=await publicContext.newPage();await joinPage.goto(access.public_url+'/join');
    await joinPage.waitForFunction(()=>new URL(location.href).searchParams.has('code'));
    assert.equal(new URL(joinPage.url()).searchParams.get('code'),new URL(await viewer.locator('#join-link').getAttribute('href')).searchParams.get('code'));
    assert.equal(await joinPage.locator('.page-tabs [aria-current="page"]').innerText(),'Join');
    assert.equal(await joinPage.locator('#join').isEnabled(),true);
    assert.equal(await joinPage.locator('#preview-container').isVisible(),false);
    assert.equal(await joinPage.locator('.program-panel').count(),0);
    await joinPage.screenshot({path:path.join(runtime,'join-fixed.png')});await joinPage.close();
    record('shared_navigation_layout_clean_chat_and_direct_join');
    const snapshot=await (await publicContext.request.get(access.public_url+'/api/status')).json();
    const expected={run_id:snapshot.control.run_id,context_revision:snapshot.control.context_revision,control_revision:snapshot.control.control_revision,program_revision:snapshot.program.revision,sources:[]};
    const action=async(op,id,stamp=expected)=>(await (await publicContext.request.post(access.public_url+'/api/actions',{data:{id,op,args:{},expected:stamp}})).json());
    assert.equal((await action('resume','release-http')).state,'Finished');
    const released=await (await publicContext.request.get(access.public_url+'/api/status')).json();
    assert.equal(released.control.crew_paused,false);assert.equal(released.control.mode,undefined);
    const delayedRelease={...expected,control_revision:released.control.control_revision};
    assert.equal((await action('takeover','urgent-http',expected)).state,'Finished');
    assert.equal((await action('resume','late-release-http',delayedRelease)).state,'Rejected');
    const unknown=await (await publicContext.request.post(access.public_url+'/api/chat',{data:{id:'unsupported-http',text:'Replay that goal',run_id:expected.run_id}})).json();
    assert.equal(unknown.state,'Rejected');assert(!unknown.reason.includes('not connected'));
    const unsafeRelease=await (await publicContext.request.post(access.public_url+'/api/chat',{data:{id:'unsafe-release-http',text:'/resume',run_id:expected.run_id}})).json();
    assert.equal(unsafeRelease.state,'Rejected');
    const held=await (await publicContext.request.get(access.public_url+'/api/status')).json();
    assert.equal(held.control.crew_paused,true);assert.equal(held.program.revision,snapshot.program.revision);
    record('http_urgent_takeover_delayed_release_and_specific_unsupported_command');
    await viewer.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await viewer.locator('.scene-orbit').evaluate(el=>getComputedStyle(el).animationName),'none');
    assert.equal(await viewer.locator('.page-reveal').first().evaluate(el=>getComputedStyle(el).animationName),'none');
    record('decorative_motion_respects_reduced_motion');
    const previous = await viewer.locator('#join-link').getAttribute('href');
    await publicPage.getByRole('button',{name:'Event details',exact:true}).click();
    await publicPage.getByText('Camera join code',{exact:true}).click();
    await publicPage.getByRole('button', {name: 'Replace join code'}).click();
    await publicPage.keyboard.press('Escape');
    await viewer.waitForFunction(old => document.querySelector('#join-link').href !== old, previous);
    await decode(viewer);
    const oldCode = new URL(previous).searchParams.get('code');
    assert.equal((await publicContext.request.post(access.public_url + '/api/leases', {data: {code: oldCode, client: 'expired-qr-fixture'}})).status(), 403);
    record('operator_rotation_updates_broadcast_qr_and_revokes_old_join_code');
    const mobileContext = await context({width: 390, height: 844}), mobile = await mobileContext.newPage();
    await mobile.goto(access.broadcast_url);
    const mobileQR = await decode(mobile);
    await mobile.waitForFunction(() => document.querySelector('#program').getVideoPlaybackQuality().totalVideoFrames > 15);
    await branding(mobile);
    assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await mobile.screenshot({path: path.join(runtime, 'broadcast-mobile.png'), fullPage: true});
    await mobile.goto(access.operator_url);
    await mobile.locator('#workspace').waitFor({state: 'visible'});
    await mobile.waitForFunction(() => document.querySelector('#program').getVideoPlaybackQuality().totalVideoFrames > 15);
    assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await mobile.screenshot({path: path.join(runtime, 'operator-mobile.png'), fullPage: true});
    record('mobile_pages_fit_viewport_and_qr_decodes', mobileQR);
    const retryContext = await context(), retry = await retryContext.newPage();
    await retry.route('**/api/qr', route => route.fulfill({status: 503, body: ''}));
    await retry.goto(access.broadcast_url);
    await retry.getByText('Join code unavailable. Retrying…', {exact: true}).waitFor();
    assert.equal(await retry.locator('#login').isVisible(), false);
    assert.equal(await retry.locator('#qr').isVisible(), false);
    await retry.unroute('**/api/qr');
    await decode(retry);
    record('qr_failure_shows_retry_state_and_recovers');
    await retry.goto(await viewer.locator('#join-link').getAttribute('href'));
    await branding(retry);
    await retry.screenshot({path: path.join(runtime, 'join-branded.png'), fullPage: true});
    record('brand_assets_and_fonts_load_without_experiment_labels');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(runtime, 'ui-report.json'), JSON.stringify({passed: true, browser: await browser.version(),
      mode: 'Local Docker browser UI without viewer/operator keys; no physical phone scan', checks, page_errors: errors}, null, 2) + '\n');
  } finally { await browser.close(); }
})().catch(error => {console.error(error.message); process.exit(1);});
