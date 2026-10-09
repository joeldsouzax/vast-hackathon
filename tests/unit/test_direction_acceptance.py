"""A partial green check must not become complete PRD acceptance."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'media'))
from direction_check import acceptance_coverage,REQUIRED_CONDITIONS


class DirectionAcceptance(unittest.TestCase):
    def test_partial_media_and_green_contracts_keep_missing_conditions_open(self):
        contracts={key:{'contract_checks_passed':True} for key in REQUIRED_CONDITIONS}
        media={'D03':{'passed':True,'conditions':{'scheduler_return':True}},
               'D15':{'passed':True,'conditions':{'role_queue_bounds':True}}}
        coverage=acceptance_coverage(contracts,media,{'local_ready':True})
        self.assertFalse(coverage['D03']['passed'])
        self.assertIn('holding_decoded',coverage['D03']['missing_conditions'])
        self.assertFalse(coverage['D15']['passed'])
        self.assertIn('speech_storage_saturation',coverage['D15']['missing_conditions'])
        self.assertFalse(coverage['D14']['passed'])
        self.assertIn('monitor_mute_is_local',coverage['D14']['missing_conditions'])

    def test_all_required_observations_are_needed_to_close_a_criterion(self):
        for criterion,required in REQUIRED_CONDITIONS.items():
            with self.subTest(criterion=criterion):
                conditions={name:True for name in required}
                result=acceptance_coverage({}, {criterion:{'conditions':conditions}})[criterion]
                self.assertTrue(result['passed'])
                conditions[required[-1]]=False
                self.assertFalse(acceptance_coverage({}, {criterion:{'conditions':conditions}})[criterion]['passed'])
