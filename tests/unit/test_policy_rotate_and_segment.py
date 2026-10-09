"""Policy rotation request shape and deterministic segmentor fallback."""
import inspect
import unittest
from control import Coordinator
from foundation_records import DecisionSnapshot, Interval, SceneEvent, SourceEpoch
from replay_work import deterministic_segment


class DeterministicSegment(unittest.TestCase):
    def test_builds_plan_from_scene(self):
        source=SourceEpoch(event_id='e',run_id='r'*32,source_id='camera/a',epoch=1,slot=1,time_base='1/90000')
        native=Interval(start=0,end=180000)
        eid='a'*64
        scene=SceneEvent(scene_id='s'*64,revision=1,source=source,native=native,
            evidence_ids=[eid],description='scene',uncertainty=0.3,origin='provider')
        snapshot=DecisionSnapshot(event_id='e',run_id='r'*32,context_revision=1,configuration_revision=1,
            evidence_revision=1,program_revision=0,control_revision=0,sources=[source])
        result=deterministic_segment(
            {'observations':[{'evidence_id':eid,'native':{'start':0,'end':180000}}]},
            scene,snapshot)
        self.assertIsNotNone(result)
        self.assertEqual(result.payload.op,'plan')
        self.assertGreaterEqual(len(result.payload.shots),1)
        self.assertEqual(result.model_id,'deterministic-segmentor')


class PolicyRotateRequest(unittest.TestCase):
    def test_expected_requires_args(self):
        self.assertIn('args',inspect.signature(Coordinator.expected).parameters)


if __name__=='__main__':
    unittest.main()
