// Real WebRTC pixels and Operator controls. Input is five labeled sample publishers.
const {chromium} = require('playwright');
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const runtime = path.resolve(process.argv[2] || path.join(__dirname, '../../.runtime/graphics-review'));
const access = JSON.parse(fs.readFileSync(path.join(runtime, 'access.json')));
const executablePath = process.env.CHROME_PATH || (process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : '/usr/bin/chromium');
(async () => {
  const browser = await chromium.launch({executablePath, headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const checks = [], errors = [];
  context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
  const url = access.public_url;
  let operator;
  async function feature(name) {
    const scope=await operator.locator('#asset-drawer').evaluate(d=>d.open)?'.modal-feature-tabs':'.program-toolbar';
    await operator.locator(name==='settings'&&scope==='.program-toolbar'?'[data-panel="settings"]':`${scope} [data-panel="${name}"]`).click();
  }
  async function state() { return (await context.request.get(url + '/api/status')).json(); }
  async function wait(predicate, name, timeout = 20000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) { if (await predicate()) return; await new Promise(resolve => setTimeout(resolve, 100)); }
    throw new Error(`Timed out: ${name}`);
  }
  function record(name, detail = {}) { checks.push({name, ...detail}); console.log(name); }
  async function command(action, extra = {}) {
    const current = await state();
    const slots=new Set([extra.slot||current.program.primary_slot]);
    const asset=current.replays.find(r=>r.id===extra.replay_id);if(asset)asset.validation.plan.shots.forEach(s=>slots.add(Number(s.source_id.replace('camera-',''))));
    const expected={run_id:current.control.run_id,context_revision:current.control.context_revision,control_revision:current.control.control_revision,program_revision:current.program.revision,sources:[...slots].sort().flatMap(slot=>{const c=current.cameras.find(c=>c.slot===slot);return c?[{slot,source_path:c.source_path,epoch:c.epoch}]:[];})};
    const response = await context.request.post(url + '/api/program', {data: {action, revision: current.program.revision, expected, ...extra}});
    assert.equal(response.status(), 200, await response.text());
    const accepted = await response.json();
    await wait(async () => (await state()).program.applied_revision === accepted.revision, `Applied ${action}`);
    return accepted;
  }
  async function pixels(viewer, rectangle, color, tolerance = 15) {
    return viewer.locator('#program').evaluate((video, {rectangle: [x, y, w, h], color, tolerance}) => {
      const canvas = document.createElement('canvas'); canvas.width = video.videoWidth; canvas.height = video.videoHeight;
      const ctx = canvas.getContext('2d'); ctx.drawImage(video, 0, 0);
      const data = ctx.getImageData(x, y, w, h).data; let matches = 0;
      for (let i = 0; i < data.length; i += 4) if (color.every((c, n) => Math.abs(c-data[i+n]) < tolerance)) matches++;
      return matches/(w*h);
    }, {rectangle, color, tolerance});
  }
  async function saveVideoFrame(viewer, name) {
    const bytes = await viewer.locator('#program').evaluate(video => {
      const c = document.createElement('canvas'); c.width = video.videoWidth; c.height = video.videoHeight;
      c.getContext('2d').drawImage(video, 0, 0); return c.toDataURL('image/png').split(',')[1];
    });
    fs.writeFileSync(path.join(runtime, name), Buffer.from(bytes, 'base64'));
  }
  try {
    const initial = await state(); assert.equal(initial.occupied, 5);
    assert(initial.cameras.every(c => c.buffer_ready));
    assert.equal(initial.program.graphics.score.home_score, null); assert.equal(initial.program.graphics.score.authority, null);
    const pid = initial.program.encoder_pid;
    await command('live', {slot: 1, independent: true});
    operator = await context.newPage(); await operator.goto(url + '/operator');
    await operator.waitForFunction(() => document.querySelectorAll('.graphic-tile').length === 24);
    const viewer = await context.newPage(); await viewer.goto(url);
    await viewer.waitForFunction(() => document.querySelector('#program').getVideoPlaybackQuality().totalVideoFrames > 30);
    assert.equal(await operator.locator('#qr').count(), 0);
    record('catalog_contains_24_prepared_designs_and_unknown_initial_scores');
    const revision = (await state()).program.revision;
    for (const score of [{home_score: 3}, {confirmed: true, home_score: true}]) {
      const response = await context.request.post(url + '/api/program', {data: {action: 'graphics', revision, expected:{run_id:initial.control.run_id,context_revision:initial.control.context_revision,control_revision:initial.control.control_revision,program_revision:revision,sources:initial.cameras.filter(c=>c.slot===1).map(c=>({slot:c.slot,source_path:c.source_path,epoch:c.epoch}))}, graphics: {op: 'score', score}}});
      assert.equal(response.status(), 409);
    }
    const preview = await context.request.post(url + '/api/graphics/preview', {data: {op: 'cue', preset: 'score-wide', score: {home: 'PREVIEW ONLY', home_score: 9}}});
    assert.equal(preview.status(), 200); assert.equal((await state()).program.revision, revision);
    assert.equal((await state()).program.graphics.score.home_score, null);
    for (const data of [
      {op: 'cue', preset: 'caption', title: ('José · '+ 'W'.repeat(120)).slice(0, 120)},
      {op: 'cue', preset: 'score-wide', score: {home: 'W'.repeat(24), away: 'Å'.repeat(24), home_score: 999}},
    ]) {
      const response = await context.request.post(url + '/api/graphics/preview', {data});
      assert.equal(response.status(), 200);
      const bytes = await response.body(); assert.equal(bytes.readUInt32BE(16), 640); assert.equal(bytes.readUInt32BE(20), 360);
    }
    record('preview_and_invalid_updates_cannot_commit_scores');
    record('long_text_non_ascii_names_and_unknown_values_render_at_program_size');
    await feature('graphics');
    await operator.locator('.graphic-tile').filter({hasText: 'Classic name bar'}).click();
    await operator.locator('#graphic-title').fill('José · A fresh perspective');
    await operator.locator('#graphic-subtitle').fill('Operator control check');
    const start = performance.now();
    await feature('graphics');
    await operator.locator('#graphic-show').click();
    await wait(async () => (await state()).program.graphics.applied.visible.some(c => c.preset === 'lower-classic'), 'Name bar on encoded program');
    await wait(async () => await pixels(viewer, [355, 304, 70, 14], [69, 88, 63]) > .9, 'Name bar reaches viewer');
    await saveVideoFrame(viewer, 'graphic-name-bar.png');
    record('edited_name_bar_reaches_actual_webrtc_pixels', {request_to_observed_pixels_ms: Math.round(performance.now()-start)});
    await operator.getByRole('button', {name: 'Clear Classic name bar', exact: true}).click();
    await wait(async () => !(await state()).program.graphics.applied.visible.length, 'Clear name bar');
    await operator.locator('.graphic-tile').filter({hasText: 'Compact scoreboard'}).click();
    await feature('settings');
    await operator.locator('#score-controls').evaluate(details=>{details.open=true;});
    await operator.locator('#score-home').fill('TEST A'); await operator.locator('#score-away').fill('TEST B');
    await operator.locator('#score-home-value').fill('2'); await operator.locator('#score-away-value').fill('1');
    await operator.locator('#score-period').fill('TEST ROUND'); await operator.locator('#score-clock-value').fill('30');
    await operator.locator('#score-clock-running').check(); await operator.locator('#score-confirmed').check();
    await operator.getByRole('button', {name: 'Update score'}).click();
    await wait(async () => (await state()).program.graphics.score.home_score === 2, 'Official score saved');
    await feature('graphics');
    await operator.locator('#graphic-show').click();
    await wait(async () => (await state()).program.graphics.applied.visible.some(c => c.slot === 'score'), 'Scoreboard visible');
    await wait(async () => await pixels(viewer, [50, 91, 90, 10], [41, 45, 37]) > .9, 'Scoreboard reaches viewer');
    await wait(async () => (await state()).program.graphics.applied.clock !== '00:30', 'Display clock advances');
    const clockBefore = (await state()).program.graphics.applied.clock;
    await feature('settings');
    await operator.locator('#score-home-value').fill('3'); await operator.locator('#score-confirmed').check();
    await operator.getByRole('button', {name: 'Update score'}).click();
    await wait(async () => (await state()).program.graphics.score.home_score === 3, 'Score-only update');
    assert.equal((await state()).program.graphics.score.clock_running, true);
    const clockAfter = (await state()).program.graphics.applied.clock;
    assert(clockAfter >= clockBefore, 'A score-only edit must not reset the running display clock');
    record('score_only_updates_preserve_running_clock');
    await operator.locator('#score-clock-running').uncheck();
    await operator.locator('#score-clock-value').fill('33'); await operator.locator('#score-confirmed').check();
    await operator.getByRole('button', {name: 'Update score'}).click();
    await wait(async () => (await state()).program.graphics.score.clock_running === false, 'Clock paused');
    await new Promise(resolve => setTimeout(resolve, 1600));
    await saveVideoFrame(viewer, 'graphic-scoreboard.png');
    assert.equal((await state()).program.graphics.applied.clock, '00:33');
    record('operator_confirms_scores_and_starts_stops_display_clock');
    await feature('graphics');
    await operator.locator('.graphic-tile').filter({hasText: 'Breadcast corner logo'}).click();
    await feature('graphics');
    await operator.locator('#graphic-show').click();
    await wait(async () => (await state()).program.graphics.applied.visible.some(c => c.slot === 'bug'), 'Corner logo');
    await feature('replays');
    await operator.locator('#seconds').selectOption('2'); await operator.locator('#speed').selectOption('0.5');
    await operator.getByRole('button', {name: 'Prepare replay', exact: true}).click();
    await operator.locator('#replays .replay').last().getByRole('button', {name: 'Play replay', exact: true}).waitFor();
    await operator.locator('#replays .replay').last().getByRole('button', {name: 'Play replay', exact: true}).click();
    await wait(async () => (await state()).program.actual === 'REPLAY', 'Replay begins');
    const replay = (await state()).program.graphics.applied;
    assert.equal(replay.score_hidden_during_replay, true); assert(replay.visible.every(c => c.slot === 'bug'));
    await wait(async () => await pixels(viewer, [50, 91, 90, 10], [41, 45, 37]) < .5, 'Current scoreboard gone from replay pixels');
    await wait(async () => await pixels(viewer, [18, 12, 110, 20], [239, 168, 69], 65) > .02, 'Replay marker reaches viewer');
    await saveVideoFrame(viewer, 'graphic-replay.png');
    await wait(async () => (await state()).program.actual === 'LIVE', 'Automatic return');
    await wait(async () => (await state()).program.graphics.applied.visible.some(c => c.slot === 'score'), 'Restore score after replay');
    record('replay_keeps_marker_and_brand_hides_current_score_then_restores_live');
    await command('graphics', {graphics: {op: 'clear-all'}});
    await feature('graphics');
    const catalog = await (await context.request.get(url + '/api/graphics/catalog')).json();
    const acknowledgements = [];
    for (const spec of catalog.assets) {
      const start = performance.now();
      await command('graphics', {graphics: {op: 'cue', preset: spec.id, duration_s: spec.id === 'countdown' ? 2 : 0}});
      acknowledgements.push(Math.round(performance.now()-start));
      await wait(async () => (await state()).program.graphics.applied.visible.some(c => c.preset === spec.id), `Preset ${spec.id}`);
      if (spec.slot === 'screen') {
        await wait(async () => (await state()).program.graphics.applied.covers_camera, `Full-screen ${spec.id}`);
        assert((await state()).program.graphics.applied.visible.every(c => ['screen', 'bug', 'stinger'].includes(c.slot)));
      }
      const frames = await viewer.locator('#program').evaluate(v => v.getVideoPlaybackQuality().totalVideoFrames);
      await viewer.waitForFunction(n => document.querySelector('#program').getVideoPlaybackQuality().totalVideoFrames > n+12, frames);
      if (spec.id === 'opening') { await wait(async () => await pixels(viewer, [300, 10, 80, 30], [247, 244, 236]) > .9, 'Opening reaches viewer'); await saveVideoFrame(viewer, 'graphic-opening.png'); }
      await command('graphics', {graphics: {op: 'clear-all'}});
      await wait(async () => !(await state()).program.graphics.applied.visible.length, `Clear ${spec.id}`);
    }
    record('all_24_presets_cued_with_continuous_viewer_playback', {frames_submitted_ack_ms: acknowledgements, max_ack_ms: Math.max(...acknowledgements)});
    await command('graphics', {graphics: {op: 'cue', preset: 'countdown', duration_s: 2}});
    await wait(async () => !(await state()).program.graphics.applied.visible.length, 'Countdown expires');
    await command('graphics', {graphics: {op: 'cue', preset: 'toast-wipe'}});
    await wait(async () => !(await state()).program.graphics.applied.visible.length, 'Stinger expires');
    await command('graphics', {graphics: {op: 'cue', preset: 'opening'}});
    await command('live');
    assert(!(await state()).program.graphics.requested.some(c => ['screen', 'stinger'].includes(c.slot)));
    record('countdown_and_stinger_expire_return_to_live_clears_full_screen');
    const mobileContext = await browser.newContext({viewport: {width: 390, height: 844}});
    const mobile = await mobileContext.newPage(); mobile.on('pageerror', e => errors.push(e.message));
    await mobile.goto(url + '/operator');
    await mobile.waitForFunction(() => document.querySelectorAll('.graphic-tile').length === 24);
    await mobile.getByRole('button',{name:'Graphics',exact:true}).click();
    await mobile.locator('.graphic-tile').filter({hasText: 'Wide scoreboard'}).click();
    assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await mobile.screenshot({path: path.join(runtime, 'graphics-mobile.png'), fullPage: true});
    await operator.reload(); await operator.waitForFunction(() => document.querySelectorAll('.graphic-tile').length === 24);
    await operator.screenshot({path: path.join(runtime, 'graphics-operator.png'), fullPage: true});
    assert.equal((await state()).program.encoder_pid, pid);
    assert.equal((await state()).program.graphics.error, null); assert.equal((await state()).occupied, 5);
    assert.deepEqual(errors, []);
    record('five_cameras_remain_active_no_encoder_restart_no_page_errors_mobile_fits');
    fs.writeFileSync(path.join(runtime, 'graphics-browser-report.json'), JSON.stringify({passed: true, browser: await browser.version(),
      mode: 'Five labeled sample publishers; actual WebRTC viewer pixels; TEST scores are fixtures', checks, page_errors: errors}, null, 2)+'\n');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exit(1);});
