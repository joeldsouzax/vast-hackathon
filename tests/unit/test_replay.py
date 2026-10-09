"""Replay ownership, time, evidence, and retention contracts."""
import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'media'))
from replay_fixture import memory_source, calibration, evidence, plan
from replay import ReplayContext
from studio import Config


class ReplayContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg = Config(Path(cls.tmp.name), 'http://localhost')
        cls.originals = {i: memory_source(cls.cfg, i) for i in range(1, 6)}

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def setUp(self):
        self.sources = dict(self.originals)
        self.context = ReplayContext(self.cfg, self.sources.get)
        for i in range(1, 5):
            self.context.calibrate(calibration(self.sources[i]))
            self.context.register_evidence(evidence(i, 'usable' if i < 3 else 'obscured'))
        self.p = plan(self.cfg)

    def rejects(self, p, reason):
        with self.assertRaisesRegex(ValueError, reason): self.context.resolve(p)

    def test_continuous_order_and_pinned_source_time(self):
        r = self.context.resolve(self.p)
        self.assertAlmostEqual(r.expected_ms, 5000)
        self.assertEqual([s['source_id'] for s in r.shots], ['camera-1', 'camera-2'])
        self.assertLessEqual(r.shots[1]['combined_alignment_bound_ms'], 150)
        self.assertEqual(r.shots[1]['output_start_ms'], 2500)
        self.assertEqual(r.shots[1]['source_start_ms'], 3500)
        self.assertEqual(len(r.pins), 2)
        r.release(); self.assertEqual(r.pins, [])

    def test_explicit_repeat_and_time_jump_rejection(self):
        r = self.context.resolve(plan(self.cfg, True)); self.assertEqual(r.expected_ms, 4000); r.release()
        self.p['shots'][1]['event_start_ms'] = 2000
        self.rejects(self.p, 'Continuous cuts')
        self.p['shots'][1]['edit'] = 'repeat'
        self.rejects(self.p, 'previously shown interval')

    def test_unknown_sync_and_reconnect(self):
        del self.context.mappings[('camera-2',1)]
        self.rejects(self.p, 'unknown')
        self.sources[2] = copy.copy(self.sources[2]); self.sources[2].epoch = 2
        self.rejects(self.p, 'reconnect')
        self.p['shots'][1]['source_epoch'] = 2; self.p['source_mapping_revisions'] = {'camera-1:1':1,'camera-2:2':1}
        self.rejects(self.p, 'unknown')

    def test_legacy_translation_pins_media_before_render(self):
        from media import compile_single_camera
        source = copy.copy(self.sources[1]); source.frames = list(source.frames)
        r = compile_single_camera(self.cfg,'legacy',source,2,.5,1.5)
        source.frames.clear()
        self.assertEqual(len(r.pins[0]),30)
        self.assertEqual(r.plan['timing_mode'],'source_only')
        self.assertIsNone(r.plan['shots'][0]['event_start_ms'])
        self.assertAlmostEqual(r.expected_ms,4000)
        r.release()

    def test_native_pts_origin_and_time_base_are_preserved(self):
        source = copy.copy(self.sources[2])
        source.frames = [replace(f, media_s=f.media_s+100, pts=f.pts+1500) for f in source.frames]
        self.sources[2] = source
        data = calibration(source)
        for marker in data['markers']: marker['event_ms'] -= 100000
        self.context.calibrate(data)
        ev = evidence(2); ev['revision'] = 2; ev['mapping_revision'] = 2
        self.context.register_evidence(ev)
        self.p['source_mapping_revisions']['camera-2:1'] = 2; self.p['evidence_revisions']['view-2'] = 2
        r = self.context.resolve(self.p)
        self.assertAlmostEqual(r.shots[1]['source_start_ms'],103500)
        self.assertEqual(r.shots[1]['retained_media']['time_base'],'1/15')
        self.assertGreater(r.shots[1]['retained_media']['pts'][0],1500)
        r.release()

    def test_documented_multi_camera_example_uses_shared_contract(self):
        p = __import__('json').loads((Path(__file__).resolve().parents[2] / 'docs/examples/replay-plan.example.json').read_text())
        p['expires_at'] = time.time()+60
        r = self.context.resolve(p)
        self.assertAlmostEqual(r.expected_ms,9000)
        self.assertEqual(r.shots[-1]['edit'],'repeat')
        r.release()

    def test_replaced_lease_does_not_inherit_mapping(self):
        source = copy.copy(self.sources[2]); source.path = 'new-lease'; self.sources[2] = source
        self.rejects(self.p, 'replaced camera lease')

    def test_segmentor_timeout_and_malformed_response(self):
        with self.assertRaisesRegex(ValueError, 'Segmentor timeout'):
            self.context.accept_segmentor_response(self.p, time.monotonic()-1)
        with self.assertRaisesRegex(ValueError, 'Malformed segmentor'):
            self.context.accept_segmentor_response('not json', time.monotonic()+1)
        r = self.context.accept_segmentor_response(self.p, time.monotonic()+1)
        r.release()

    def test_unknown_source_and_foreign_event(self):
        self.p['event_id'] = 'another-event'; self.rejects(self.p, 'event')
        self.p = plan(self.cfg); self.p['shots'][1]['source_id'] = 'camera-6'; self.rejects(self.p, 'five camera')

    def test_stale_mapping_and_drift(self):
        self.p['shots'][1]['event_end_ms'] = 8000; self.rejects(self.p, 'calibration interval')
        key = ('camera-2',1); m = self.context.mappings[key][0]
        self.context.mappings[key][0] = replace(m, uncertainty_ms=100)
        self.rejects(plan(self.cfg), 'alignment uncertainty')
        data = calibration(self.sources[2]); data['markers'][1]['event_ms'] += 400
        m = self.context.calibrate(data); self.assertGreater(m['measured_residual_ms'], 300)
        data = calibration(self.sources[2]); data['markers'][-1]['event_ms'] += 400
        with self.assertRaisesRegex(ValueError, 'drift'): self.context.calibrate(data)

    def test_mapping_revision_is_pinned(self):
        r = self.context.resolve(self.p)
        data = calibration(self.sources[2]); data['uncertainty_ms'] = 20
        self.context.calibrate(data)
        self.assertEqual(r.shots[1]['mapping_revision'], 1)
        self.assertAlmostEqual(self.context.mapping('camera-2',1,1).uncertainty_ms, 3)
        r.release()

    def test_missing_media_and_packet_join(self):
        source = copy.copy(self.sources[2]); source.frames = list(source.frames)
        source.frames.pop(70); self.sources[2] = source
        self.rejects(self.p, 'gap')
        source.frames = source.frames[:60]; self.rejects(self.p, 'not finalized')

    def test_evidence_ownership_quality_and_fixture_label(self):
        self.p['shots'][1]['evidence_ids'] = ['view-1']; self.rejects(self.p, 'Evidence source')
        self.p = plan(self.cfg); self.p['fixture'] = False; self.rejects(self.p, 'unlabeled fixture')
        self.context.evidence['view-2']['quality'] = 'obscured'; self.rejects(plan(self.cfg), 'obscured')

    def test_evidence_coverage_freshness_and_retraction(self):
        self.context.evidence['view-2']['event_start_ms'] = 4000; self.rejects(self.p, 'boundaries')
        self.context.evidence['view-2']['status'] = 'retracted'; self.rejects(self.p, 'retracted')
        self.context.evidence['view-2']['status'] = 'active'; self.context.evidence['view-2']['revision'] = 2
        self.rejects(self.p, 'stale')
        self.context.evidence['view-2']['revision'] = 1; self.context.evidence['view-2']['expires_at'] = time.time()-1
        self.rejects(self.p, 'expired')

    def test_expiry_and_scene_revision(self):
        self.p['expires_at'] = time.time()-1; self.rejects(self.p, 'expired')
        self.p = plan(self.cfg); self.p['scene_revision'] = 2; self.rejects(self.p, 'scene revision')

    def test_speeds_crops_limits_totals_and_untyped_operations(self):
        for field, value, reason in [('speed', .75, 'presets'), ('speed', True, 'finite'), ('crop_normalized', [0,0,2,1], 'crop'), ('crop_normalized', [0,0,.5,.6], 'crop'), ('speed', float('nan'), 'JSON')]:
            p = copy.deepcopy(self.p); p['shots'][1][field] = value
            with self.assertRaises((ValueError,TypeError)): self.context.resolve(p)
        self.p['expected_duration_ms'] = 7000; self.rejects(self.p, 'Stored duration')
        self.p = plan(self.cfg); self.p['shots'][0]['filter'] = 'shell'; self.rejects(self.p, 'unsupported')
        self.p = plan(self.cfg); self.p['shots'] *= 4; self.rejects(self.p, 'shot count')
        self.p = plan(self.cfg); self.p['shots'][0]['speed'] = .5; self.p['shots'][1]['speed'] = 2
        r = self.context.resolve(self.p); self.assertEqual(r.expected_ms, 6250); r.release()

    def test_calibration_requires_actual_marker_frames_and_holdout(self):
        data = calibration(self.sources[1]); data['markers'] = data['markers'][:2]
        with self.assertRaisesRegex(ValueError, '3–16'): self.context.calibrate(data)
        data = calibration(self.sources[1]); data['markers'][1]['pts'] = 9000
        with self.assertRaisesRegex(ValueError, 'retained frame'): self.context.calibrate(data)

    def test_context_is_bounded_actual_frame_sequence(self):
        data = self.context.window({'source_id':'camera-1','source_epoch':1,'source_start_ms':0,'source_end_ms':7000})
        self.assertEqual(len(data['frames']),24)
        self.assertIn('jpeg_base64', data['frames'][10]); self.assertIsNotNone(data['frames'][10]['event_ms'])
        with self.assertRaisesRegex(ValueError, 'ten seconds'):
            self.context.window({'source_id':'camera-1','source_epoch':1,'source_start_ms':0,'source_end_ms':11000})

    def test_visual_context_does_not_extrapolate_event_time(self):
        self.sources[1] = memory_source(self.cfg,1,150)
        data = self.context.window({'source_id':'camera-1','source_epoch':1,'source_start_ms':8200,'source_end_ms':9000})
        self.assertTrue(data['synchronization'].startswith('unknown'))
        self.assertTrue(all(frame['event_ms'] is None for frame in data['frames']))
        self.sources[1].path = 'replaced-lease'
        data = self.context.window({'source_id':'camera-1','source_epoch':1,'source_start_ms':0,'source_end_ms':7000})
        self.assertIsNone(data['mapping'])
        self.assertEqual(data['evidence_ids'],[])
        self.assertTrue(all(frame['event_ms'] is None for frame in data['frames']))

    def test_five_view_fixture_selection_fallback_and_skip(self):
        request = {'fixture':True,'scene_id':'staged-ball','scene_revision':1,'event_start_ms':1000,'event_end_ms':6000,'repeat':False}
        p = self.context.select_fixture(request); self.assertEqual(len(p['shots']),2)
        self.assertIn('camera-3',p['selection_reason'])
        self.context.evidence['view-2']['quality'] = 'obscured'
        p = self.context.select_fixture(request); self.assertEqual(len(p['shots']),1)
        self.context.evidence['view-1']['quality'] = 'obscured'
        with self.assertRaisesRegex(ValueError, 'Skip'): self.context.select_fixture(request)
        request['fixture'] = False
        with self.assertRaisesRegex(ValueError, 'providers are blocked'): self.context.select_fixture(request)

    def test_sync_failure_simplifies_once(self):
        self.context.mappings[('camera-2',1)][0] = replace(self.context.mappings[('camera-2',1)][0], uncertainty_ms=100)
        p = self.context.select_fixture({'fixture':True,'scene_id':'staged-ball','scene_revision':1,'event_start_ms':1000,'event_end_ms':6000,'repeat':True})
        self.assertEqual(len(p['shots']),1); self.assertIn('alignment uncertainty',p['selection_reason'])


if __name__ == '__main__': unittest.main()
