"""Verify the new panels inside the complete app without contacting Jira or AI."""
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest


class AppForecastTests(unittest.TestCase):
    def test_full_forecast_stays_on_schedule_page_and_weekly_page_shows_summary(self):
        now=pd.Timestamp.now(tz='UTC')
        raw=[]
        for team,epic in [('finance_accounting','KAN-6'),('credit','KAN-8'),('collection','KAN-7')]:
            for index in range(2):
                raw.append({'key':f'KAN-{100+len(raw)}','_story_points':3 if index==0 else 13,'fields':{
                    'summary':'Offline integration fixture','issuetype':{'name':'Story'},
                    'status':{'name':'Done' if index==0 else 'To Do','statusCategory':{'key':'done' if index==0 else 'new'}},
                    'assignee':{'displayName':team+'1'},'priority':{'name':'Medium'},
                    'customfield_10001':{'name':team},'parent':{'key':epic},'labels':['SIMULATION'],
                    'created':(now-pd.Timedelta(days=30)).isoformat(),'updated':now.isoformat(),
                    'resolutiondate':(now-pd.Timedelta(days=1)).isoformat() if index==0 else None,
                }})
        inputs=dict(data_mode='simulation',error=None,
            milestone_scenario=dict(target_completion_pct=91,scope_growth_until='2026-10-30'),
            forecast=dict(enabled=True,trials=100),confirmations={
                key:dict(confirmed=True,owner='TPM',evidence='Simulation assumption')
                for key in ('milestone_scope','dependencies','capacity','remaining_work','testing_rework')})
        app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'),default_timeout=30)
        app.session_state['jira_token']={'access_token':'offline-test-only','expires_at':time.time()+3600}
        app.session_state['jira_sites']=[]
        app.session_state['jira_issues']=raw
        app.session_state['jira_last_sync']=now
        app.session_state['_jira_initial_refresh_attempted']=True
        with patch('jira_dashboard.client.fetch_project_issues',side_effect=AssertionError('No Jira calls')), \
             patch('jira_dashboard.client.fetch_accessible_sites',return_value=[]), \
             patch('jira_dashboard.readiness_inputs.load_estimation_inputs',return_value=inputs), \
             patch('jira_dashboard.readout._generate_model_step',side_effect=AssertionError('No AI on page load')):
            app.run()
        self.assertFalse(app.exception,list(app.exception))
        self.assertEqual(len(app.tabs),10)
        tables=[item.value for item in app.dataframe if '50% model date' in item.value.columns]
        self.assertEqual(len(tables),1)
        self.assertEqual(len(tables[0]),6)
        weekly_text='\n'.join(item.value for item in app.markdown)
        self.assertIn('Earliest modeled completion',weekly_text)
        self.assertIn('Latest modeled completion',weekly_text)
        self.assertIn('Forecast details are available on',weekly_text)


if __name__=='__main__':
    unittest.main()
