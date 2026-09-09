"""Workspace-scoped UI and API for the company knowledge loop."""
from flask import Blueprint, jsonify, redirect, render_template, request
import company_memory as memory
import onboarding_store
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import context_privacy
import operational_model_service
import policy_import

bp = Blueprint('memory', __name__)
jobs = {}
job_lock = Lock()
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='memory-process')

def workspace():
    result = onboarding_store.get_workspace(request.cookies.get('ops_workspace_id'))
    if not result:
        raise PermissionError('Avvia prima la configurazione della tua organizzazione.')
    if result.get('status') != 'completed':
        raise PermissionError('Completa la configurazione prima di aprire la Memoria Operativa.')
    return result

@bp.errorhandler(PermissionError)
def permission_error(exc):
    return jsonify(errore=str(exc)), 401

@bp.errorhandler(ValueError)
def validation_error(exc):
    return jsonify(errore=str(exc)), 400

@bp.errorhandler(KeyError)
def missing_error(exc):
    return jsonify(errore='Elemento non trovato in questa organizzazione.'), 404

@bp.get('/memory')
@bp.get('/workspace/<section>')
def page(section='memory'):
    if section not in {'memory', 'assist', 'cases', 'analytics', 'radar', 'academy'}:
        return 'Pagina non trovata', 404
    try:
        company = workspace()
    except PermissionError:
        return redirect('/onboarding')
    titles = {'memory': 'Memoria Operativa', 'assist': 'Area operativa', 'cases': 'Casi', 'analytics': 'Analisi', 'radar': 'Radar', 'academy': 'Academy'}
    return render_template('company_memory.html', section=section, section_title=titles[section], memory_state=memory.state(company['id']))

@bp.get('/api/memory')
def get_memory():
    return jsonify(memory.state(workspace()['id']))

@bp.post('/api/memory')
def save_memory():
    data = request.get_json() or {}
    return jsonify(memory.save(workspace()['id'], data.get('document'), data.get('revision')))

@bp.post('/api/memory/publish')
def publish_memory():
    data = request.get_json() or {}
    return jsonify(memory.publish(workspace()['id'], data.get('revision'), data.get('note')))

@bp.get('/api/memory/versions/<int:version>')
def version_memory(version):
    result = memory.published(workspace()['id'], version)
    if not result:
        raise KeyError(version)
    return jsonify(result)

@bp.get('/api/ops/context')
def ops_context():
    wid = workspace()['id']
    return jsonify(published=memory.published(wid), cases=memory.cases(wid), proposals=memory.proposals(wid), signals=memory.radar(wid), academy=memory.academy(wid), analytics=memory.insights(wid))

@bp.post('/api/ops/cases')
def create_case():
    data = request.get_json() or {}
    return jsonify(memory.create_case(workspace()['id'], data.get('process_id'), data.get('message')))

@bp.get('/api/ops/cases/<cid>')
def get_case(cid):
    return jsonify(memory.get_case(workspace()['id'], cid))

@bp.post('/api/ops/cases/<cid>')
def update_case(cid):
    return jsonify(memory.update_case(workspace()['id'], cid, request.get_json() or {}))

@bp.post('/api/ops/proposals')
def create_proposal():
    data = request.get_json() or {}
    return jsonify(memory.add_proposal(workspace()['id'], data.get('process_id'), data.get('title'), data.get('suggestion'), data.get('cases') or []))

@bp.post('/api/ops/proposals/<pid>')
def resolve_proposal(pid):
    data = request.get_json() or {}
    return jsonify(memory.resolve_proposal(workspace()['id'], pid, data.get('status'), data.get('resolution')))

@bp.post('/api/ops/academy')
def academy_answer():
    data = request.get_json() or {}
    return jsonify(memory.academy_answer(workspace()['id'], data.get('process_id'), data.get('rule_id'), data.get('answer'), data.get('version')))

