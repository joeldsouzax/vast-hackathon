"""Required checks for packaged contract provenance, without running media gates."""
from pathlib import Path
import shutil
import tempfile
import unittest

from replay_check import ROOT, contract_provenance


class ReplayProvenanceR24(unittest.TestCase):
    def environment(self, root=ROOT):
        return {'BREADCAST_CHECK_COMMIT':'a'*40,'BREADCAST_CHECK_DIFF_SHA256':'b'*64,
            'BREADCAST_CHECK_CONTRACT_SHA256':contract_provenance(root,{})['input_sha256']}

    def test_matching_inputs_are_required(self):
        environment=self.environment()
        self.assertTrue(contract_provenance(environment=environment)['matched'])
        for expected in (None,'present','0'*64):
            with self.subTest(expected=expected):
                environment['BREADCAST_CHECK_CONTRACT_SHA256']=expected
                self.assertFalse(contract_provenance(environment=environment)['matched'])

    def test_changed_packaged_fixture_rejects_the_original_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for relative in contract_provenance()['file_sha256']:
                target=root/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(ROOT/relative,target)
            environment=self.environment(root)
            fixture=root/'docs/examples/replay-plan.example.json'
            fixture.write_bytes(fixture.read_bytes()+b'\n')
            self.assertFalse(contract_provenance(root,environment)['matched'])

    def test_missing_or_malformed_revision_metadata_is_rejected(self):
        for name in ('BREADCAST_CHECK_COMMIT','BREADCAST_CHECK_DIFF_SHA256'):
            for value in ('','claimed-current','a'*39):
                with self.subTest(name=name,value=value):
                    environment=self.environment()
                    environment[name]=value
                    self.assertFalse(contract_provenance(environment=environment)['matched'])
