import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
import app
import company_memory as memory
import database
import onboarding_store


def process_fixture():
    return {'id':'proc-1','name':'Approvazione acquisti','area':'Amministrazione',
            'summary':'Il richiedente indica la spesa e il responsabile verifica il limite prima di autorizzarla.',
            'owner':'Responsabile acquisti','trigger':'Una richiesta di acquisto','completion':'Acquisto registrato',
            'status':'approved','steps':[{'id':'s1','title':'Verifica importo','action':'Controllare il totale','owner':'Responsabile acquisti','output':'Importo verificato'}],
            'fields':[{'id':'amount','label':'Importo','type':'number'}],
            'rules':[{'id':'r1','title':'Acquisti entro il limite','when':'Importo al massimo 200 euro',
                      'action':'Il responsabile può autorizzare la richiesta.', 'mode':'conditions',
                      'criteria':[{'field':'amount','operator':'lte','value':'200'}],
                      'source':'Procedura acquisti','evidence':'Il limite è 200 euro.', 'origin':'explicit','approved':True}],
            'gaps':[],'notes':''}


class CompanyMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old=os.environ.get('DATABASE_PATH')
        os.environ['DATABASE_PATH']=str(Path(self.temp.name)/'memory.db')
        database.init_database()
        self.ws=onboarding_store.create_workspace()
        onboarding_store.update_workspace(self.ws['id'],{'company_name':'Acme','status':'completed'})
        self.client=app.app.test_client()
        self.client.set_cookie('ops_workspace_id',self.ws['id'])
        self.initial=memory.state(self.ws['id'])
        self.doc=self.initial['draft']
        self.doc['processes']=[process_fixture()]

    def tearDown(self):
        if self.old is None:os.environ.pop('DATABASE_PATH',None)
        else:os.environ['DATABASE_PATH']=self.old
        self.temp.cleanup()

    def publish(self):
        state=memory.save(self.ws['id'],self.doc,memory.state(self.ws['id'])['revision'])
        return memory.publish(self.ws['id'],state['revision'],'Revisione verificata')

    def test_draft_is_never_used_as_published_authority(self):
        memory.save(self.ws['id'],self.doc,self.initial['revision'])
        with self.assertRaises(ValueError):memory.create_case(self.ws['id'],'proc-1','Acquisto di materiale')
        self.publish()
        case=memory.create_case(self.ws['id'],'proc-1','Acquisto di materiale')
        self.assertEqual('missing',case['evaluation']['status'])
        case=memory.update_case(self.ws['id'],case['id'],{'facts':{'amount':'150'}})
        self.assertEqual('ready',case['evaluation']['status'])
        case=memory.update_case(self.ws['id'],case['id'],{'facts':{'amount':'250'}})
        self.assertEqual('not_applicable',case['evaluation']['rules'][0]['status'])
        self.assertNotEqual('ready',case['evaluation']['status'])

    def test_versioned_case_and_academy_follow_publication(self):
        self.publish()
        old=memory.create_case(self.ws['id'],'proc-1','Acquisto di attrezzatura')
        self.doc['processes'][0]['rules'][0]['criteria'][0]['value']='100'
        self.doc['processes'][0]['rules'][0]['action']='Richiedere la nuova autorizzazione.'
        self.publish()
        old=memory.update_case(self.ws['id'],old['id'],{'facts':{'amount':150}})
        fresh=memory.create_case(self.ws['id'],'proc-1','Nuovo acquisto di attrezzatura')
        fresh=memory.update_case(self.ws['id'],fresh['id'],{'facts':{'amount':150}})
        self.assertEqual('ready',old['evaluation']['status'])
        self.assertEqual('human',fresh['evaluation']['status'])
        self.assertEqual(1,old['memory_version'])
        self.assertEqual(2,memory.academy(self.ws['id'])['version'])
        self.assertEqual('Richiedere la nuova autorizzazione.',memory.academy(self.ws['id'])['lessons'][0]['action'])

    def test_multiple_matching_actions_require_resolution(self):
        p=process_fixture()
        second=copy.deepcopy(p['rules'][0]);second.update(id='r2',action='Attendere il direttore.')
        p['rules'].append(second)
        self.assertEqual('conflict',memory.evaluate(p,{'amount':50})['status'])

    def test_matching_condition_never_overrides_a_human_review_rule(self):
        p=process_fixture()
        second=copy.deepcopy(p['rules'][0]);second.update(id='r2',action='Verificare il fornitore.',mode='human',criteria=[])
        p['rules'].append(second)
        self.assertEqual('human',memory.evaluate(p,{'amount':50})['status'])

    def test_false_condition_does_not_ask_irrelevant_information(self):
        p=process_fixture();p['fields'].append({'id':'country','label':'Paese','type':'text'})
        p['rules'][0]['criteria'].append({'field':'country','operator':'eq','value':'Italia'})
        result=memory.evaluate(p,{'amount':300})
        self.assertEqual([],result['missing'])

    def test_changes_in_facts_invalidate_approval(self):
        self.publish()
        c=memory.create_case(self.ws['id'],'proc-1','Richiesta materiale ufficio')
        c=memory.update_case(self.ws['id'],c['id'],{'facts':{'amount':50}})
        c=memory.update_case(self.ws['id'],c['id'],{'decision':'Autorizzato'})
        c=memory.update_case(self.ws['id'],c['id'],{'facts':{'amount':500}})
        self.assertIsNone(c['decision'])

    def test_full_feedback_loop_requires_review_and_publication(self):
        self.publish()
        c=memory.create_case(self.ws['id'],'proc-1','Acquisto con eccezione')
        with self.assertRaises(ValueError):memory.update_case(self.ws['id'],c['id'],{'outcome':'Completato'})
        c=memory.update_case(self.ws['id'],c['id'],{'facts':{'amount':50}})
        c=memory.update_case(self.ws['id'],c['id'],{'decision':'Autorizzo','feedback':'Manca il centro di costo.'})
        self.assertIsNone(c['outcome'])
        c=memory.update_case(self.ws['id'],c['id'],{'action_note':'Ordine inoltrato'})
        c=memory.update_case(self.ws['id'],c['id'],{'outcome':'Materiale ricevuto'})
        self.assertEqual(1,memory.insights(self.ws['id'])['outcomes'])
        self.assertEqual(1,len(memory.radar(self.ws['id'])))
        prop=memory.add_proposal(self.ws['id'],'proc-1','Centro di costo','Aggiungere il centro di costo alla raccolta.',[c['id']])[0]
        memory.resolve_proposal(self.ws['id'],prop['id'],'applied','Il responsabile verifica anche il centro di costo.')
        draft=memory.state(self.ws['id'])
        self.assertEqual('draft',draft['draft']['processes'][0]['status'])
        self.assertNotIn('centro di costo',memory.published(self.ws['id'])['document']['processes'][0]['notes'])
        draft['draft']['processes'][0]['status']='approved'
        saved=memory.save(self.ws['id'],draft['draft'],draft['revision'])
        memory.publish(self.ws['id'],saved['revision'],'Aggiunto controllo centro di costo')
        self.assertIn('centro di costo',memory.published(self.ws['id'])['document']['processes'][0]['notes'])
        self.assertEqual('published',memory.proposals(self.ws['id'])[0]['status'])

    def test_scope_enforced_on_cases_versions_and_proposals(self):
        self.publish();c=memory.create_case(self.ws['id'],'proc-1','Richiesta di acquisto')
        other=onboarding_store.create_workspace()
        onboarding_store.update_workspace(other['id'],{'company_name':'Altra azienda','status':'completed'})
        self.client.set_cookie('ops_workspace_id',other['id'])
        self.assertEqual(404,self.client.get('/api/ops/cases/'+c['id']).status_code)
        self.assertEqual(404,self.client.get('/api/memory/versions/1').status_code)
        self.assertEqual(0,memory.insights(other['id'])['total'])

    def test_invalid_predicates_and_stale_drafts_are_rejected(self):
        broken=copy.deepcopy(self.doc);broken['processes'][0]['rules'][0]['criteria'][0]['field']='unknown'
        with self.assertRaises(ValueError):memory.save(self.ws['id'],broken,1)
        memory.save(self.ws['id'],self.doc,1)
        with self.assertRaises(ValueError):memory.save(self.ws['id'],self.doc,1)

    def test_suggested_rules_are_not_executed(self):
        p=process_fixture();p['rules'][0]['approved']=False
        self.assertEqual([],memory.evaluate(p,{'amount':50})['rules'])
        with self.assertRaises(ValueError):memory.validate({'company':{'name':'Acme'},'processes':[p]})

    def test_pages_render_and_all_api_data_stays_in_workspace(self):
        for section in ('memory','assist','cases','analytics','radar','academy'):
            self.assertEqual(200,self.client.get('/workspace/'+section).status_code)
        self.assertEqual(200,self.client.get('/api/ops/context').status_code)

    def test_local_fact_detection_requires_confirmation(self):
        self.publish()
        c=memory.create_case(self.ws['id'],'proc-1','Richiedo materiale. Importo: 150')
        detected=memory.extract_facts(self.ws['id'],c['id'])
        self.assertEqual(150,detected['suggestions'][0]['value'])
        self.assertIn('Importo: 150',detected['suggestions'][0]['evidence'])
        self.assertEqual({},memory.get_case(self.ws['id'],c['id'])['facts'])
        self.assertEqual('missing',memory.get_case(self.ws['id'],c['id'])['evaluation']['status'])

    def test_academy_tests_a_real_boundary_and_rejects_stale_version(self):
        self.publish()
        lesson=memory.academy(self.ws['id'])['lessons'][0]
        self.assertEqual(201,lesson['quiz_facts'][0]['value'])
        self.assertEqual('review',lesson['correct_key'])
        self.assertFalse(memory.academy_answer(self.ws['id'],'proc-1','r1','follow',1)['correct'])
        self.assertTrue(memory.academy_answer(self.ws['id'],'proc-1','r1','review',1)['correct'])
        self.publish()
        with self.assertRaises(ValueError):memory.academy_answer(self.ws['id'],'proc-1','r1','review',1)

    def test_multi_process_import_preserves_links_sources_and_exceptions(self):
        model={'operational_grammar':{
            'processes':[{'id':'buy','name':'Acquisti'},{'id':'hire','name':'Assunzioni'}],
            'lifecycle':[{'id':'stage','name':'Valutazione candidato','links':['hire']}],
            'decision_rules':[{'id':'rule','name':'Limite','action':'Autorizzare', 'applies_to':['buy'],
                               'provenance':{'source_type':'explicit','evidence':['Limite 200 euro']}}],
            'exceptions':[{'id':'exception','trigger':'Importo superiore','handling':'Direzione','links':['buy']}],
            'metrics':[{'id':'metric','name':'Tempo di selezione','definition':'Giorni','links':['hire']}]
        }}
        doc=memory.from_onboarding({'company_name':'Acme'},model,[{'id':'src','name':'Acquisti.md','content':'Limite 200 euro'}])
        buy,hire=doc['processes']
        self.assertEqual([],buy['steps'])
        self.assertEqual([],hire['rules'])
        self.assertEqual('Acquisti.md',buy['rules'][0]['source'])
        self.assertIn('Direzione',buy['notes'])
        self.assertNotIn('Direzione',hire['notes'])
        self.assertIn('Tempo di selezione',hire['notes'])
        self.assertFalse(buy['rules'][0]['approved'])

    def test_malformed_api_documents_are_rejected(self):
        for bad in (None,[],{'company':{'name':'Acme'},'processes':[42]}):
            response=self.client.post('/api/memory',json={'document':bad,'revision':1})
            self.assertEqual(400,response.status_code)
        self.publish()
        case=memory.create_case(self.ws['id'],'proc-1','Richiesta acquisto')
        self.assertEqual(400,self.client.post('/api/ops/cases/'+case['id'],json={'facts':[]}).status_code)

    def test_unfinished_onboarding_cannot_create_an_empty_memory(self):
        other=onboarding_store.create_workspace()
        self.client.set_cookie('ops_workspace_id',other['id'])
        self.assertEqual(302,self.client.get('/memory').status_code)
        self.assertEqual(401,self.client.get('/api/memory').status_code)
        with database.session() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM company_memory WHERE workspace_id=?',(other['id'],)).fetchone())


if __name__=='__main__':unittest.main()
