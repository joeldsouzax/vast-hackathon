"""Cosmos observation parsing for live GPU analyze."""
import unittest

from live_gpu import _ffmpeg_seconds, cosmos_observation_items, json_content


class CosmosObservationParse(unittest.TestCase):
    def test_clamps_tiny_overshoot(self):
        duration = 4.976533333333333
        items = [{
            'start_s': 0.0, 'end_s': 5.0, 'description': 'rider turns left',
            'kind': 'observed', 'uncertainty': 0.3, 'subject_visible': True,
            'view_quality': 'usable', 'adds': 'full view of action',
            'replay_opportunity': None, 'urgent_live': False,
        }]
        parsed = cosmos_observation_items(items, duration)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['end_s'], duration)
        self.assertLessEqual(parsed[0]['end_s'], duration)

    def test_skips_far_out_of_range(self):
        items = [{
            'start_s': 0.0, 'end_s': 9.0, 'description': 'too long',
            'kind': 'observed', 'uncertainty': 0.3, 'subject_visible': True,
            'view_quality': 'usable', 'adds': 'full view',
        }]
        self.assertEqual(cosmos_observation_items(items, 5.0), [])

    def test_json_content_list_parts(self):
        data = json_content([{'type': 'text', 'text': '{"observations":[]}'}])
        self.assertEqual(data, {'observations': []})

    def test_rejects_more_than_four(self):
        items = [{'start_s': 0.0, 'end_s': 0.5, 'description': 'x', 'kind': 'observed',
                  'uncertainty': 0.1, 'subject_visible': True, 'view_quality': 'usable',
                  'adds': 'a'} for _ in range(5)]
        with self.assertRaises(ValueError):
            cosmos_observation_items(items, 5.0)

    def test_ffmpeg_seconds_avoids_scientific_notation(self):
        self.assertEqual(_ffmpeg_seconds(1.1111111111111112e-05), '0.000011')
        self.assertEqual(_ffmpeg_seconds(-0.5), '0.000000')
        self.assertNotIn('e', _ffmpeg_seconds(2.0))


if __name__ == '__main__':
    unittest.main()
