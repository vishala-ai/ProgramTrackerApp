import unittest

from streamlit.testing.v1 import AppTest


SETUP = """
import pandas as pd
from jira_dashboard.delivery_views import render_delivery_views

now = pd.Timestamp('2026-10-11 10:30', tz='America/Los_Angeles')
issues = pd.DataFrame([
    dict(key='KAN-1', summary='Overdue test', issue_type='Story', priority='High', status_category='In progress', status='In Progress', due_date='2026-10-10', resolved=None, assignee='Alex', url='https://example.com/browse/KAN-1', story_points=3),
    dict(key='KAN-2', summary='Completed test', issue_type='Bug', priority='Medium', status_category='Done', status='Done', due_date=None, resolved='2026-10-10T12:00:00Z', assignee='Sam', url='https://example.com/browse/KAN-2', story_points=None),
    dict(key='KAN-3', summary='Unspecified priority', issue_type='Story', priority=None, status_category='To do', status='To Do', due_date=None, resolved=None, assignee='Alex', url='https://example.com/browse/KAN-3', story_points=None),
])
issues['team'] = ['Credit', 'Credit', 'Collection']
"""


class DeliveryViewTests(unittest.TestCase):
    def run_app(self, body):
        app = AppTest.from_string(SETUP + body).run(timeout=15)
        self.assertEqual(len(app.exception), 0, list(app.exception))
        return app

    def test_details_and_compact_can_render_together_without_duplicate_ids(self):
        app = self.run_app("""
render_delivery_views(issues, key_prefix='team', as_of='Oct 11, 2026 10:30 PDT', resolution_dates_available=True, now=now, show_effort_outlook=True, milestone_due=pd.Timestamp('2027-04-30'))
render_delivery_views(issues, key_prefix='budget', as_of='Oct 11, 2026 10:30 PDT', resolution_dates_available=True, now=now, compact=True)
""")
        self.assertEqual([item.value for item in app.metric[:4]], ['1', '1', '1', '2'])
        self.assertEqual(len(app.dataframe), 3)
        actions = app.dataframe[0].value
        self.assertEqual(actions['Item'].tolist(), ['KAN-1'])
        self.assertEqual(actions['Overdue'].tolist(), ['Yes'])
        self.assertEqual([item.value for item in app.metric[4:7]], ['3', '1 of 2', '1'])
        self.assertTrue(any('Milestone target: Apr 30, 2027' in item.value and 'not a predicted finish date' in item.value for item in app.markdown))
        self.assertTrue(any('A reliable finish date is not available' in item.value for item in app.info))
        teams = app.dataframe[1].value.set_index('Team')
        self.assertEqual(teams.loc['Credit', 'Known remaining points'], '3')
        self.assertEqual(teams.loc['Collection', 'Known remaining points'], 'Unavailable')
        self.assertEqual(app.dataframe[2].value.iloc[-1]['Known completed points'], 'Unavailable')

    def test_unavailable_history_stays_unknown_in_effort_outlook(self):
        app = self.run_app("""
issues['resolved'] = None
render_delivery_views(issues, key_prefix='missing', as_of='Oct 11, 2026', resolution_dates_available=False, now=now, show_effort_outlook=True)
""")
        self.assertEqual(app.metric[2].value, 'Unavailable')
        self.assertTrue(any('resolution dates' in item.value for item in app.info))
        self.assertFalse(any('are projected to close around' in item.value for item in app.markdown))

    def test_known_zero_completions_is_distinct_from_missing_history(self):
        app = self.run_app("""
issues = issues.loc[issues['status_category'] != 'Done'].copy()
render_delivery_views(issues, key_prefix='zero', as_of='Oct 11, 2026', resolution_dates_available=True, now=now, show_effort_outlook=True)
""")
        self.assertEqual(app.metric[2].value, '0')
        self.assertTrue(any('A reliable finish date is not available' in item.value for item in app.info))
        self.assertTrue(app.dataframe[2].value['Known completed points'].eq('0').all())

    def test_partial_completion_dates_show_known_pace_and_coverage(self):
        app = self.run_app("""
issues.loc[len(issues)] = dict(issues.iloc[1], key='KAN-4', resolved=None)
render_delivery_views(issues, key_prefix='partial', as_of='Oct 11, 2026', resolution_dates_available=True, now=now, show_effort_outlook=True)
""")
        self.assertEqual(app.metric[2].value, '1')
        self.assertTrue(any('1 Done item lacks usable Jira resolution dates' in item.value for item in app.caption))
        self.assertTrue(any('missing completion dates limit coverage' in item.value for item in app.caption))
        self.assertFalse(any('are projected to close around' in item.value for item in app.markdown))

    def test_all_done_dates_missing_stays_unavailable_even_when_field_loaded(self):
        app = self.run_app("""
issues['resolved'] = None
render_delivery_views(issues, key_prefix='all-missing', as_of='Oct 11, 2026', resolution_dates_available=True, now=now, show_effort_outlook=True)
""")
        self.assertEqual(app.metric[2].value, 'Unavailable')
        self.assertTrue(any('Completion timing is unavailable for all Done items' in item.value for item in app.caption))
        self.assertFalse(any('are projected to close around' in item.value for item in app.markdown))

    def test_closed_backlog_is_not_presented_as_milestone_acceptance(self):
        app = self.run_app("""
issues = issues.loc[issues['status_category'] == 'Done'].copy()
render_delivery_views(issues, key_prefix='closed', as_of='Oct 11, 2026', resolution_dates_available=True, now=now, show_effort_outlook=True, milestone_due=pd.Timestamp('2027-04-30'))
""")
        self.assertTrue(any('does not establish milestone validation or acceptance sign-off' in item.value for item in app.info))
        self.assertEqual(len(app.metric), 4)

    def test_empty_scope_has_no_false_zero_metrics(self):
        app = self.run_app("""
render_delivery_views(pd.DataFrame(), key_prefix='empty', as_of='Unavailable', resolution_dates_available=False, now=now)
""")
        self.assertEqual(len(app.metric), 0)
        self.assertTrue(any('No Stories or Bugs' in item.value for item in app.info))


if __name__ == '__main__':
    unittest.main()
