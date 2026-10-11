import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from jira_dashboard.forecast_artifacts import save_forecast_run
from jira_dashboard.forecast_review import run_estimation_loop
from jira_dashboard.readout import (FORECAST_REVIEW_SCHEMA, _check_commentary_facts,
                                    build_weekly_readout, make_estimation_reviewer)


class ForecastIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now = pd.Timestamp('2026-10-11T19:00:00Z')
        self.issues = pd.DataFrame([
            dict(key=f'KAN-{index}', issue_type='Story', team='Credit', assignee='credit1',
                 priority='Medium', status_category='Done' if index < 2 else 'To do',
                 story_points=3 if index < 2 else 13,
                 resolved=self.now-pd.Timedelta(days=1) if index < 2 else None)
            for index in range(20)
        ])
        self.milestones = pd.DataFrame([
            dict(milestone='Inventory completion', start_date=pd.Timestamp('2026-09-20'),
                 due_date=pd.Timestamp('2026-10-31'), status='In Progress'),
            dict(milestone='Unit testing validation', start_date=pd.Timestamp('2027-02-01'),
                 due_date=pd.Timestamp('2027-04-30'), status='Not started'),
        ])
        self.inputs = dict(data_mode='simulation', error=None,
            milestone_scenario=dict(target_completion_pct=91, scope_growth_until='2026-10-30'),
            forecast=dict(enabled=True, trials=100), confirmations={
                key: dict(confirmed=True, owner='TPM', evidence='User-confirmed simulation assumption.')
                for key in ('milestone_scope','dependencies','capacity','remaining_work','testing_rework')
            })

    def report(self, forecast=None, inputs=None):
        return build_weekly_readout(
            project_name='Test', project_key='KAN', generated_at=self.now, issues=self.issues,
            snapshots=pd.DataFrame(), milestones=self.milestones, budget=pd.DataFrame(),
            actuals=pd.DataFrame(), budget_cap=10_000_000, overall_status='RED', schedule_status='RED',
            cost_status='RED', planned_rate=1, actual_rate=2, estimation_inputs=inputs or self.inputs,
            estimation_forecast=forecast,
        )

    def test_enabled_readout_contains_conditional_results_and_schedule_risks(self):
        bundle = run_estimation_loop(self.issues, self.milestones, self.now, estimation_inputs=self.inputs)
        with patch('jira_dashboard.readout._generate_model_step', side_effect=AssertionError('No automatic AI calls')):
            report = self.report(bundle)
        self.assertIn('## Unit testing forecast summary', report)
        self.assertIn('Earliest modeled completion', report)
        self.assertIn('Latest modeled completion', report)
        earliest = min(case['p10_date'] for case in bundle['scenarios'])
        latest = max(case['p90_date'] for case in bundle['scenarios'])
        self.assertIn(pd.Timestamp(earliest).strftime('%b %d, %Y'), report)
        self.assertIn(pd.Timestamp(latest).strftime('%b %d, %Y'), report)
        self.assertIn('Conditional simulated range', report)
        self.assertIn('unreviewed', report.lower())
        self.assertIn('Schedule Deep Dive', report)
        self.assertIn('91%', report)
        self.assertIn('9%', report)
        self.assertIn('Schedule risk', report)
        for detail in ('| Scope case /', '## Effort and milestone outlook', '## Estimation readiness',
                       '## Milestone completion scenario', '### Remaining effort by workstream',
                       '### Observed completed effort', '**Reproducibility:**', '**AI evidence review:**'):
            self.assertNotIn(detail, report)

    def test_rejected_forecast_withholds_dates_and_keeps_fact_based_report(self):
        bundle = run_estimation_loop(self.issues,self.milestones,self.now,estimation_inputs=self.inputs,
            reviewer=lambda _:dict(approved=False, findings=['Assumptions need review.'], revision='none'))
        self.assertFalse(bundle['published'])
        report=self.report(bundle)
        self.assertIn('Earliest modeled completion',report)
        self.assertIn('Latest modeled completion',report)
        self.assertIn('Unavailable',report)
        self.assertNotIn('| Scope case /',report)
        self.assertNotIn('19 Done items required',report)
        self.assertIn('Apr 30, 2027',report)
        self.assertIn('Schedule Deep Dive',report)

    def test_ai_outage_keeps_simulated_results_explicitly_unreviewed(self):
        def offline(_):
            raise TimeoutError()
        bundle=run_estimation_loop(self.issues,self.milestones,self.now,estimation_inputs=self.inputs,reviewer=offline)
        report=self.report(bundle)
        self.assertTrue(bundle['published'])
        self.assertIn('unreviewed',report.lower())
        self.assertNotIn('**AI evidence review:**',report)
        date=pd.Timestamp(min(case['p10_date'] for case in bundle['scenarios'])).strftime('%b %d, %Y')
        self.assertIn(date,report)
        findings=_check_commentary_facts(report,
            f'Overall: RED; Schedule: RED; Cost: RED. Conditional simulated completion: {date}.')
        self.assertFalse(any('date' in finding or 'predicted finish' in finding for finding in findings))

    def test_disabled_config_preserves_existing_no_forecast_behavior(self):
        inputs=copy.deepcopy(self.inputs)
        inputs['forecast']['enabled']=False
        report=self.report(inputs=inputs)
        self.assertNotIn('## Unit testing forecast summary',report)
        self.assertIn('No reliable finish date is estimated.',report)
        self.assertIn('Schedule Deep Dive',report)
        self.assertNotIn('## Estimation readiness',report)

    def test_model_dates_cannot_become_unconditional_or_downstream_completion_claims(self):
        bundle=run_estimation_loop(self.issues,self.milestones,self.now,estimation_inputs=self.inputs)
        report=self.report(bundle)
        date=pd.Timestamp(min(case['p10_date'] for case in bundle['scenarios'])).strftime('%b %d, %Y')
        for claim in (f'The program will finish by {date}.',
                      f'Unit testing will finish by {date}.',
                      f'The conditional simulation shows UAT will complete by {date}.'):
            with self.subTest(claim=claim):
                findings=_check_commentary_facts(report,'Overall: RED; Schedule: RED; Cost: RED. '+claim)
                self.assertTrue(any('Unit testing date' in item or 'program completion' in item for item in findings))

    def test_late_conditional_primary_case_is_visible_as_schedule_risk_and_action(self):
        self.issues.loc[2:,'story_points']=64
        bundle=run_estimation_loop(self.issues,self.milestones,self.now,estimation_inputs=self.inputs)
        report=self.report(bundle)
        self.assertIn('**Schedule risk — simulated timing:**',report)
        self.assertIn('TPM and workstream leads: review the simulated timing risk',report)
        lowlights=report.split('## Lowlights')[1].split('## Suggested actions')[0]
        self.assertNotIn('conditional median Unit testing date',lowlights)
        self.assertNotIn('% of all trials',lowlights)
        for claim in ('Unit testing is on track.',
                      'The team is on track with unit testing, but scope growth and residual scope risks need attention.'):
            with self.subTest(claim=claim):
                findings=_check_commentary_facts(report,claim)
                self.assertTrue(any('Do not call Unit testing on track' in item for item in findings))
        for claim in ('Unit testing is not on track.',
                      'Unit testing pace is on track, but the conditional forecast flags timing risk.'):
            with self.subTest(claim=claim):
                findings=_check_commentary_facts(report,claim)
                self.assertFalse(any('Do not call Unit testing on track' in item for item in findings))

    @patch('jira_dashboard.readout._generate_model_step')
    def test_provider_adapter_uses_bounded_json_schema(self, model):
        model.return_value='{"approved":true,"findings":[],"revision":"none"}'
        result=make_estimation_reviewer('ollama','test-model')({'scenarios':[]})
        self.assertTrue(result['approved'])
        self.assertEqual(model.call_args.kwargs['response_schema'],FORECAST_REVIEW_SCHEMA)
        self.assertEqual(json.loads(model.call_args.args[2]),{'scenarios':[]})
        model.return_value='Not JSON'
        with self.assertRaises(RuntimeError):
            make_estimation_reviewer('ollama','test-model')({})

    def test_saved_run_replays_model_without_saving_credentials_or_raw_jira_text(self):
        self.issues['story_points']=self.issues['story_points'].astype(float)
        self.issues.loc[0,'story_points']=2.2222222222222223
        bundle=run_estimation_loop(self.issues,self.milestones,self.now,estimation_inputs=self.inputs)
        source=self.issues.assign(access_token='do-not-save',summary='Jira text not needed by model')
        with TemporaryDirectory() as directory:
            path=Path(directory)/'weekly_readout_test.forecast.json'
            save_forecast_run(path,bundle,issues=source,milestones=self.milestones,now=self.now,
                              estimation_inputs=self.inputs | {'api_key':'do-not-save'})
            saved=json.loads(path.read_text())
            self.assertNotIn('do-not-save',path.read_text())
            self.assertNotIn('Jira text not needed',path.read_text())
            inputs=saved['reproducibility_inputs']
            replay=run_estimation_loop(pd.DataFrame(inputs['issues']),pd.DataFrame(inputs['milestones']),
                pd.Timestamp(inputs['as_of']),estimation_inputs=inputs['estimation_inputs'])
        self.assertEqual(bundle['scenarios'],replay['scenarios'])
        self.assertEqual(bundle['input_fingerprint'],replay['input_fingerprint'])
        self.assertEqual(bundle['run_id'],replay['run_id'])


if __name__ == '__main__':
    unittest.main()
