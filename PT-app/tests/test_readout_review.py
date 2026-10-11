"""Regression checks for commentary transport and evidence-review failures."""
import json
import unittest
from unittest.mock import Mock, patch

from jira_dashboard import readout


class CommentaryReviewTests(unittest.TestCase):
    def decision(self, **changes):
        decision = dict(approved=True, missing_aspects=[], unsupported_claims=[],
                        uncovered_risks=[], risk_coverage='adequate', revision_instructions='')
        decision.update(changes)
        return json.dumps(decision)

    @patch.object(readout.requests, 'post')
    def test_local_critic_enforces_schema(self, post):
        post.return_value = Mock(ok=True)
        post.return_value.json.return_value = {'message': {'content': self.decision()}, 'done_reason': 'stop'}
        raw = readout._generate_model_step('ollama', 'llama3.2', 'report', readout.REVIEW_INSTRUCTIONS)
        request = post.call_args.kwargs['json']
        self.assertEqual(request['format']['required'], list(readout.REVIEW_SCHEMA['properties']))
        self.assertEqual(request['options']['temperature'], 0)
        self.assertEqual(list(request['format']['properties'])[-1], 'approved')
        self.assertEqual(request['options']['num_ctx'], 8192)
        self.assertTrue(readout._parse_commentary_review(raw)['approved'])

    @patch.object(readout.requests, 'post')
    def test_truncated_output_cannot_pass(self, post):
        post.return_value = Mock(ok=True)
        post.return_value.json.return_value = {'message': {'content': self.decision()}, 'done_reason': 'length'}
        with self.assertRaisesRegex(readout.OllamaReadoutError, 'token limit'):
            readout.generate_ollama_commentary('llama3.2', 'report')

    def test_contradictory_approval_is_rejected(self):
        decision = self.decision(unsupported_claims=['Invented savings'])
        self.assertFalse(readout._parse_commentary_review(decision)['approved'])

    def test_report_copy_is_rejected_as_commentary_even_when_facts_match(self):
        draft='Overall: RED; Schedule: RED; Cost: RED.\n## Executive status\n| Scope | 335 |'
        self.assertTrue(any('headings or tables' in item for item in readout._check_commentary_facts(draft,draft)))

    @patch.object(readout,'_generate_model_step')
    def test_structured_summary_renders_once_without_report_sections(self,step):
        step.side_effect=[json.dumps(dict(status_line='Overall: RED; Schedule: RED; Cost: RED.',
                                         summary='Review the supplied risks.',bullets=['Confirm the scope.'])),self.decision()]
        result=readout.generate_configured_commentary('ollama','test','Overall: RED; Schedule: RED; Cost: RED.')
        self.assertEqual(result.text.count('Overall:'),1)
        self.assertIn('- Confirm the scope.',result.text)
        schema=step.call_args_list[0].kwargs['response_schema']
        self.assertEqual(schema['properties']['status_line']['enum'],['Overall: RED; Schedule: RED; Cost: RED.'])
        self.assertEqual(schema['properties']['bullets']['maxItems'],5)

    @patch.object(readout, '_generate_model_step')
    def test_unresolved_evidence_failure_still_falls_back(self, step):
        bad = self.decision(approved=False, uncovered_risks=['Budget gap missing'], risk_coverage='incomplete')
        step.side_effect = ['draft', bad, 'revised draft', bad]
        with self.assertRaisesRegex(readout.ReadoutModelError, '2 review passes'):
            readout.generate_configured_commentary('ollama', 'llama3.2', 'report')
        self.assertEqual(step.call_count, 4)

    @patch.object(readout, '_generate_model_step')
    def test_wrong_status_cannot_pass_even_when_model_approves(self, step):
        source = 'Overall: RED. Schedule: RED. Cost: RED.'
        bad = 'Overall: GREEN. Schedule: GREEN. Cost: GREEN.'
        step.side_effect = [bad, self.decision(), bad, self.decision()]
        with self.assertRaisesRegex(readout.ReadoutModelError, 'state Overall: RED'):
            readout.generate_configured_commentary('ollama', 'llama3.2', source)

    def test_invented_money_is_rejected_and_equivalent_units_accepted(self):
        report = '$10,000,000 cap; $2,000,000 gap'
        self.assertEqual(readout._check_commentary_facts(report, '$10 million cap; $2M gap'), [])
        self.assertTrue(readout._check_commentary_facts(report, '$3,000,000 savings'))

    def test_equivalent_status_wording(self):
        report = 'Overall: RED; Schedule: RED; Cost: RED.'
        for draft in ('Overall status is RED. Schedule remains RED. Cost: RED.',
                      'Overall, Schedule and Cost are all RED.'):
            self.assertEqual(readout._check_commentary_facts(report, draft), [])
        self.assertTrue(readout._check_commentary_facts(report, 'Overall: GREEN. Overall, Schedule and Cost are all RED.'))
        self.assertTrue(readout._check_commentary_facts(report, 'Overall: RED; Cost: RED.'))

    def test_commentary_cannot_invent_an_early_calendar_date(self):
        source = 'Unit testing target: Apr 30, 2027. No reliable finish date is estimated.'
        self.assertTrue(readout._check_commentary_facts(source, 'Projected closure: Jan 22, 2027.'))
        self.assertEqual(readout._check_commentary_facts(source, 'The planned target is April 30, 2027.'), [])
        self.assertEqual(readout._check_commentary_facts(source, 'The planned target is 2027-04-30.'), [])

    def test_planned_target_cannot_be_recast_as_a_predicted_finish(self):
        source = ('Effort-based milestone outlook: No reliable finish date is estimated. '
                  'Unit testing target: Apr 30, 2027.')
        for draft in ('Expected completion: Apr 30, 2027.', 'The project will finish by Apr 30, 2027.'):
            self.assertTrue(readout._check_commentary_facts(source, draft))
        self.assertEqual(readout._check_commentary_facts(source, 'Apr 30, 2027 is the planned target, not a predicted finish.'), [])

    def test_commentary_cannot_upgrade_unresolved_estimation_readiness(self):
        source = '**Estimation readiness:** Not ready.'
        self.assertTrue(readout._check_commentary_facts(source, 'Estimation readiness: Ready for model setup.'))
        self.assertEqual(readout._check_commentary_facts(source, 'Estimation readiness: Not ready; representative history is missing.'), [])

    @patch.object(readout, '_generate_model_step')
    def test_literal_source_statement_false_rejection(self, step):
        quote = 'No new positive change was identified from the available snapshots.'
        source = 'Overall: RED; Schedule: RED; Cost: RED.\n' + quote
        draft = 'Overall status is RED; Schedule remains RED; Cost: RED.\n' + quote
        bad = self.decision(approved=False, unsupported_claims=[quote])
        step.side_effect = [draft, bad]
        result = readout.generate_configured_commentary('ollama', 'llama3.2', source)
        self.assertEqual(result.review_passes, 1)

    def test_quote_reconciliation_preserves_other_findings(self):
        quote = 'No new positive change was identified from the available snapshots.'
        review = readout._parse_commentary_review(self.decision(
            approved=False, unsupported_claims=[quote, 'The budget has no risks.'],
            missing_aspects=['scope'], uncovered_risks=['budget gap'], risk_coverage='incomplete'))
        result = readout._reconcile_source_quotes(review, quote, quote)
        self.assertFalse(result['approved'])
        self.assertEqual(result['unsupported_claims'], ['The budget has no risks.'])
        # A source quote absent from the draft cannot overturn a finding.
        result = readout._reconcile_source_quotes(review, quote, 'different draft')
        self.assertEqual(len(result['unsupported_claims']), 2)

    @patch.object(readout.requests, 'post', side_effect=readout.requests.Timeout)
    def test_timeout_has_correct_diagnosis(self, post):
        with self.assertRaisesRegex(readout.OllamaReadoutError, 'timed out'):
            readout.generate_ollama_commentary('llama3.2', 'report')


if __name__ == '__main__':
    unittest.main()
