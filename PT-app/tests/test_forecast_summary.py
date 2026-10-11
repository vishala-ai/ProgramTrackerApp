import copy
import unittest

from jira_dashboard.forecast_summary import forecast_completion_range, format_forecast_summary


class ForecastSummaryTests(unittest.TestCase):
    def setUp(self):
        self.bundle = dict(status='available',published=True,review=dict(status='passed'),
            scenarios=[
                dict(p10_date='2027-05-02',p90_date='2027-07-04',growth_fraction=0),
                dict(p10_date='2027-04-25',p90_date='2027-05-23',growth_fraction=0),
                dict(p10_date='2027-07-11',p90_date='2027-08-22',growth_fraction=.25),
            ], evidence=dict(target_completion_pct=91,imputed_open_items=14,
                             scope_growth_until='2026-10-30',scope_growth_open=True))

    def test_range_uses_all_cases_rather_than_first_or_primary_case_only(self):
        self.assertEqual(forecast_completion_range(self.bundle),('2027-04-25','2027-08-22'))
        output='\n'.join(format_forecast_summary(self.bundle))
        self.assertIn('Earliest modeled completion:** Apr 25, 2027',output)
        self.assertIn('Latest modeled completion:** Aug 22, 2027',output)
        self.assertIn('not guaranteed completion dates',output)
        self.assertIn('Schedule Deep Dive',output)

    def test_censored_case_never_silently_caps_the_latest_completion_date(self):
        self.bundle['scenarios'][2]['p90_date']=None
        self.assertEqual(forecast_completion_range(self.bundle),('2027-04-25',None))
        output='\n'.join(format_forecast_summary(self.bundle))
        self.assertIn('Latest modeled completion:** Beyond simulation horizon / unavailable',output)
        self.assertNotIn('Jul 04, 2027',output)

    def test_all_censored_dates_remain_unavailable(self):
        for case in self.bundle['scenarios']:
            case['p10_date']=case['p90_date']=None
        self.assertEqual(forecast_completion_range(self.bundle),(None,None))

    def test_rejected_or_unpublished_results_never_expose_dates(self):
        for changes in (dict(status='unavailable',published=False),
                        dict(review=dict(status='rejected'))):
            with self.subTest(changes=changes):
                bundle=copy.deepcopy(self.bundle)
                bundle.update(changes)
                self.assertEqual(forecast_completion_range(bundle),(None,None))
                output='\n'.join(format_forecast_summary(bundle))
                self.assertNotIn('Apr 25, 2027',output)
                self.assertNotIn('Aug 22, 2027',output)
                self.assertIn('No reliable finish date is estimated',output)

    def test_brief_assumptions_keep_scope_effort_and_stage_distinctions(self):
        output='\n'.join(format_forecast_summary(self.bundle))
        self.assertIn('91% of Stories/Bugs in Done by item count',output)
        self.assertIn('full story-point effort for open work',output)
        self.assertIn('model-only assumptions for missing estimates',output)
        self.assertIn('0–25% scope-growth scenarios through Oct 30, 2026',output)
        self.assertIn('UAT and overall closure are excluded',output)
        self.assertNotIn('Unreviewed',output)

    def test_after_cutoff_does_not_claim_future_growth_or_verified_scope_freeze(self):
        self.bundle['evidence']['scope_growth_open']=False
        output='\n'.join(format_forecast_summary(self.bundle))
        self.assertIn('no further growth modeled after Oct 30, 2026, pending scope confirmation',output)
        self.assertNotIn('0–25%',output)


if __name__=='__main__':
    unittest.main()
