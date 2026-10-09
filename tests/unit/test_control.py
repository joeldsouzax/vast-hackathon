"""Authority, retries, delayed commits, source ownership, and exact command grammar."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from studio import App, Config
from types import SimpleNamespace


class ControlContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = App(Config(Path(self.directory.name), 'http://localhost'))
        self.c = self.app.control
        self.app.sources[1] = SimpleNamespace(slot=1, path='camera/lease-one', epoch=1, has_audio=True, at=lambda _: object())
        self.n = 0

    def tearDown(self):
        self.app.stop.set()
        if self.c.thread.is_alive(): self.c.thread.join(2)
        self.app.foundation.close()
        self.app.leases.db.close()
        self.directory.cleanup()

    def human(self, op, **args):
        self.n += 1
        return self.c.submit({'id': f'h{self.n}', 'op': op, 'args': args})

    def proposal(self, op='live', **args):
        self.n += 1
        return {'id': f'p{self.n}', 'op': op, 'args': args, 'expected': self.c.expected(args), 'expires_at': time.time()+30}

    def test_retry_and_changed_id_contents(self):
        request = {'id': 'once', 'op': 'live', 'args': {'slot': 1}}
        first = self.c.submit(request)
        self.assertEqual(first['state'], 'Applying')
        self.assertEqual(self.c.submit(request)['id'], first['id'])
        self.assertEqual(self.app.program.revision, 1)
        with self.assertRaisesRegex(ValueError, 'changed contents'):
            self.c.submit({**request, 'args': {'slot': 2}})
        self.assertEqual(self.app.program.revision, 1)

    def test_E01_human_score_snapshot_survives_its_own_takeover(self):
        args={'graphics':{'op':'score','score':{'confirmed':True,'home_score':2}}}
        request={'id':'score-once','op':'graphics','args':args,'expected':self.c.expected(args)}
        result=self.c.submit(request)
        self.assertEqual(result['state'],'Applying',result)
        self.assertEqual(self.app.foundation.context_revision,2)
        self.assertEqual(self.app.program.graphics.score['home_score'],2)
        self.c.submit(request)
        self.assertEqual(self.app.foundation.context_revision,2)

    def test_direct_invalid_target_takes_over_and_preserves_picture(self):
        self.human('resume')
        proposal = self.c.propose(self.proposal(slot=1))
        result = self.human('live', slot=2)
        self.assertEqual(result['state'], 'Rejected')
        self.assertTrue(result['takeover'])
        self.assertTrue(self.c.crew_paused)
        self.assertEqual(self.c.actions[proposal['id']]['state'], 'Canceled')
        self.assertEqual(self.app.program.requested, 'HOLDING')

    def test_delayed_proposal_after_takeover_and_release(self):
        self.human('resume')
        delayed = self.proposal(slot=1)
        self.human('takeover')
        self.human('resume')
        self.assertEqual(self.c.propose(delayed)['state'], 'Rejected')
        self.assertEqual(self.app.program.revision, 0)

    def test_crew_enabled_by_default_and_explicit_takeover_release(self):
        self.assertFalse(self.c.snapshot()['crew_paused'])
        self.assertNotIn('mode', self.c.snapshot())
        p = self.c.propose(self.proposal(slot=1))
        self.assertEqual(p['state'], 'Scheduled')
        self.human('takeover')
        self.assertTrue(self.c.crew_paused)
        self.assertEqual(self.c.actions[p['id']]['state'], 'Canceled')
        self.assertEqual(self.c.propose(self.proposal(slot=1))['state'], 'Rejected')
        self.human('resume')
        self.assertFalse(self.c.crew_paused)
        self.assertEqual(self.c.propose(self.proposal(slot=1))['state'], 'Scheduled')
        for op, args in [('mode', {'mode': 'Assisted'}), ('approve', {'proposal_id': p['id'], 'signature': p['signature']})]:
            self.assertEqual(self.human(op, **args)['state'], 'Rejected')
        self.assertEqual(self.app.program.revision, 0)

    def test_chat_cannot_release_without_a_reviewed_control_revision(self):
        self.human('takeover')
        revision = self.c.revision
        result = self.c.chat('unsafe-release', '/resume', self.c.run_id)
        self.assertEqual(result['state'], 'Rejected')
        self.assertTrue(self.c.crew_paused)
        self.assertEqual(self.c.revision, revision)

    def test_takeover_remains_urgent_with_an_old_control_revision(self):
        expected = self.c.expected({})
        self.human('resume')
        result = self.c.submit({'id': 'urgent-takeover', 'op': 'takeover', 'args': {}, 'expected': expected})
        self.assertEqual(result['state'], 'Finished')
        self.assertTrue(self.c.crew_paused)
        self.assertEqual(self.app.program.revision, 0)

    def test_default_crew_commits_without_mode_or_approval(self):
        proposal = self.c.propose(self.proposal(slot=1))
        self.c.start()
        deadline = time.monotonic()+2
        while self.c.actions[proposal['id']]['state'] == 'Scheduled' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.c.actions[proposal['id']]['state'], 'Applying')
        self.assertEqual(self.app.program.revision, 1)
        self.assertFalse(self.c.crew_paused)

    def test_pause_keeps_explicit_preparation_and_never_grants_airtime(self):
        self.human('takeover')
        with patch.object(self.app, 'render', return_value={'id': 'human-job', 'state': 'rendering'}):
            self.assertEqual(self.c.propose(self.proposal('prepare', slot=1))['state'], 'Rejected')
            self.assertEqual(self.human('prepare', slot=1)['state'], 'Preparing')
        self.assertTrue(self.c.crew_paused)
        self.assertEqual(self.app.program.revision, 0)

    def test_expired_stale_program_and_reused_source_are_rejected(self):
        self.human('resume')
        for mutation in ('expiry', 'source', 'program'):
            with self.subTest(mutation=mutation):
                p = self.proposal(slot=1)
                if mutation == 'expiry': p['expires_at'] = time.time()-1
                elif mutation == 'source': self.app.sources[1].path += '-new'
                else: self.app.program.revision += 1
                self.assertEqual(self.c.propose(p)['state'], 'Rejected')

    def test_crew_cannot_claim_human_or_confirm_official_facts(self):
        with self.assertRaisesRegex(ValueError, 'actor and approval'):
            self.c.submit({'id':'spoof','op':'live','args':{'slot':1},'origin':'crew'})
        self.human('resume')
        p = self.c.propose(self.proposal('graphics', graphics={'op':'score','score':{'confirmed':True,'home_score':9}}))
        self.assertEqual(p['state'],'Rejected')
        self.assertIsNone(self.app.program.graphics.score['home_score'])
        for op in ('resume','policy','takeover','rehearsal','mode','approve'):
            self.assertEqual(self.c.propose(self.proposal(op))['state'],'Rejected')

    def test_unrecognized_chat_does_not_take_over_or_invent(self):
        self.human('resume')
        for i, text in enumerate(('Replay that goal', "Show the speaker's name", '/camera two', 'use fewer replays', '/graphic lower-classic')):
            result = self.c.chat(f'chat{i}', text)
            self.assertEqual(result['state'], 'Rejected')
            self.assertEqual(result['input_text'], text)
            self.assertNotIn('not connected', result['reason'])
            self.assertFalse(self.c.crew_paused)
            self.assertEqual(self.app.program.revision,0)
        self.assertEqual(self.c.chat('stay','/stay 1')['state'],'Applying')
        self.assertEqual(next(a for a in self.c.snapshot()['actions'] if a['id'] == 'stay')['input_text'], '/stay 1')
        self.assertTrue(self.c.crew_paused)

    def test_old_cancel_never_cancels_new_job(self):
        self.app.jobs = {'old': {'state':'ready'}, 'new': {'state':'rendering'}}
        result = self.human('cancel', job_id='old')
        self.assertEqual(result['state'], 'Rejected')
        self.assertFalse(self.app.render_cancel.is_set())
        self.assertEqual(self.human('cancel', job_id='new')['state'], 'Finished')
        self.assertTrue(self.app.render_cancel.is_set())

    def test_text_binding_after_takeover_cannot_commit(self):
        entered, release = threading.Event(), threading.Event()
        original = self.app.program.graphics.prepare
        def delayed(data):
            entered.set(); release.wait(3); return original(data)
        result = []
        with patch.object(self.app.program.graphics,'prepare', delayed):
            worker = threading.Thread(target=lambda: result.append(self.human('graphics', graphics={'op':'cue','preset':'corner-label'})))
            worker.start(); self.assertTrue(entered.wait(2))
            self.human('takeover'); release.set(); worker.join(3)
        self.assertEqual(result[0]['state'],'Rejected')
        self.assertEqual(self.app.program.revision,0)
        self.assertEqual(self.app.program.graphics.active,{})

    def test_concurrent_duplicate_commits_once(self):
        request = {'id':'race','op':'live','args':{'slot':1}}
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.c.submit(copy.deepcopy(request)))) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertEqual(len(results),8)
        self.assertEqual(self.app.program.revision,1)
        self.assertEqual(len(self.c.actions),1)

    def test_suppressed_graphic_is_not_reported_on_air(self):
        action = self.human('graphics', graphics={'op':'cue','preset':'lower-classic','title':'Supplied'})
        self.app.program.applied_commands['1'] = {'monotonic_s': time.monotonic(),'target':{'kind':'replay'}}
        self.assertEqual(self.c.snapshot()['actions'][-1]['state'],'Applying')
        self.app.program.graphics_applied['visible'] = self.app.program.graphics.public_state()['requested']
        self.assertEqual(self.c.snapshot()['actions'][-1]['state'],'On air')

    def test_bad_revision_takes_over_and_pending_limit_keeps_human_priority(self):
        self.human('resume')
        for _ in range(32):
            self.assertEqual(self.c.propose(self.proposal(slot=1))['state'],'Scheduled')
        with self.assertRaisesRegex(ValueError,'32-action'):
            self.c.propose(self.proposal(slot=1))
        with self.assertRaisesRegex(ValueError,'integers'):
            self.app.human_action({'action':'live','slot':1,'revision':0.0})
        self.assertTrue(self.c.crew_paused)
        self.assertEqual(self.app.program.revision,0)

    def test_policy_defaults_preparation_and_restart(self):
        self.assertEqual(self.c.policy['minimum_shot_s'],5)
        self.assertEqual(self.c.policy['replay_max_s'],12)
        self.assertEqual(self.c.policy['replay_cooldown_s'],30)
        self.human('resume')
        self.c.last_shot = time.monotonic()
        proposal = self.c.propose(self.proposal(slot=1))
        self.assertGreater(proposal['not_before'],time.monotonic()+4)
        old_run = self.c.run_id
        from control import Coordinator
        fresh = Coordinator(self.app)
        self.assertNotEqual(fresh.run_id,old_run)
        self.assertFalse(fresh.crew_paused); self.assertEqual(fresh.actions,{})

    def test_legacy_route_uses_takeover_and_revision(self):
        self.human('resume')
        with self.assertRaisesRegex(ValueError,'revision changed'):
            self.app.human_action({'action':'live','slot':1,'revision':12})
        self.assertTrue(self.c.crew_paused)
        self.assertEqual(self.app.program.revision,0)
        self.assertEqual(self.app.human_action({'action':'live','slot':1,'revision':0})['revision'],1)

    def test_each_play_of_same_asset_has_its_own_applied_session(self):
        path = Path(self.directory.name)/'unit-readiness.mp4'; path.write_bytes(b'unit fixture')
        self.app.replays['same'] = SimpleNamespace(id='same', frames=[b'unit frame'],path=path,
            report={'decode_passed':True,'plan':{'timing_mode':'source_only','shots':[{'source_id':'camera-1'}]},
                    'source_map':[{'source_id':'camera-1','source_epoch':1,'retained_media':{'source_path':'camera/lease-one'}}]})
        first = self.human('replay',replay_id='same')
        self.app.program.actual = 'REPLAY'
        self.app.program.actual_target = {'kind':'replay','id':'same','command_revision':1}
        self.app.program.applied_commands['1'] = {'monotonic_s':time.monotonic(),'target':self.app.program.actual_target.copy()}
        self.c.snapshot()
        second = self.human('replay',replay_id='same')
        self.app.program.actual_target['command_revision'] = 2
        self.app.program.applied_commands['2'] = {'monotonic_s':time.monotonic(),'target':self.app.program.actual_target.copy()}
        self.c.snapshot()
        self.assertEqual(self.c.actions[first['id']]['state'],'Finished')
        self.assertEqual(self.c.actions[second['id']]['state'],'On air')

    def test_prior_run_airtime_request_has_no_takeover_effect(self):
        self.human('resume')
        expected = self.c.expected({'slot':1}); expected['run_id']='old-run'
        result=self.c.submit({'id':'old-air','op':'live','args':{'slot':1},'expected':expected})
        self.assertEqual(result['state'],'Rejected');self.assertNotIn('takeover',result)
        self.assertFalse(self.c.crew_paused);self.assertEqual(self.app.program.revision,0)

    def test_delayed_human_resume_cannot_reverse_takeover(self):
        self.human('resume')
        expected = self.c.expected({})
        self.human('takeover')
        record = self.c.submit({'id':'delayed-resume','op':'resume','args':{},'expected':expected})
        self.assertEqual(record['state'],'Rejected')
        self.assertTrue(self.c.crew_paused)

    def test_live_and_audio_selection_pin_the_lease(self):
        self.human('live',slot=1)
        self.human('audio',slot=1)
        self.assertEqual(self.app.program.primary_source_path,'camera/lease-one')
        self.assertEqual(self.app.program.audio_source_path,'camera/lease-one')
        self.app.sources[1].path = 'camera/new-owner'
        self.assertEqual(self.human('live')['state'],'Rejected')
        self.assertEqual(self.app.program.primary_source_path,'camera/lease-one')
        self.assertEqual(self.human('live',slot=1,independent=True)['state'],'Applying')
        self.assertEqual(self.app.program.primary_source_path,'camera/new-owner')
        self.assertEqual(self.app.program.audio_source_path,'camera/lease-one')

    def test_wrong_applied_target_cannot_claim_on_air(self):
        action = self.human('live',slot=1)
        self.app.program.applied_commands['1'] = {'monotonic_s':time.monotonic(),'target':{'kind':'holding'}}
        self.assertEqual(self.c.snapshot()['actions'][-1]['state'],'Rejected')

    def test_microphone_mute_and_exclusive_selection_keep_source_ownership(self):
        self.app.sources[2] = SimpleNamespace(slot=2, path='camera/lease-two', epoch=1, has_audio=True, at=lambda _: object())
        self.human('audio', slot=1, muted=True)
        self.human('live', slot=2, independent=True)
        self.assertTrue(self.app.program.audio_muted)
        self.assertEqual(self.app.program.audio_source_path, 'camera/lease-one')
        self.human('audio', slot=2, muted=False)
        self.assertFalse(self.app.program.audio_muted)
        self.assertEqual(self.app.program.audio_slot, 2)
        self.assertEqual(self.app.program.audio_source_path, 'camera/lease-two')
        revision = self.app.program.revision
        self.assertEqual(self.human('audio', slot=1, muted='false')['state'], 'Rejected')
        self.assertEqual(self.app.program.revision, revision)
        self.assertEqual(self.app.program.audio_source_path, 'camera/lease-two')
        self.human('audio', slot=2, muted=True)
        self.app.sources[2].path = 'camera/replacement'
        self.assertTrue(self.app.program.audio_muted)
        self.assertEqual(self.app.program.audio_source_path, 'camera/lease-two')

    def test_source_failure_overrides_crew_minimum_shot(self):
        self.human('resume')
        self.app.program.requested = 'LIVE'; self.app.program.actual = 'HOLDING'
        self.app.sources[1].at = lambda _: None
        self.app.sources[2] = SimpleNamespace(slot=2,path='camera/healthy',epoch=1,has_audio=True,at=lambda _: object())
        self.c.last_shot = time.monotonic()
        proposal = self.c.propose(self.proposal(slot=2,independent=True))
        self.assertEqual(proposal['state'],'Scheduled')
        self.assertLessEqual(proposal['not_before'],time.monotonic())

    def test_legacy_retry_keeps_original_source_snapshot(self):
        body = {'id':'legacy-once','action':'graphics','revision':0,'graphics':{'op':'cue','preset':'corner-label'}}
        self.app.human_action(body)
        original_revision = self.app.program.revision
        self.app.sources[1].path = 'camera/replaced-after-command'
        self.app.human_action(body)
        self.assertEqual(self.app.program.revision, original_revision)
        with self.assertRaisesRegex(ValueError,'changed contents'):
            self.app.human_action({**body,'graphics':{'op':'clear-all'}})

    def test_prior_run_controls_and_chat_cannot_resume_after_restart(self):
        expected = self.c.expected({}); expected['run_id'] = 'old-run'
        for op, args in [('resume',{}), ('rehearsal',{'slot':1})]:
            self.assertEqual(self.c.submit({'id':f'old-{op}','op':op,'args':args,'expected':expected})['state'],'Rejected')
        self.assertEqual(self.c.chat('old-chat','/camera 1','old-run')['state'],'Rejected')
        self.assertFalse(self.c.crew_paused); self.assertEqual(self.app.program.revision,0)
        self.c.chat('exact-chat','/camera 1')
        with self.assertRaisesRegex(ValueError,'changed contents'):
            self.c.chat('exact-chat','/stay 1')

    def test_crew_preparation_policy_and_owned_cancellation(self):
        self.human('resume')
        self.human('policy',replays_enabled=False)
        self.assertEqual(self.c.propose(self.proposal('prepare',slot=1))['state'],'Rejected')
        self.human('policy',replays_enabled=True)
        self.app.jobs['owned'] = {'state':'rendering'}
        with patch.object(self.app,'render',return_value={'id':'owned','state':'rendering'}):
            prepare = self.c.propose(self.proposal('prepare',slot=1))
        self.assertEqual(prepare['state'],'Preparing')
        self.assertEqual(self.c.propose(self.proposal('cancel',job_id='other'))['state'],'Rejected')
        self.assertFalse(self.app.render_cancel.is_set())
        self.assertEqual(self.c.propose(self.proposal('cancel',job_id='owned'))['state'],'Finished')
        self.assertTrue(self.app.render_cancel.is_set())

    def test_malformed_crew_and_legacy_targets_are_rejected_with_history(self):
        self.human('resume')
        for op, args in [('graphics',{'graphics':None}), ('replay',{'replay_id':[]})]:
            request = {'id':f'malformed-{op}','op':op,'args':args,
                       'expected':self.c.expected({}), 'expires_at':time.time()+30}
            self.assertEqual(self.c.propose(request)['state'],'Rejected')
        with self.assertRaisesRegex(ValueError,'integer'):
            self.app.human_action({'action':'live','slot':[],'revision':0})
        self.assertTrue(self.c.crew_paused)
        self.assertEqual(self.app.program.revision,0)

    def test_source_changes_at_human_commit_are_rechecked(self):
        request = {'id':'source-race','op':'live','args':{'slot':1},'expected':self.c.expected({'slot':1})}
        original = self.c._media
        def switch_at_commit(record, prepared=None):
            self.app.sources[1].path = 'camera/new-owner'
            return original(record, prepared)
        with patch.object(self.c, '_media', switch_at_commit):
            result = self.c.submit(request)
        self.assertEqual(result['state'],'Rejected')
        self.assertTrue(self.c.crew_paused)
        self.assertEqual(self.app.program.revision,0)

    def test_applied_state_requires_exact_encoder_receipt(self):
        action = self.human('live',slot=1)
        self.assertEqual(self.c.snapshot()['actions'][-1]['state'],'Applying')
        self.app.program.applied_commands['1'] = {'monotonic_s': time.monotonic(),'target':{'kind':'camera','source_path':'camera/lease-one','epoch':1}}
        self.assertEqual(self.c.snapshot()['actions'][-1]['state'],'On air')
        self.assertEqual(self.c.snapshot()['actions'][-1]['state'],'Finished')

if __name__ == '__main__': unittest.main()