def reconstruct_job(job_id, company, name, sources):
    try:
        operation = {'description': f'Processo specifico: {name}. Ricostruisci soltanto le attività presenti nelle fonti.',
                     'objective': f'Documentare in modo verificabile il processo {name}.',
                     'current_process': company.get('memory_context') or ''}
        prepared = context_privacy.prepare_operational_context(company, operation, sources)
        result = operational_model_service.get_operational_model_service().build(prepared)
        doc = memory.from_onboarding(company, result['model'], sources)
        if not doc['processes']:
            raise ValueError('Le fonti non descrivono ancora un processo. Aggiungi attività e responsabilità.')
        with job_lock:
            jobs[job_id].update(status='complete', processes=doc['processes'], sources=doc['sources'])
    except Exception as exc:
        _, message = operational_model_service.public_provider_error(exc)
        with job_lock:
            jobs[job_id].update(status='failed', error=message)

@bp.post('/api/memory/reconstruct')
def reconstruct():
    company = workspace()
    # Manual edits in the current memory supersede the old onboarding answers.
    current = memory.state(company['id'])['draft']['company']
    company = {**company, 'company_name':current.get('name') or '',
               'company_description':current.get('description') or '',
               'derived_context':{'core_business':current.get('description') or '',
                                  'operational_activities':current.get('activities') or '',
                                  'operational_challenges':current.get('challenges') or ''},
               'memory_context':'\n'.join(f'{key}: {current.get(key) or ""}' for key in ('goals','roles','systems','principles'))}
    name = str(request.form.get('name') or '').strip()[:160]
    text = str(request.form.get('text') or '').strip()
    if len(name) < 3:
        raise ValueError('Inserisci il nome del processo.')
    sources = []
    if text:
        sources.append({'id': memory.uid('SRC'), 'name': 'Note: '+name, 'source_type':'text', 'content':text})
    upload = request.files.get('file')
    if upload and upload.filename:
        content = policy_import.document_text(upload.filename, upload.read(policy_import.MAX_DOCUMENT_BYTES+1), upload.mimetype or '')
        sources.append({'id':memory.uid('SRC'), 'name':upload.filename, 'source_type':'document', 'content':content})
    if sum(len(s['content']) for s in sources) < 40:
        raise ValueError('Aggiungi un documento o almeno qualche frase sul processo.')
    if sum(len(s['content']) for s in sources) > context_privacy.MAX_SOURCE_CHARACTERS:
        raise ValueError('Per questa ricostruzione usa al massimo 24.000 caratteri. Suddividi i documenti per processo.')
    with job_lock:
        if any(j['workspace_id']==company['id'] and j['status']=='processing' for j in jobs.values()):
            raise ValueError('Una ricostruzione è già in corso per questa organizzazione.')
        # Bound temporary results; persistent knowledge is saved only after review.
        if len(jobs)>100:
            for key in list(jobs):
                if jobs[key]['status']!='processing':
                    del jobs[key]
        job_id=memory.uid('JOB')
        jobs[job_id]={'id':job_id, 'workspace_id':company['id'], 'status':'processing'}
    executor.submit(reconstruct_job,job_id,company,name,sources)
    return jsonify(id=job_id,status='processing'),202

@bp.get('/api/memory/reconstruct/<job_id>')
def reconstruct_status(job_id):
    wid=workspace()['id']
    with job_lock:
        job=jobs.get(job_id)
        if not job or job['workspace_id']!=wid:
            raise KeyError(job_id)
        return jsonify({k:v for k,v in job.items() if k!='workspace_id'})

def extraction_job(job_id,wid,cid):
    try:
        result=memory.extract_facts(wid,cid,use_ai=True)
        with job_lock:jobs[job_id].update(status='complete',**result)
    except Exception as exc:
        _, message=operational_model_service.public_provider_error(exc)
        with job_lock:jobs[job_id].update(status='failed',error=message)

@bp.post('/api/ops/cases/<cid>/extract')
def extract_case_facts(cid):
    wid=workspace()['id']
    memory.get_case(wid,cid)
    if not (request.get_json(silent=True) or {}).get('use_ai'):
        return jsonify(memory.extract_facts(wid,cid))
    with job_lock:
        if any(j['workspace_id']==wid and j['status']=='processing' for j in jobs.values()):
            raise ValueError('Una richiesta è già in corso. Attendi il completamento.')
        job_id=memory.uid('JOB')
        jobs[job_id]={'id':job_id,'workspace_id':wid,'status':'processing'}
    executor.submit(extraction_job,job_id,wid,cid)
    return jsonify(id=job_id,status='processing'),202
