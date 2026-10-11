import unittest

import pandas as pd

from jira_dashboard.readout import _effort_outlook, build_weekly_readout


class EffortReadoutTests(unittest.TestCase):
    def setUp(self):
        self.now = pd.Timestamp('2026-10-11T19:00:00Z')
        self.milestones = pd.DataFrame([
            dict(milestone='Unit testing validation', start_date=pd.Timestamp('2027-04-01'),
                 due_date=pd.Timestamp('2027-04-30'), status='Not started'),
        ])
        self.issues = pd.DataFrame([
            dict(issue_type='Story', team='Credit', status_category='In progress', story_points=13, resolved=None),
            dict(issue_type='Story', team='Credit', status_category='To do', story_points=None, resolved=None),
            dict(issue_type='Bug', team='Collection', status_category='To do', story_points=3, resolved=None),
            dict(issue_type='Story', team='Credit', status_category='Done', story_points=5, resolved='2026-10-10T12:00:00Z'),
            dict(issue_type='Bug', team='Collection', status_category='Done', story_points=None, resolved='2026-10-10T12:00:00Z'),
        ])

    def outlook(self, issues=None):
        return '\n'.join(_effort_outlook(self.issues if issues is None else issues, self.now, self.milestones))

    def test_effort_coverage_and_history_preserve_unknown_work(self):
        report = self.outlook()
        self.assertIn('16 story points across 2 of 3 open Stories/Bugs; 1 open items are unestimated', report)
        self.assertIn('| Credit | 2 | 1 | 1 | 13 |', report)
        self.assertIn('| Collection | 1 | 1 | 0 | 3 |', report)
        self.assertIn('| Oct 11, 2026 12:00 | 2 | 1 | 1 | 5 |', report)
        self.assertIn('No reliable finish date is estimated', report)
        self.assertIn('Apr 30, 2027 (planned deadline, not a predicted finish)', report)
        self.assertIn('remaining effort within an in-progress item is unknown', report)

    def test_missing_estimates_are_not_assumed_to_be_zero_effort(self):
        issues = self.issues.drop(columns='story_points')
        report = self.outlook(issues)
        self.assertIn('Known remaining effort:** Unavailable story points', report)
        self.assertIn('3 open items are unestimated', report)
        self.assertIn('| Oct 11, 2026 12:00 | 2 | 0 | 2 | Unavailable |', report)

    def test_fractional_estimates_are_not_rounded_to_zero(self):
        self.issues['story_points'] = None
        self.issues.loc[0, 'story_points'] = 0.04
        self.assertIn('Known remaining effort:** 0.04 story points', self.outlook())

    def test_missing_issue_baseline_and_closed_backlog_do_not_imply_acceptance(self):
        self.assertIn('Unavailable without issue-level Jira estimates', self.outlook(pd.DataFrame()))
        closed = self.issues.loc[self.issues['status_category'].eq('Done')]
        self.assertIn('milestone validation and acceptance still need confirmation', self.outlook(closed))
        self.milestones.loc[0, 'due_date'] = pd.NaT
        self.assertIn('Unavailable; confirm the milestone plan', self.outlook())

    def test_high_item_throughput_does_not_produce_a_linear_closure_date(self):
        report = build_weekly_readout(
            project_name='Test', project_key='KAN', generated_at=self.now,
            issues=self.issues, snapshots=pd.DataFrame(), milestones=self.milestones,
            budget=pd.DataFrame(), actuals=pd.DataFrame(), budget_cap=10_000_000,
            overall_status='RED', schedule_status='GREEN', cost_status='RED',
            planned_rate=7.7, actual_rate=15.0,
        )
        self.assertNotIn('## Effort and milestone outlook', report)
        self.assertIn('No reliable finish date is estimated', report)
        self.assertIn('Schedule Deep Dive', report)
        self.assertNotIn('### Remaining effort by workstream', report)
        self.assertNotIn('### Observed completed effort', report)
        self.assertNotIn('Projected backlog closure', report)
        self.assertNotIn('days before', report)
        self.assertNotIn('completed items/week ≈', report)
        self.assertIn('Review Unit testing forecast inputs and unresolved evidence in Schedule Deep Dive', report)

    def test_readout_does_not_trust_stale_item_pace_when_completion_dates_are_missing(self):
        self.issues['resolved'] = None
        report = build_weekly_readout(
            project_name='Test', project_key='KAN', generated_at=self.now,
            issues=self.issues, snapshots=pd.DataFrame(), milestones=self.milestones,
            budget=pd.DataFrame(), actuals=pd.DataFrame(), budget_cap=10_000_000,
            overall_status='RED', schedule_status='GREEN', cost_status='RED',
            planned_rate=7.7, actual_rate=15.0,
        )
        self.assertIn('Jira actual pace is unavailable', report)
        self.assertNotIn('2 Done items lack usable resolution dates', report)
        self.assertIn('Schedule Deep Dive', report)
        self.assertNotIn('15.0 actual', report)

    def test_readiness_details_are_omitted_from_concise_weekly_reports(self):
        report = build_weekly_readout(
            project_name='Test', project_key='KAN', generated_at=self.now,
            issues=self.issues, snapshots=pd.DataFrame(), milestones=self.milestones,
            budget=pd.DataFrame(), actuals=pd.DataFrame(), budget_cap=10_000_000,
            overall_status='RED', schedule_status='GREEN', cost_status='RED',
            planned_rate=7.7, actual_rate=15.0,
            estimation_inputs={'data_mode': 'simulation', 'confirmations': {}, 'error': None},
        )
        self.assertNotIn('## Estimation readiness', report)
        self.assertNotIn('| Input | Status | Evidence | Owner / role | Next step |', report)
        self.assertNotIn('No finish date is produced by this assessment', report)
        self.assertIn('No reliable finish date is estimated', report)
        self.assertIn('Schedule Deep Dive', report)

    def test_invalid_owner_configuration_is_visible_in_report(self):
        report = build_weekly_readout(
            project_name='Test', project_key='KAN', generated_at=self.now,
            issues=self.issues, snapshots=pd.DataFrame(), milestones=self.milestones,
            budget=pd.DataFrame(), actuals=pd.DataFrame(), budget_cap=10_000_000,
            overall_status='RED', schedule_status='GREEN', cost_status='RED',
            planned_rate=7.7, actual_rate=15.0,
            estimation_inputs={'data_mode': 'unknown', 'confirmations': {}, 'error': 'Project mismatch'},
        )
        self.assertIn('Project mismatch', report)
        self.assertIn('No owner confirmations were applied', report)

    def test_simulated_provenance_does_not_add_verbose_readiness_details(self):
        issues = pd.concat([
            self.issues.assign(is_simulated=False),
            pd.DataFrame([dict(issue_type='Epic', status_category='Done', is_simulated=True)]),
        ], ignore_index=True)
        report = build_weekly_readout(
            project_name='Test', project_key='KAN', generated_at=self.now,
            issues=issues, snapshots=pd.DataFrame(), milestones=self.milestones,
            budget=pd.DataFrame(), actuals=pd.DataFrame(), budget_cap=10_000_000,
            overall_status='RED', schedule_status='GREEN', cost_status='RED',
            planned_rate=7.7, actual_rate=15.0,
            estimation_inputs={'data_mode': 'real', 'confirmations': {}, 'error': None},
        )
        self.assertNotIn('| Data provenance |', report)
        self.assertNotIn('at least one issue is marked is_simulated=True', report)
        self.assertIn('No reliable finish date is estimated', report)
        self.assertIn('Schedule Deep Dive', report)


if __name__ == '__main__':
    unittest.main()
