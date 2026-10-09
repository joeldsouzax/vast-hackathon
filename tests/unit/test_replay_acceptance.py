"""Task 3 acceptance cannot be inferred from a green subset."""
import unittest
from replay_check import REQUIRED,coverage

class ReplayAcceptance(unittest.TestCase):
    def test_R24_missing_conditions_block_complete_acceptance(self):
        contract_only={key:{'contracts':True} for key in REQUIRED}
        report=coverage(contract_only)
        self.assertFalse(all(row['passed'] for row in report.values()))
        self.assertIn('five_sources_900s',report['R21']['missing_conditions'])
        for key,names in REQUIRED.items():
            complete={name:True for name in names}
            self.assertTrue(coverage({key:complete})[key]['passed'])
            complete[names[-1]]=False
            self.assertFalse(coverage({key:complete})[key]['passed'])
