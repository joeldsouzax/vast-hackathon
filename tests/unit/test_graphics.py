"""Graphics contracts: prepared assets, official values, controller ownership, replay policy."""
import math
from pathlib import Path
import tempfile
import unittest
from PIL import Image, ImageChops
from graphics import CATALOG, Graphics, score_values
from media import Program
from studio import Config

class GraphicsContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graphics = Graphics()

    def setUp(self):
        self.g = self.graphics
        self.g.active.clear(); self.g.retiring.clear()
        self.g.score = score_values({}, confirmed=False); self.g.score['authority'] = None
        self.g.previous_score = self.g.score.copy()
        self.g.score_changed = 0

    def test_all_prepared_presets_animate_and_preserve_output_format(self):
        self.assertEqual(len(CATALOG), 24)
        self.assertEqual(len(self.g.manifest()['assets']), 24)
        self.assertIsNone(self.g.manifest()['context_revision'])
        for spec in CATALOG.values():
            with self.subTest(preset=spec['id']):
                self.g.active.clear()
                cue = self.g.prepare({'op': 'cue', 'preset': spec['id'], 'duration_s': 5 if spec['id'] == 'countdown' else 0})
                self.g.apply(cue, 100)
                frames = []
                for age in (0.1, 0.4, 0.7, 1.2, 1.8, 2.1):
                    image, snapshot = self.g.compose(Image.new('RGB', (640, 360), '#78213B'), 100+age, 'LIVE')
                    self.assertEqual(image.size, (640, 360)); self.assertEqual(image.mode, 'RGB')
                    frames.append(image)
                self.assertIsNotNone(ImageChops.difference(frames[0], frames[2]).getbbox())
                with Image.open(__import__('io').BytesIO(self.g.thumbnails[spec['id']])) as preview:
                    self.assertEqual(preview.size, (640, 360))
                if spec['slot'] not in ('screen', 'stinger'):
                    self.assertEqual(self.g.base_layers[spec['id']].getpixel((0, 0))[3], 0)
        self.assertIsNone(self.g.score['home_score']); self.assertIsNone(self.g.score['away_score'])

    def test_score_requires_explicit_confirmation_and_valid_fields(self):
        for data in ({}, {'confirmed': False}, {'confirmed': True, 'home_score': True},
                     {'confirmed': True, 'home_score': 3.5}, {'confirmed': True, 'clock_running': True},
                     {'confirmed': True, 'away_score': -1}, {'confirmed': True, 'score': 1}):
            with self.subTest(data=data), self.assertRaises(ValueError): score_values(data)
        score = score_values({'confirmed': True, 'home_score': 0})
        self.assertEqual(score['home_score'], 0); self.assertIsNone(score['away_score'])
        self.assertEqual(score['authority'], 'operator-confirmed')
        with self.assertRaises(ValueError):
            self.g.prepare({'op': 'cue', 'preset': 'score-compact', 'score': {'confirmed': True}})
        for duration in (math.nan, math.inf, True, -1, 3601):
            with self.assertRaises(ValueError): self.g.prepare({'op': 'cue', 'preset': 'opening', 'duration_s': duration})
        with self.assertRaises(ValueError): self.g.prepare({'op': 'cue', 'preset': 'opening', 'shell': 'anything'})

    def test_preview_does_not_commit_unconfirmed_facts(self):
        preview = self.g.prepare({'op': 'cue', 'preset': 'score-wide', 'score': {'home': 'Preview team', 'home_score': 5}}, preview=True)
        self.assertTrue(self.g.preview(preview).startswith(b'\x89PNG'))
        self.assertIsNone(self.g.score['home_score']); self.assertEqual(self.g.active, {})

    def test_score_clock_and_update_animation(self):
        self.g.apply(self.g.prepare({'op': 'cue', 'preset': 'score-compact'}), 100)
        first = self.g.prepare({'op': 'score', 'score': {'confirmed': True, 'home_score': 2, 'clock_seconds': 60, 'clock_running': True}})
        self.g.apply(first, 101)
        self.assertEqual(self.g.clock(self.g.score, 104.2), '01:03')
        self.g.apply(self.g.prepare({'op': 'score', 'score': {'confirmed': True, 'home_score': 3}}), 104.3)
        self.assertEqual(self.g.clock(self.g.score, 107.2), '01:06')
        frames = [self.g.compose(Image.new('RGB', (640, 360)), t, 'LIVE')[0] for t in (104.4, 105.0)]
        self.assertIsNotNone(ImageChops.difference(*frames).getbbox())
        self.g.apply(self.g.prepare({'op': 'score', 'score': {'confirmed': True, 'clock_seconds': None, 'clock_running': False}}), 105)
        self.assertEqual(self.g.clock(self.g.score, 110), '--:--')
        self.assertIsNone(self.g.score['home_score'])

    def test_replay_hides_current_facts_and_keeps_brand(self):
        for preset in ('lower-classic', 'ticker', 'headline', 'score-compact', 'brand-bug'):
            self.g.apply(self.g.prepare({'op': 'cue', 'preset': preset}), 100)
        _, snapshot = self.g.compose(Image.new('RGB', (640, 360)), 101, 'REPLAY')
        self.assertEqual([item['preset'] for item in snapshot['visible']], ['brand-bug'])
        self.assertTrue(snapshot['score_hidden_during_replay'])
        self.assertEqual(len(self.g.active), 5)  # Return to live restores eligible overlays.

    def test_layers_expire_exit_and_report_full_coverage(self):
        self.g.apply(self.g.prepare({'op': 'cue', 'preset': 'opening'}), 100)
        _, transparent = self.g.compose(Image.new('RGB', (640, 360)), 100, 'LIVE')
        self.assertEqual(transparent['visible'], [])
        _, applied = self.g.compose(Image.new('RGB', (640, 360)), 101, 'LIVE')
        self.assertTrue(applied['covers_camera'])
        self.g.clear('screen', 101)
        _, applied = self.g.compose(Image.new('RGB', (640, 360)), 101.1, 'LIVE')
        self.assertTrue(applied['visible'][0]['exiting']); self.assertFalse(applied['covers_camera'])
        _, applied = self.g.compose(Image.new('RGB', (640, 360)), 102, 'LIVE')
        self.assertEqual(applied['visible'], [])
        self.g.apply(self.g.prepare({'op': 'cue', 'preset': 'countdown', 'duration_s': 3}), 103)
        _, applied = self.g.compose(Image.new('RGB', (640, 360)), 106.1, 'LIVE')
        self.assertEqual(applied['visible'], [])

    def test_only_controller_commits_with_current_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            program = Program(Config(Path(directory), 'http://localhost:8080'), lambda _: None, lambda *a, **k: None)
            program.command('graphics', 0, graphics={'op': 'cue', 'preset': 'opening'})
            self.assertEqual(program.status()['graphics']['applied']['visible'], [])
            with self.assertRaises(ValueError):
                program.command('graphics', 0, graphics={'op': 'score', 'score': {'confirmed': True, 'home_score': 9}})
            self.assertIsNone(program.graphics.score['home_score'])
            program.command('live', 1)
            self.assertEqual(program.graphics.active, {})
            self.assertTrue((Path(directory) / 'graphics-package.json').is_file())
            from types import SimpleNamespace
            ready_path = Path(directory) / 'fixture.mp4'
            ready_path.write_bytes(b'labeled readiness fixture')
            program.command('replay', 2, replay=SimpleNamespace(id='fixture', frames=[b'frame'], path=ready_path, report={'decode_passed': True}))
            with self.assertRaises(ValueError):
                program.command('graphics', 3, graphics={'op': 'cue', 'preset': 'opening'})
            self.assertEqual(program.revision, 3)

if __name__ == '__main__': unittest.main()
