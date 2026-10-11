import unittest

from streamlit.testing.v1 import AppTest


SETUP = """
import pandas as pd
from jira_dashboard.readiness_views import render_estimation_readiness
rows = pd.DataFrame([
    dict(input='Jira scope', status='Available', evidence='Current KAN Stories and Bugs', owner='TPM', next_step='Confirm milestone mapping'),
    dict(input='Milestone acceptance', status='Missing', evidence='No owner confirmation supplied', owner='Milestone owner', next_step='Record acceptance criteria and owner'),
    dict(input='Completion history', status='Partial', evidence='One week of recorded delivery', owner='Delivery lead', next_step='Review representative delivery history'),
])
assessment = dict(status='Not ready', forecast_available=False, rows=rows, blockers=['Milestone acceptance', 'Completion history'], counts=dict(available=1, total=3, blocking=2))
"""


class ReadinessViewTests(unittest.TestCase):
    def run_app(self, body):
        app = AppTest.from_string(SETUP + body).run(timeout=15)
        self.assertEqual(len(app.exception), 0, list(app.exception))
        return app

    def test_incomplete_inputs_are_visible_and_available_evidence_is_separate(self):
        app = self.run_app("""
render_estimation_readiness(assessment, key_prefix='schedule')
""")
        self.assertEqual([item.value for item in app.metric], ['1 of 3', '2'])
        self.assertEqual([item.label for item in app.metric], ['Real-delivery inputs available', 'Real-delivery evidence gaps'])
        self.assertEqual(app.dataframe[0].value['Input'].tolist(), ['Milestone acceptance', 'Completion history'])
        self.assertEqual(app.dataframe[1].value['Input'].tolist(), ['Jira scope'])
        self.assertTrue(any('Real-delivery status: **Not ready**' in item.value for item in app.info))
        self.assertTrue(any('No finish date is produced' in item.value for item in app.caption))
        self.assertTrue(any('do not by themselves block it' in item.value for item in app.caption))
        self.assertTrue(any('Evidence gaps for real-delivery forecasting' in item.value for item in app.markdown))
        self.assertEqual(app.dataframe[0].value.columns.tolist(), ['Input', 'Status', 'Evidence', 'Owner / role', 'Next step'])

    def test_simulation_only_history_remains_an_unresolved_input(self):
        app = self.run_app("""
assessment['rows'].loc[2, 'status'] = 'Simulation only'
assessment['rows'].loc[2, 'evidence'] = 'Synthetic Jira status transitions'
render_estimation_readiness(assessment, key_prefix='simulation')
""")
        visible = app.dataframe[0].value.set_index('Input')
        self.assertEqual(visible.loc['Completion history', 'Status'], 'Simulation only')
        self.assertTrue(any('Real-delivery status: **Not ready**' in item.value for item in app.info))
        self.assertTrue(any('conditional simulated forecast can run' in item.value for item in app.caption))
        self.assertFalse(any('Inputs to resolve before model setup' in item.value for item in app.markdown))
        self.assertEqual(len(app.success), 0)

    def test_ready_state_allows_model_setup_without_prediction_or_confidence(self):
        app = self.run_app("""
assessment['rows'].loc[1:, 'status'] = 'Owner-confirmed'
assessment['status'] = 'Ready for model setup'
assessment['blockers'] = []
assessment['counts'] = dict(available=3, total=3, blocking=0)
render_estimation_readiness(assessment, key_prefix='ready')
render_estimation_readiness(assessment, key_prefix='second-view')
""")
        self.assertEqual([item.value for item in app.metric[:2]], ['3 of 3', '0'])
        self.assertTrue(any('Real-delivery status: **Ready for model setup**' in item.value for item in app.info))
        self.assertEqual(len(app.success), 0)
        self.assertEqual(len(app.dataframe), 2)
        self.assertTrue(any('No finish date is produced' in item.value for item in app.caption))
        self.assertTrue(any('Owner-confirmed means' in item.value for item in app.caption))
        self.assertFalse(any('%' in item.value for item in app.metric))


if __name__ == '__main__':
    unittest.main()
