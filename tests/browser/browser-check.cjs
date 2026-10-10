// All camera devices here are Chrome test fixtures. No physical camera is opened.
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const runtime = path.resolve(process.argv[2] || path.join(__dirname, '../../.runtime'));
const access = JSON.parse(fs.readFileSync(path.join(runtime, 'access.json')));
const executablePath = process.env.CHROME_PATH || (process.platform === 'darwin'
  ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : '/usr/bin/chromium');

(async () => {
  const browser = await chromium.launch({executablePath, headless: true, args: [
    '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
    '--autoplay-policy=no-user-gesture-required',
  ]});
  const context = await browser.newContext();
  const errors = [], phones = [], checks = [];
  context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
  async function state() {
    const response = await context.request.get(access.public_url + '/api/status');
    assert.equal(response.status(), 200); return response.json();
  }
  async function wait(check, description, timeout = 45000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 250)); }
    throw new Error(`Timed out: ${description}`);
  }
  function record(name, extra = {}) {checks.push({name, ...extra}); console.log(name);}
  try {
    assert.equal((await state()).occupied, 0, 'Start a fresh experiment without camera publishers before this browser check');
    const operator = await context.newPage();
    await operator.goto(access.operator_url);
    await operator.waitForFunction(() => document.querySelector('#program').videoWidth === 640);
    const pid = (await state()).program.encoder_pid;
    const joinUrl = (await state()).join_url;
    async function joinPhone() {
      const phoneContext = await browser.newContext({viewport: {width: 390, height: 844}});
      const phone = await phoneContext.newPage();
      phone.on('pageerror', error => errors.push(error.message));
      await phone.goto(joinUrl);
      assert.equal(await phone.locator('#preview-container').isVisible(),false);
      await phone.getByRole('button', {name: 'Join camera', exact: true}).click();
      await phone.waitForFunction(() => !document.querySelector('#stop').disabled);
      assert.equal(await phone.locator('#preview-container').isVisible(),true);
      assert.equal(await phone.locator('#microphone').isDisabled(),true);
      assert.equal(await phone.locator('#start').count(),0);
      await phone.waitForFunction(() => document.querySelector('#status').textContent.includes('Live'), {}, {timeout: 45000});
      phones.push({phone, context: phoneContext});
    }
    await joinPhone();
    await wait(async () => { const c = (await state()).cameras[0]; return c?.buffer_seconds > 5 && c.buffer_ready; }, 'first browser camera buffer');
    await operator.getByRole('button', {name: 'Take camera 1 live', exact: true}).click();
    await wait(async () => (await state()).program.actual === 'LIVE', 'browser camera on air');
    const viewer = await context.newPage(); await viewer.goto(access.broadcast_url);
    await viewer.waitForFunction(() => document.querySelector('#program').getVideoPlaybackQuality().totalVideoFrames > 60);
    const one = await viewer.locator('#program').evaluate(video => ({width: video.videoWidth, height: video.videoHeight,
      frames: video.getVideoPlaybackQuality().totalVideoFrames}));
    assert.equal(one.width, 640); assert.equal(one.height, 360);
    record('one_browser_camera_to_webrtc_viewer', one);
    await phones[0].phone.screenshot({path: path.join(runtime, 'phone.png')});
    await operator.getByRole('button',{name:'Replays',exact:true}).click();
    await operator.locator('#seconds').selectOption('2');
    await operator.locator('#speed').selectOption('0.5');
    await operator.locator('#zoom').selectOption('1.5');
    await operator.getByRole('button', {name: 'Prepare replay', exact: true}).click();
    await operator.locator('#replays').getByRole('button', {name: 'Play replay', exact: true}).and(operator.locator('button:not(:disabled)')).last().waitFor({timeout: 30000});
    await operator.locator('#replays').getByRole('button', {name: 'Play replay', exact: true}).and(operator.locator('button:not(:disabled)')).last().click();
    await wait(async () => (await state()).program.actual === 'REPLAY', 'replay scheduled');
    const before = await viewer.locator('#program').evaluate(video => video.getVideoPlaybackQuality().totalVideoFrames);
    await viewer.waitForFunction(n => document.querySelector('#program').getVideoPlaybackQuality().totalVideoFrames > n + 15, before);
    await operator.locator('.modal-urgent [data-urgent=live]').click();
    await operator.locator('#close-drawer').click();
    await wait(async () => (await state()).program.actual === 'LIVE', 'return to live');
    assert.equal((await state()).program.encoder_pid, pid);
    record('browser_replay_and_return_without_encoder_restart');
    for (let i = 1; i < 5; i++) await joinPhone();
    await wait(async () => {
      const current = await state(); return current.occupied === 5 && current.cameras.every(camera => camera.decoded_frames > 30);
    }, 'five WebRTC camera publishers');
    record('five_browser_cameras_stream_and_decode', {slots: (await state()).cameras.map(camera => camera.slot)});
    const sixth = await context.newPage(); await sixth.goto(joinUrl);
    await sixth.getByRole('button', {name: 'Join camera', exact: true}).click();
    await sixth.getByText('All five camera slots are occupied', {exact: true}).waitFor();
    assert.equal((await state()).occupied, 5);
    record('sixth_browser_camera_rejected');
    await operator.screenshot({path: path.join(runtime, 'operator.png'), fullPage: true});
    await viewer.screenshot({path: path.join(runtime, 'viewer.png')});
    for (const {phone} of phones) await phone.getByRole('button', {name: 'Stop sharing', exact: true}).click();
    await wait(async () => (await state()).occupied === 0, 'all browser leases released');
    assert.equal((await state()).program.encoder_pid, pid);
    assert.deepEqual(errors, []);
    record('browser_stop_releases_all_camera_slots');
    fs.writeFileSync(path.join(runtime, 'browser-report.json'), JSON.stringify({passed: true,
      mode: 'Chrome fake camera devices; physical phones not tested', browser: await browser.version(),
      checks, page_errors: errors}, null, 2) + '\n');
  } finally {await browser.close();}
})().catch(error => {console.error(error.message); process.exit(1);});
