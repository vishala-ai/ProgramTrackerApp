import unittest

import pandas as pd

from jira_dashboard.readout import build_weekly_readout


class MilestoneScenarioReadoutTests(unittest.TestCase):
    def setUp(self):
        self.now = pd.Timestamp('2026-10-11T19:00:00Z')
        self.issues = pd.DataFrame([
            dict(issue_type='Story' if index % 2 else 'Bug', team='Credit',
                 status_category='Done' if index < 60 else 'In progress',
                 story_points=1 if index < 60 else 13,
                 resolved=self.now - pd.Timedelta(days=1) if index < 60 else None)
            for index in range(100)
        ])
        self.milestones = pd.DataFrame([
            dict(milestone='Inventory completion', start_date=pd.Timestamp('2026-09-20'),
                 due_date=pd.Timestamp('2026-10-30'), status='In Progress'),
            dict(milestone='Unit testing validation', start_date=pd.Timestamp('2027-02-01'),
                 due_date=pd.Timestamp('2027-04-30'), status='Not started'),
        ])
        self.inputs = {
            'data_mode': 'simulation', 'error': None,
            'confirmations': {'milestone_scope': {
                'confirmed': True, 'owner': 'TPM',
                'evidence': 'All KAN Stories/Bugs including technical debt; at least 91% Done by Apr 30, 2027.',
            }},
            'milestone_scenario': {'target_completion_pct': 91, 'scope_growth_until': '2026-10-30'},
        }

    def report(self, **changes):
        arguments = dict(
            project_name='Test', project_key='KAN', generated_at=self.now,
            issues=self.issues, snapshots=pd.DataFrame(), milestones=self.milestones,
            budget=pd.DataFrame(), actuals=pd.DataFrame(), budget_cap=10_000_000,
            overall_status='RED', schedule_status='GREEN', cost_status='RED',
            planned_rate=7.7, actual_rate=15.0, estimation_inputs=self.inputs,
        )
        arguments.update(changes)
        return build_weekly_readout(**arguments)

    def test_threshold_risks_and_actions_are_saved_without_ai(self):
        report = self.report()
        self.assertIn('40 open and 60 completed stories/bugs', report)
        self.assertNotIn('91 Done items required; 31 additional completions needed', report)
        weeks_left = (pd.Timestamp('2027-04-30') - pd.Timestamp('2026-10-11')).days / 7
        self.assertIn(f'{31 / weeks_left:.1f} planned / 60.0 actual items per week', report)
        self.assertIn('Planned item pace uses the current 91% Done threshold', report)
        lowlights = report.split('## Lowlights')[1].split('## Suggested actions')[0]
        self.assertIn('Schedule risk — residual scope', lowlights)
        self.assertIn('9%', lowlights)
        self.assertIn('Schedule risk — scope growth', lowlights)
        self.assertIn('Oct 30, 2026', lowlights)
        self.assertIn('9% residual scope', report)
        self.assertIn('Apr 30, 2027', report)
        self.assertIn('Schedule Deep Dive', report)
        for detail in ('520 story points', '## Milestone completion scenario',
                       '## Effort and milestone outlook', '## Estimation readiness',
                       '| Milestone scope and acceptance | Owner-confirmed |'):
            self.assertNotIn(detail, report)

    def test_91_percent_is_reached_without_requiring_100_percent(self):
        issues = self.issues.copy()
        issues.loc[:90, 'status_category'] = 'Done'
        report = self.report(issues=issues)
        self.assertIn('9 open and 91 completed stories/bugs', report)
        self.assertIn('0.0 planned /', report)
        self.assertIn('9%', report)
        self.assertIn('Schedule risk — scope growth', report)
        self.assertNotIn('91 Done items required', report)

    def test_after_checkpoint_does_not_assert_automatic_scope_freeze(self):
        report = self.report(generated_at=pd.Timestamp('2026-10-31T19:00:00Z'))
        self.assertIn('Schedule risk — scope baseline', report)
        self.assertIn('Oct 30, 2026', report)
        self.assertNotIn('Scope remains open to further additions through', report)

    def test_empty_issue_baseline_does_not_use_aggregate_snapshots_for_target_counts(self):
        snapshots = pd.DataFrame([dict(snapshot_date=pd.Timestamp('2026-10-11'), open_items=40, completed_items=60)])
        report = self.report(issues=pd.DataFrame(), snapshots=snapshots)
        self.assertIn('40 open and 60 completed stories/bugs', report)
        self.assertIn('Planned pace is unavailable', report)
        self.assertNotIn('91 Done items required', report)
        self.assertIn('Schedule risk — residual scope', report)

    def test_old_configuration_does_not_invent_scenario_assumptions(self):
        report = self.report(estimation_inputs={'data_mode': 'simulation', 'confirmations': {}, 'error': None})
        self.assertNotIn('## Milestone completion scenario', report)
        self.assertNotIn('Schedule risk — residual scope', report)


if __name__ == '__main__':
    unittest.main()
