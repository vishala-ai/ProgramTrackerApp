import copy
import json
import math
import unittest

import pandas as pd

from jira_dashboard.forecast_review import run_estimation_loop


class EstimationLoopTests(unittest.TestCase):
    def setUp(self):
        self.now = pd.Timestamp('2026-10-11T17:30:00Z')
        self.issues = pd.DataFrame([
            dict(key='KAN-1', issue_type='Story', status_category='Done', story_points=3, priority='High', team='Credit'),
            dict(key='KAN-2', issue_type='Story', status_category='In progress', story_points=5, priority='Medium', team='Credit'),
            dict(key='KAN-3', issue_type='Bug', status_category='To do', story_points=2, priority='High', team='Collection'),
        ])
        self.milestones = pd.DataFrame([dict(milestone='Unit testing validation', start_date=pd.Timestamp('2026-10-01'), due_date=pd.Timestamp('2027-04-30'))])
        self.inputs = dict(
            milestone_scenario=dict(target_completion_pct=91, scope_growth_until='2026-10-30'),
            forecast=dict(enabled=True, seed=42, trials=400, growth_fractions=[0, .25]),
        )
        self.builder_calls = []

    def build(self, issues, milestones, now, *, estimation_inputs, overrides=None):
        self.builder_calls.append(copy.deepcopy(dict(inputs=estimation_inputs, overrides=overrides)))
        options = estimation_inputs['forecast']
        trials = overrides['trials'] if overrides else options['trials']
        threshold = estimation_inputs['milestone_scenario']['target_completion_pct']
        scenarios = []
        for index, growth in enumerate(options['growth_fractions']):
            additional = math.ceil(len(issues) * growth)
            total = len(issues) + additional
            for policy in ('priority_larger_first', 'priority_smaller_first'):
                scenarios.append(dict(
                    id=f'{index}-{policy}', label=f'{growth:g} growth', growth_fraction=growth,
                    additional_items=additional, total_items=total, target_done_items=math.ceil(total * threshold / 100),
                    selection_policy=policy, p10_date='2026-11-30', p50_date='2027-01-10', p90_date='2027-03-15',
                    target_attainment_pct=80, finished_trials=trials, censored_trials=0, total_trials=trials,
                ))
        return dict(
            status='available', label='Simulated scenario', milestone='Unit testing validation',
            as_of=now.isoformat(), target_date='2027-04-30', scenarios=scenarios,
            assumptions=['Synthetic work and stated capacity assumptions.'], warnings=['This is simulation data.'],
            evidence=dict(current_total_items=3, current_done_items=1, target_completion_pct=threshold,
                          scope_growth_until='2026-10-30', earliest_attainment_date='2026-10-30'),
            seed=options['seed'], trials=trials, model_version='test-model',
        )

    def run_loop(self, **kwargs):
        return run_estimation_loop(self.issues, self.milestones, self.now, estimation_inputs=self.inputs,
                                   forecast_builder=kwargs.pop('forecast_builder', self.build), **kwargs)

    def test_ordinary_render_has_no_ai_review_and_reproducible_audit(self):
        result = self.run_loop()
        repeated = self.run_loop()
        self.assertTrue(result['published'])
        self.assertEqual(result['review'], dict(status='not_requested', passes=0, revisions=0, findings=[]))
        self.assertEqual(result['input_fingerprint'], repeated['input_fingerprint'])
        self.assertEqual(result['run_id'], repeated['run_id'])
        self.issues.loc[1, 'story_points'] = 8
        changed = self.run_loop()
        self.assertNotEqual(result['input_fingerprint'], changed['input_fingerprint'])

    def test_critic_receives_detached_json_and_cannot_edit_model_dates(self):
        def critic(bundle):
            bundle['scenarios'][0]['p50_date'] = '1999-01-01'
            bundle['evidence']['current_total_items'] = 999
            return dict(approved=True, findings=[], revision='none')
        result = self.run_loop(reviewer=critic)
        self.assertEqual(result['review']['status'], 'passed')
        self.assertEqual(result['scenarios'][0]['p50_date'], '2027-01-10')
        self.assertEqual(result['evidence']['current_total_items'], 3)

    def test_fingerprint_replays_equivalent_datetime_strings_and_numeric_estimates(self):
        self.issues['created'] = pd.Timestamp('2026-10-01T09:00:00-07:00')
        initial = self.run_loop()
        self.issues = pd.DataFrame(json.loads(self.issues.to_json(orient='records', date_format='iso')))
        self.milestones['start_date'] = '2026-10-01'
        self.milestones['due_date'] = '2027-04-30T00:00:00.000Z'
        self.now = self.now.tz_convert('America/Los_Angeles')
        repeated = self.run_loop()
        self.assertEqual(initial['input_fingerprint'], repeated['input_fingerprint'])
        self.assertEqual(initial['run_id'], repeated['run_id'])

    def test_one_allowed_revision_only_increases_trials_and_retains_inputs(self):
        responses = iter([
            dict(approved=False, findings=['Increase sampling resolution.'], revision='increase_trials'),
            dict(approved=True, findings=[], revision='none'),
        ])
        result = self.run_loop(reviewer=lambda bundle: next(responses))
        self.assertTrue(result['published'])
        self.assertEqual(result['review'], dict(status='passed', passes=2, revisions=1, findings=[]))
        self.assertEqual(result['seed'], 42)
        self.assertEqual(result['trials'], 800)
        self.assertEqual(self.builder_calls[1]['overrides'], {'trials': 800})
        self.assertEqual(self.builder_calls[0]['inputs'], self.builder_calls[1]['inputs'])
        self.assertEqual(self.inputs['forecast']['trials'], 400)

    def test_second_rejection_withholds_dates_but_preserves_source_facts(self):
        result = self.run_loop(reviewer=lambda bundle: dict(approved=False, findings=['Model not approved.'], revision='increase_trials'))
        self.assertFalse(result['published'])
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['review']['passes'], 2)
        self.assertEqual(result['review']['revisions'], 1)
        self.assertEqual(result['scenarios'], [])
        self.assertEqual(result['evidence']['current_total_items'], 3)
        self.assertNotIn('earliest_attainment_date', result['evidence'])

    def test_review_timeout_keeps_valid_outputs_clearly_unreviewed(self):
        def timeout(bundle):
            raise TimeoutError('private connection details should not be echoed')
        result = self.run_loop(reviewer=timeout)
        self.assertTrue(result['published'])
        self.assertEqual(result['review']['status'], 'unavailable')
        self.assertEqual(result['review']['passes'], 1)
        self.assertNotIn('private connection', str(result))

    def test_malformed_critic_cannot_change_assumptions_or_crash(self):
        for response in (
            'invalid JSON',
            dict(approved='yes', findings=[], revision='none'),
            dict(approved=False, findings=['Change capacity.'], revision='change_capacity'),
            dict(approved=True, findings=['Contradictory finding'], revision='none'),
            dict(approved=True, findings=[], revision='none', new_target='2027-01-01'),
        ):
            with self.subTest(response=response):
                result = self.run_loop(reviewer=lambda bundle: response)
                self.assertTrue(result['published'])
                self.assertEqual(result['review']['status'], 'unavailable')
                self.assertEqual(result['trials'], 400)

    def test_deterministic_errors_are_rejected_before_any_ai_call(self):
        def never_review(bundle):
            self.fail('Invalid arithmetic must not be sent to AI for approval.')
        mutations = [
            lambda bundle: bundle.update(milestone='UAT'),
            lambda bundle: bundle.update(label='Real delivery confidence'),
            lambda bundle: bundle['scenarios'][0].update(target_done_items=2),
            lambda bundle: bundle['scenarios'][0].update(total_items=99),
            lambda bundle: bundle['scenarios'][0].update(target_attainment_pct=101),
            lambda bundle: bundle['scenarios'][0].update(censored_trials=1),
            lambda bundle: bundle['scenarios'][0].update(p50_date='2026-11-01'),
            lambda bundle: bundle['scenarios'][0].update(p10_date='2026-10-15'),
            lambda bundle: bundle['scenarios'].pop(),
            lambda bundle: bundle['scenarios'][0].update(selection_policy={'unsupported': True}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                def bad_model(*args, **kwargs):
                    bundle = self.build(*args, **kwargs)
                    mutate(bundle)
                    return bundle
                result = self.run_loop(forecast_builder=bad_model, reviewer=never_review)
                self.assertFalse(result['published'])
                self.assertEqual(result['deterministic_validation']['status'], 'rejected')
                self.assertEqual(result['review']['passes'], 0)
                self.assertEqual(result['scenarios'], [])

    def test_revision_cannot_change_the_seed(self):
        def changed_seed(*args, **kwargs):
            bundle = self.build(*args, **kwargs)
            if kwargs.get('overrides'):
                bundle['seed'] = 99
            return bundle
        result = self.run_loop(forecast_builder=changed_seed, reviewer=lambda bundle: dict(approved=False, findings=['More samples.'], revision='increase_trials'))
        self.assertFalse(result['published'])
        self.assertEqual(result['review']['passes'], 1)
        self.assertEqual(result['review']['revisions'], 1)
        self.assertTrue(any('random seed' in item for item in result['review']['findings']))

    def test_trial_cap_does_not_invent_a_second_review(self):
        self.inputs['forecast']['trials'] = 2000
        result = self.run_loop(reviewer=lambda bundle: dict(approved=False, findings=['More trials.'], revision='increase_trials'))
        self.assertFalse(result['published'])
        self.assertEqual(result['review']['passes'], 1)
        self.assertEqual(result['review']['revisions'], 0)
        self.assertEqual(len(self.builder_calls), 1)

    def test_configurable_threshold_uses_exact_ceiling(self):
        self.inputs['milestone_scenario']['target_completion_pct'] = 50
        result = self.run_loop()
        self.assertTrue(result['published'])
        self.assertEqual(result['scenarios'][0]['target_done_items'], 2)

    def test_unavailable_engine_does_not_call_critic(self):
        result = self.run_loop(forecast_builder=lambda *args, **kwargs: dict(status='disabled', scenarios=[]),
                               reviewer=lambda bundle: self.fail('Disabled model cannot be reviewed.'))
        self.assertFalse(result['published'])
        self.assertEqual(result['review']['passes'], 0)
        self.assertEqual(result['review']['status'], 'not_requested')

    def test_credential_fields_are_removed_before_review_and_publication(self):
        def builder(*args, **kwargs):
            bundle = self.build(*args, **kwargs)
            bundle['access_token'] = 'private-top-level-token'
            bundle['evidence']['credentials'] = dict(password='private-password')
            return bundle
        def critic(bundle):
            self.assertNotIn('private-', str(bundle))
            return dict(approved=True, findings=[], revision='none')
        result = self.run_loop(forecast_builder=builder, reviewer=critic)
        self.assertTrue(result['published'])
        self.assertNotIn('private-', str(result))


if __name__ == '__main__':
    unittest.main()
