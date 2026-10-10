"""Scene facts stay bound to inspected frame identities and native time."""
from types import SimpleNamespace
import unittest

from foundation_records import DecisionSnapshot, SourceEpoch
from gemini_stack import FrameScene, GeminiStack
from provider_errors import ProviderFailure


class SceneEvidence(unittest.TestCase):
    def setUp(self):
        source=SourceEpoch(event_id='event',run_id='run',source_id='camera/one',slot=1,epoch=1,time_base='1/90000')
        snapshot=DecisionSnapshot(event_id='event',run_id='run',context_revision=1,configuration_revision=1,
            evidence_revision=0,program_revision=0,control_revision=0,sources=[source])
        self.window=SimpleNamespace(job_key='job',source=source,snapshot=snapshot,chunk_ids=['chunk'])
        self.frames=[{'frame_id':'frame-1','pts':90000,'end':93000}]
        self.scene=FrameScene(frame_id='frame-1',description='People at a table',visible_people=None,
            readable_text=['DEMO'],uncertainty=.3)
        self.stack=GeminiStack.__new__(GeminiStack)

    def test_native_frame_binding_does_not_invent_count_or_motion(self):
        row=self.stack.frame_observations([self.scene],self.frames,self.window,{'id':'model','version':'v1'})[0]
        self.assertEqual(row.native.model_dump(),{'start':90000,'end':93000})
        self.assertEqual(row.source,self.window.source)
        self.assertIsNone(row.view)
        self.assertNotIn('Visible people in this frame:',row.description)
        self.assertIn('"DEMO"',row.description)

    def test_unknown_missing_and_duplicate_frame_ids_are_rejected(self):
        for rows in ([],[self.scene,self.scene],[self.scene.model_copy(update={'frame_id':'invented'})]):
            with self.subTest(rows=rows),self.assertRaises(ProviderFailure):
                self.stack.frame_observations(rows,self.frames,self.window,{'id':'model','version':'v1'})
