"""Cost-aware, resumable Anthropic reconstruction.

One small company map identifies the supported process boundaries. Each process
is then reconstructed independently. Completed fragments are checkpointed so a
retry pays only for unfinished work.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import operational_grammar


class StageFailure(ValueError):
    def __init__(self, message, *, stage, process_name='', usage=None):
        super().__init__(message)
        self.stage = stage
        self.process_name = process_name
        self.usage = usage or {}


def _integer(name, default, low, high):
    try:
        return max(low, min(high, int(os.getenv(name, default))))
    except (TypeError, ValueError):
        return default


def _fingerprint(context):
    stable = copy.deepcopy(context)
    stable.get('privacy', {}).pop('external_provider_used', None)
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _context_block(context, cached=False):
    block = {'type':'text', 'text':'CONTESTO AZIENDALE E FONTI REDATTE\n'+json.dumps(context, ensure_ascii=False, separators=(',', ':'))}
    if cached:
        block['cache_control'] = {'type':'ephemeral', 'ttl':'5m'}
    return block


def _usage(response):
    usage = response.usage
    return {
        'input_tokens':int(getattr(usage, 'input_tokens', 0) or 0),
        'output_tokens':int(getattr(usage, 'output_tokens', 0) or 0),
        'cache_read_input_tokens':int(getattr(usage, 'cache_read_input_tokens', 0) or 0),
        'cache_creation_input_tokens':int(getattr(usage, 'cache_creation_input_tokens', 0) or 0),
    }


def _call(client, *, model, effort, max_tokens, system_prompt, context, instruction, cached=False):
    output_config={'format':{'type':'json_schema','schema':operational_grammar.schema()}}
    if effort:
        output_config['effort']=effort
    request = {
        'model':model,
        'max_tokens':max_tokens,
        'system':system_prompt,
        'output_config':output_config,
        'messages':[{'role':'user','content':[_context_block(context,cached),{'type':'text','text':instruction}]}],
    }
    with client.messages.stream(**request) as stream:
        text = stream.get_final_text()
        response = stream.get_final_message()
    usage=_usage(response)
    if getattr(response, 'stop_reason', None) == 'max_tokens':
        raise StageFailure('Claude ha raggiunto il limite durante una fase della ricostruzione.',stage='max_tokens',usage=usage)
    try:
        value=json.loads(text)
    except json.JSONDecodeError as exc:
        raise StageFailure('Claude non ha completato una risposta strutturata.',stage='json',usage=usage) from exc
    return value, usage


def _normal(value):
    return ' '.join(str(value or '').split()).casefold()


def _ground(payload, context):
    """Unsupported verbatim claims become reviewable derivations."""
    corpus=_normal(json.dumps(context, ensure_ascii=False))
    result=copy.deepcopy(payload)
    for item in [result.get('operation') or {}, *(result.get('elements') or [])]:
        evidence=item.get('evidence') or []
        if item.get('source_type')=='explicit' and not any(_normal(quote) in corpus for quote in evidence if _normal(quote)):
            item['source_type']='derived'
            item['requires_confirmation']=True
            item['confidence']=min(float(item.get('confidence') or 0), .55)
    return result


def _cap_company_map(payload, limit=6):
    result=copy.deepcopy(payload)
    processes=[item for item in result.get('elements',[]) if item.get('kind')=='process']
    if len(processes)<=limit:
        return result
    omitted=processes[limit:]
    omitted_ids={item['id'] for item in omitted}
    result['elements']=[item for item in result['elements'] if item.get('id') not in omitted_ids]
    for item in result['elements']:
        item['links']=[link for link in item.get('links',[]) if link not in omitted_ids]
    result['elements'].append({
        'kind':'missing_knowledge','id':'MAP-REMAINING-PROCESSES','name':'Processi da approfondire successivamente',
        'summary':f'{len(omitted)} ulteriori processi sono stati rinviati per mantenere questa configurazione rapida e controllabile.',
        'condition':'','action':'Aggiungerli dalla Memoria Operativa, uno alla volta.','owner':'','details':[item['name'] for item in omitted[:3]],
        'links':[],'order':0,'required':False,'source_type':'suggested','confidence':1.0,'evidence':[],'requires_confirmation':True,
    })
    return result


def _namespace_fragment(payload, process_id, domain_ids):
    result=copy.deepcopy(payload)
    elements=[item for item in result.get('elements', []) if item.get('kind') not in {'process','operational_domain'}]
    mapping={item.get('id'):f"{process_id}::{item.get('id')}" for item in elements if item.get('id')}
    for item in elements:
        item['id']=mapping.get(item.get('id'),item.get('id'))
        links=[]
        for link in item.get('links') or []:
            translated=mapping.get(link,link)
            if translated in mapping.values() or translated==process_id or translated in domain_ids:
                links.append(translated)
        if item.get('kind') not in {'actor','system'} and process_id not in links:
            links.append(process_id)
        item['links']=list(dict.fromkeys(links))
    result['elements']=elements
    return result


def _normalise_single(payload, context):
    """Make one-process output deterministic before semantic validation."""
    result=_ground(payload,context)
    elements=result.get('elements') or []
    processes=[item for item in elements if item.get('kind')=='process']
    if len(processes)!=1:
        return result, processes
    process_id=processes[0]['id']
    known={item.get('id') for item in elements}
    domain_ids={item.get('id') for item in elements if item.get('kind')=='operational_domain'}
    for item in elements:
        links=[link for link in item.get('links',[]) if link in known]
        if item.get('kind')=='process':
            links=[link for link in links if link in domain_ids]
        elif item.get('kind')!='operational_domain' and process_id not in links:
            links.append(process_id)
        item['links']=list(dict.fromkeys(links))
    for index,item in enumerate((x for x in elements if x.get('kind')=='stage'),1):
        item['order']=index
    for index,item in enumerate((x for x in elements if x.get('kind')=='decision_rule'),1):
        item['order']=index
    return result, processes


def _merge(company_map, fragments, context):
    merged=copy.deepcopy(company_map)
    merged['elements']=[item for item in merged.get('elements', []) if item.get('kind') in {'operational_domain','process','actor','system','ambiguity','missing_knowledge'}]
    domain_ids={item['id'] for item in merged['elements'] if item.get('kind')=='operational_domain'}
    for process in [item for item in merged['elements'] if item.get('kind')=='process']:
        fragment=_namespace_fragment(fragments.get(process['id'], {'elements':[]}),process['id'],domain_ids)
        merged['elements'].extend(fragment['elements'])
    # The transport requires one global order even though each process is local.
    for index,item in enumerate((x for x in merged['elements'] if x.get('kind')=='stage'),1):
        item['order']=index
    for index,item in enumerate((x for x in merged['elements'] if x.get('kind')=='decision_rule'),1):
        item['order']=index
    known={item.get('id') for item in merged['elements']}
    for item in merged['elements']:
        item['links']=[link for link in item.get('links',[]) if link in known]
    merged=_ground(merged,context)
    errors=operational_grammar.validate(merged)
    if errors:
        raise StageFailure('Il modello ricostruito non ha superato i controlli: '+'; '.join(errors[:4]),stage='validation')
    return merged


def _sum_usage(rows):
    result={'input_tokens':0,'output_tokens':0,'cache_read_input_tokens':0,'cache_creation_input_tokens':0}
    for row in rows:
        for key in result: result[key]+=int(row.get(key,0) or 0)
    return result


def build(client, context, system_prompt, detail_model, *, progress=None, checkpoint=None):
    """Return a complete grammar plus aggregate real usage and checkpoint."""
    progress=progress or (lambda update, state: None)
    fingerprint=_fingerprint(context)
    state=copy.deepcopy(checkpoint) if checkpoint and checkpoint.get('fingerprint')==fingerprint else {}
    state.setdefault('fingerprint',fingerprint);state.setdefault('details',{});state.setdefault('usage',[])
    map_model=os.getenv('OPERATIONAL_MAP_MODEL','claude-haiku-4-5').strip() or detail_model
    map_tokens=_integer('OPERATIONAL_MAP_MAX_TOKENS',2800,1200,5000)
    detail_tokens=_integer('OPERATIONAL_PROCESS_MAX_TOKENS',5200,2400,8000)
    workers=_integer('OPERATIONAL_PROCESS_CONCURRENCY',2,1,3)
    generation=context.get('generation') or {}
    if generation.get('scope')=='single_process':
        name=str(generation.get('process_name') or 'Processo').strip()[:160]
        progress({'phase':'process','label':f'Ricostruisco: {name}','processes':[{'id':'single','name':name,'status':'processing'}]},state)
        instruction=f'''RICOSTRUZIONE DI UN SINGOLO PROCESSO. Ricostruisci esclusivamente «{name}». Restituisci una grammatica 2.0 completa ma concisa: una sola operational_domain se documentata, esattamente un process, quindi case_type, actor, system, input, stage, decision_rule, exception, escalation, constraint, outcome, metric, feedback_loop, ambiguity e missing_knowledge pertinenti. Collega tutti gli elementi specifici all’ID del processo. Massimo 10 fasi e 16 regole, senza duplicazioni. Non inventare soglie, responsabilità o sistemi. Le informazioni non supportate diventano domande, non regole.'''
        try:
            ontology,usage=_call(client,model=detail_model,effort=os.getenv('OPERATIONAL_MODEL_EFFORT','medium'),max_tokens=detail_tokens,system_prompt=system_prompt,context=context,instruction=instruction)
            ontology,processes=_normalise_single(ontology,context)
            if len(processes)!=1:
                raise StageFailure('La ricostruzione deve contenere un solo processo.',stage='process',process_name=name,usage=usage)
            errors=operational_grammar.validate(ontology)
            if errors:raise StageFailure('Il processo non ha superato i controlli: '+'; '.join(errors[:4]),stage='process',process_name=name,usage=usage)
        except StageFailure as exc:
            exc.stage='process';exc.process_name=name;raise
        state['usage'].append({'phase':'process','process_id':processes[0]['id'],'model':detail_model,**usage})
        progress({'phase':'complete','label':f'Processo pronto: {name}','processes':[{'id':processes[0]['id'],'name':name,'status':'complete'}]},state)
        totals=_sum_usage(state['usage'])
        return ontology,{'model':detail_model,'calls':len(state['usage']),'processes':1,'stages':state['usage'],**totals,'structured_output':True,'pipeline':'single_process_v1'},state
    if not state.get('map'):
        progress({'phase':'map','label':'Sto identificando aree e processi supportati dalle fonti.'},state)
        instruction='''FASE 1 — MAPPA AZIENDALE. Restituisci una grammatica 2.0 molto compatta. In elements includi soltanto: operational_domain, process, actor, system, ambiguity e missing_knowledge. Identifica al massimo 6 processi realmente supportati. Non produrre ancora fasi, input, regole, eccezioni, escalation, metriche o feedback loop. Ogni processo deve collegarsi alla propria area e avere confini, responsabile, inizio e completamento specifici; usa vuoto e conferma richiesta quando il dato manca.'''
        try:
            mapped,usage=_call(client,model=map_model,effort=None,max_tokens=map_tokens,system_prompt=system_prompt,context=context,instruction=instruction)
        except StageFailure as exc:
            exc.stage='map';raise
        mapped=_ground(_cap_company_map(mapped),context)
        errors=operational_grammar.validate(mapped)
        processes=[item for item in mapped.get('elements',[]) if item.get('kind')=='process']
        if errors or not processes:
            raise StageFailure('Claude non ha identificato una mappa aziendale valida: '+'; '.join(errors[:3]),stage='map')
        state['map']=mapped;state['usage'].append({'phase':'map','model':map_model,**usage})
        progress({'phase':'map_complete','label':f'Mappa pronta: {len(processes)} processi da approfondire.','processes':[{'id':p['id'],'name':p['name'],'status':'pending'} for p in processes]},state)
    company_map=state['map']
    processes=[item for item in company_map['elements'] if item.get('kind')=='process']
    process_rows=[{'id':p['id'],'name':p['name'],'status':'complete' if p['id'] in state['details'] else 'pending'} for p in processes]
    pending=[p for p in processes if p['id'] not in state['details']]
    cache=len(processes)>1

    def detail(process):
        instruction=f'''FASE 2 — PROCESSO SINGOLO. Approfondisci esclusivamente questo processo già identificato:\n{json.dumps(process,ensure_ascii=False,separators=(',',':'))}\nRestituisci operation ed esattamente un elemento process con lo stesso ID {process['id']}, seguito solo dagli elementi specifici e documentati di questo processo: case_type, actor, system, input, stage, decision_rule, exception, escalation, constraint, outcome, metric, feedback_loop, ambiguity e missing_knowledge. Collega ogni elemento operativo al processo {process['id']}. Produci una sequenza end-to-end non ripetitiva e regole IF/THEN. Mantieni l’output conciso: massimo 10 fasi e 16 regole, senza duplicare la stessa informazione. Non inventare soglie, ruoli o sistemi.'''
        try:
            value,usage=_call(client,model=detail_model,effort=os.getenv('OPERATIONAL_MODEL_EFFORT','medium'),max_tokens=detail_tokens,system_prompt=system_prompt,context=context,instruction=instruction,cached=cache)
        except StageFailure as exc:
            exc.stage='process';exc.process_name=process['name'];raise
        _merge(company_map,{process['id']:value},context)
        return process,value,usage

    # Finish one request before parallel fan-out so subsequent calls can read the cache.
    failures=[]
    if pending:
        first=pending.pop(0)
        process_rows[next(i for i,r in enumerate(process_rows) if r['id']==first['id'])]['status']='processing'
        progress({'phase':'process','label':f"Approfondisco: {first['name']}",'processes':process_rows},state)
        try:
            process,value,usage=detail(first)
            state['details'][process['id']]=value;state['usage'].append({'phase':'process','process_id':process['id'],'model':detail_model,**usage})
            process_rows[next(i for i,r in enumerate(process_rows) if r['id']==process['id'])]['status']='complete'
            progress({'phase':'process_complete','label':f"Processo pronto: {process['name']}",'processes':process_rows},state)
        except StageFailure as exc:
            process_rows[next(i for i,r in enumerate(process_rows) if r['id']==first['id'])]['status']='failed'
            if exc.usage:state['usage'].append({'phase':'process_failed','process_id':first['id'],'model':detail_model,**exc.usage})
            failures.append(exc)
            progress({'phase':'process_failed','label':f"Da riprovare: {first['name']}",'processes':process_rows},state)
        except Exception as exc:  # Preserve other completed fragments before surfacing API failures.
            process_rows[next(i for i,r in enumerate(process_rows) if r['id']==first['id'])]['status']='failed'
            failures.append(exc)
            progress({'phase':'process_failed','label':f"Da riprovare: {first['name']}",'processes':process_rows},state)
    if pending:
        for row in process_rows:
            if row['id'] in {p['id'] for p in pending}:row['status']='processing'
        progress({'phase':'process','label':f'Approfondisco {len(pending)} processi in parallelo.','processes':process_rows},state)
        with ThreadPoolExecutor(max_workers=min(workers,len(pending))) as pool:
            futures={pool.submit(detail,p):p for p in pending}
            for future in as_completed(futures):
                expected=futures[future]
                try:
                    process,value,usage=future.result()
                    state['details'][process['id']]=value;state['usage'].append({'phase':'process','process_id':process['id'],'model':detail_model,**usage})
                    process_rows[next(i for i,r in enumerate(process_rows) if r['id']==process['id'])]['status']='complete'
                    progress({'phase':'process_complete','label':f"Processo pronto: {process['name']}",'processes':process_rows},state)
                except StageFailure as exc:
                    process_rows[next(i for i,r in enumerate(process_rows) if r['id']==expected['id'])]['status']='failed'
                    if exc.usage:state['usage'].append({'phase':'process_failed','process_id':expected['id'],'model':detail_model,**exc.usage})
                    failures.append(exc)
                    progress({'phase':'process_failed','label':f"Da riprovare: {expected['name']}",'processes':process_rows},state)
                except Exception as exc:  # Let sibling futures finish and checkpoint their output.
                    process_rows[next(i for i,r in enumerate(process_rows) if r['id']==expected['id'])]['status']='failed'
                    failures.append(exc)
                    progress({'phase':'process_failed','label':f"Da riprovare: {expected['name']}",'processes':process_rows},state)
    if failures:
        raise failures[0]
    ontology=_merge(company_map,state['details'],context)
    totals=_sum_usage(state['usage'])
    progress({'phase':'complete','label':'Ricostruzione completa e validata.','processes':process_rows,'usage':totals},state)
    return ontology,{'model':detail_model,'map_model':map_model,'calls':len(state['usage']),'stages':state['usage'],**totals,'structured_output':True,'pipeline':'map_then_processes_v1'},state
