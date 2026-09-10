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
    def __init__(self, message, *, stage, process_name='', usage=None, reason='unknown'):
        super().__init__(message)
        self.stage = stage
        self.process_name = process_name
        self.usage = usage or {}
        self.reason = reason


def _object(properties, required):
    return {
        'type':'object',
        'additionalProperties':False,
        'properties':properties,
        'required':required,
    }


def _provenance_properties():
    return {
        'source_type':{'type':'string','enum':['explicit','derived','suggested']},
        'confidence':{'type':'number'},
        'evidence':{'type':'array','items':{'type':'string'}},
        'requires_confirmation':{'type':'boolean'},
    }


def company_map_schema():
    """A deliberately small contract for process discovery only."""
    provenance=_provenance_properties()
    operation_fields={
        'name':{'type':'string'},'objective':{'type':'string'},'scope':{'type':'string'},
        'trigger':{'type':'string'},'completion_definition':{'type':'string'},**provenance,
    }
    domain_fields={
        'id':{'type':'string'},'name':{'type':'string'},'summary':{'type':'string'},
        'objective':{'type':'string'},**provenance,
    }
    process_fields={
        'id':{'type':'string'},'name':{'type':'string'},'summary':{'type':'string'},
        'trigger':{'type':'string'},'completion':{'type':'string'},'owner':{'type':'string'},
        'domain_id':{'type':'string'},**provenance,
    }
    return _object(
        {
            'schema_version':{'type':'string','enum':['1.0']},
            'operation':_object(operation_fields,list(operation_fields)),
            'domains':{'type':'array','items':_object(domain_fields,list(domain_fields))},
            'processes':{'type':'array','items':_object(process_fields,list(process_fields))},
        },
        ['schema_version','operation','domains','processes'],
    )


def process_fragment_schema(kinds):
    """Small reusable grammar for one logical slice of a process."""
    element=copy.deepcopy(operational_grammar.schema()['properties']['elements']['items'])
    element['properties']['kind']['enum']=list(kinds)
    return _object(
        {
            'schema_version':{'type':'string','enum':['1.0']},
            'process_id':{'type':'string'},
            'elements':{'type':'array','items':element},
        },
        ['schema_version','process_id','elements'],
    )


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


def _call(client, *, model, effort, max_tokens, system_prompt, context, instruction, schema, cached=False):
    output_config={'format':{'type':'json_schema','schema':schema}}
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
        raise StageFailure(
            'Claude ha raggiunto il limite durante una fase della ricostruzione.',
            stage='request',usage=usage,reason='max_tokens',
        )
    if getattr(response, 'stop_reason', None) in {'refusal','model_context_window_exceeded'}:
        reason=str(getattr(response,'stop_reason'))
        raise StageFailure(
            'Claude ha interrotto la ricostruzione prima di produrre un risultato utilizzabile.',
            stage='request',usage=usage,reason=reason,
        )
    try:
        value=json.loads(text)
    except json.JSONDecodeError as exc:
        raise StageFailure(
            'Claude non ha completato una risposta strutturata.',
            stage='request',usage=usage,reason='invalid_json',
        ) from exc
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


def _map_to_grammar(payload):
    """Convert the compact discovery contract into grammar 2.0."""
    operation=copy.deepcopy(payload.get('operation') or {})
    result={'schema_version':'2.0','operation':operation,'elements':[]}
    domains=[]
    for item in payload.get('domains') or []:
        domain={
            'kind':'operational_domain','id':item.get('id'),'name':item.get('name'),
            'summary':item.get('summary'),'condition':'','action':item.get('objective'),
            'owner':'','details':[],'links':[],'order':0,'required':False,
            'source_type':item.get('source_type'),'confidence':item.get('confidence'),
            'evidence':item.get('evidence') or [],
            'requires_confirmation':bool(item.get('requires_confirmation')),
        }
        domains.append(domain)
        result['elements'].append(domain)
    domain_ids={item.get('id') for item in domains}
    for item in payload.get('processes') or []:
        domain_id=item.get('domain_id')
        result['elements'].append({
            'kind':'process','id':item.get('id'),'name':item.get('name'),
            'summary':item.get('summary'),'condition':item.get('trigger'),
            'action':item.get('completion'),'owner':item.get('owner'),'details':[],
            'links':[domain_id] if domain_id in domain_ids else [],'order':0,'required':False,
            'source_type':item.get('source_type'),'confidence':item.get('confidence'),
            'evidence':item.get('evidence') or [],
            'requires_confirmation':bool(item.get('requires_confirmation')),
        })
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
        # Every process fragment is generated in isolation. Keeping the parent
        # process link also on actors and systems preserves that scope in the
        # expanded memory and prevents apparently global, orphaned resources.
        if process_id not in links:
            links.append(process_id)
        item['links']=list(dict.fromkeys(links))
    result['elements']=elements
    return result


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
        raise StageFailure(
            'Il modello ricostruito non ha superato i controlli: '+'; '.join(errors[:4]),
            stage='validation',reason='validation',
        )
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
    pipeline_version=2
    state=(
        copy.deepcopy(checkpoint)
        if checkpoint and checkpoint.get('fingerprint')==fingerprint
        and checkpoint.get('pipeline_version')==pipeline_version
        else {}
    )
    state.setdefault('fingerprint',fingerprint)
    state.setdefault('pipeline_version',pipeline_version)
    state.setdefault('details',{})
    state.setdefault('partials',{})
    state.setdefault('usage',[])
    map_model=os.getenv('OPERATIONAL_MAP_MODEL','claude-haiku-4-5').strip() or detail_model
    map_tokens=_integer('OPERATIONAL_MAP_MAX_TOKENS',2800,1200,5000)
    flow_model=os.getenv('OPERATIONAL_FLOW_MODEL',map_model).strip() or map_model
    flow_tokens=_integer('OPERATIONAL_FLOW_MAX_TOKENS',5200,2200,7000)
    detail_tokens=_integer('OPERATIONAL_PROCESS_MAX_TOKENS',6500,2400,9000)
    workers=_integer('OPERATIONAL_PROCESS_CONCURRENCY',2,1,3)
    generation=context.get('generation') or {}
    single_name=(
        str(generation.get('process_name') or 'Processo').strip()[:160]
        if generation.get('scope')=='single_process' else ''
    )
    if not state.get('map'):
        progress({'phase':'map','label':'Sto identificando aree e processi supportati dalle fonti.'},state)
        if single_name:
            instruction=f'''FASE 1 — MAPPA DI UN SOLO PROCESSO. Ricostruisci esclusivamente «{single_name}». Restituisci una sintesi operativa, una sola area pertinente ed esattamente un processo. Non estrarre ancora attori, sistemi, campi, fasi, regole, eccezioni, escalation, metriche, lacune o domande. Descrivi in modo breve inizio, fine e responsabile; se il responsabile non è documentato usa una stringa vuota e richiedi conferma. Usa ID brevi e univoci. Mantieni ogni testo sotto 180 caratteri e usa al massimo una citazione breve come evidenza.'''
        else:
            instruction='''FASE 1 — MAPPA AZIENDALE COMPATTA. Identifica soltanto il perimetro dell’azienda: una sintesi operativa, da 1 a 4 aree e al massimo 6 processi realmente sostenuti dalle fonti. Non estrarre ancora attori, sistemi, campi, fasi, regole, eccezioni, escalation, metriche, lacune o domande. Per ogni processo descrivi in modo breve inizio, fine e responsabile; se il responsabile non è documentato usa una stringa vuota e richiedi conferma. Usa ID brevi e univoci. Non duplicare lo stesso processo in aree diverse. Mantieni ogni testo sotto 180 caratteri e usa al massimo una citazione breve come evidenza.'''
        retry_tokens=min(5000,max(map_tokens+1200,int(map_tokens*1.5)))
        limits=list(dict.fromkeys([map_tokens,retry_tokens]))
        raw_map=None
        usage={}
        for attempt,limit in enumerate(limits,1):
            try:
                raw_map,usage=_call(
                    client,model=map_model,effort=None,max_tokens=limit,
                    system_prompt=system_prompt,context=context,instruction=instruction,
                    schema=company_map_schema(),
                )
                break
            except StageFailure as exc:
                exc.stage='map'
                if exc.usage:
                    state['usage'].append({
                        'phase':'map_failed','attempt':attempt,'model':map_model,
                        'reason':exc.reason,**exc.usage,
                    })
                if exc.reason=='max_tokens' and attempt<len(limits):
                    progress({
                        'phase':'map_retry',
                        'label':'La prima mappa era troppo estesa: la ricompongo in forma più compatta.',
                    },state)
                    continue
                progress({
                    'phase':'map_failed','label':'La mappa non ha superato i controlli.',
                    'reason':exc.reason,
                },state)
                raise
        mapped=_ground(_cap_company_map(_map_to_grammar(raw_map),1 if single_name else 6),context)
        errors=operational_grammar.validate(mapped)
        processes=[item for item in mapped.get('elements',[]) if item.get('kind')=='process']
        if errors or not processes:
            detail='; '.join(errors[:3]) if errors else 'nessun processo identificato'
            failure=StageFailure(
                'Claude non ha identificato una mappa aziendale valida: '+detail,
                stage='map',usage=usage,reason='validation',
            )
            if usage:
                state['usage'].append({
                    'phase':'map_failed','model':map_model,'reason':'validation',**usage,
                })
            progress({'phase':'map_failed','label':'La mappa restituita non è valida.','reason':'validation'},state)
            raise failure
        state['map']=mapped;state['usage'].append({'phase':'map','model':map_model,**usage})
        progress({'phase':'map_complete','label':f'Mappa pronta: {len(processes)} processi da approfondire.','processes':[{'id':p['id'],'name':p['name'],'status':'pending'} for p in processes]},state)
    company_map=state['map']
    processes=[item for item in company_map['elements'] if item.get('kind')=='process']
    process_rows=[{'id':p['id'],'name':p['name'],'status':'complete' if p['id'] in state['details'] else 'pending'} for p in processes]
    pending=[p for p in processes if p['id'] not in state['details']]
    cache=True  # The flow call warms the common prefix for the controls call.
    flow_kinds=('case_type','actor','system','input','stage','outcome')
    control_kinds=(
        'decision_rule','exception','escalation','constraint','metric',
        'feedback_loop','ambiguity','missing_knowledge',
    )

    def detail(process):
        process_json=json.dumps(process,ensure_ascii=False,separators=(',',':'))
        requests=(
            (
                'flow',flow_kinds,flow_model,flow_tokens,
                f'''FASE 2A — FLUSSO DEL PROCESSO. Analizza esclusivamente:\n{process_json}\nRestituisci process_id={process['id']} e soltanto case_type, actor, system, input, stage e outcome sostenuti dalle fonti. Ricostruisci una sequenza end-to-end senza duplicazioni. Massimo 5 percorsi, 5 attori, 5 sistemi, 8 input, 9 fasi e 4 risultati. Sono limiti, non quantità da raggiungere. Usa gli ID degli input nei link delle fasi e collega tutti gli elementi al processo. Non inventare ruoli o sistemi. Nome massimo 80 caratteri; summary, condition e action massimo 180; massimo 3 dettagli e una sola citazione breve per elemento.''',
            ),
            (
                'controls',control_kinds,detail_model,detail_tokens,
                f'''FASE 2B — DECISIONI E CONTROLLI DEL PROCESSO. Analizza esclusivamente:\n{process_json}\nRestituisci process_id={process['id']} e soltanto decision_rule, exception, escalation, constraint, metric, feedback_loop, ambiguity e missing_knowledge sostenuti dalle fonti. Le regole devono avere semantica IF/THEN. Massimo 12 regole, 4 eccezioni, 3 escalation, 4 vincoli, 3 metriche, 2 cicli di miglioramento e complessivamente 6 ambiguità o conoscenze mancanti. Sono limiti, non quantità da raggiungere. Una raccolta vuota è corretta quando manca evidenza. Non inventare soglie; ciò che manca diventa una domanda concisa. Nome massimo 80 caratteri; summary, condition e action massimo 180; massimo 3 dettagli e una sola citazione breve per elemento.''',
            ),
        )
        fragments=[]
        usages=[]
        saved_parts=copy.deepcopy(state.get('partials',{}).get(process['id']) or {})
        for part,kinds,model,max_tokens,instruction in requests:
            if part in saved_parts:
                fragments.extend(saved_parts[part].get('elements') or [])
                continue
            retry_ceiling=7000 if part=='flow' else 9000
            retry_tokens=min(retry_ceiling,max(max_tokens+1400,int(max_tokens*1.35)))
            limits=list(dict.fromkeys([max_tokens,retry_tokens]))
            for attempt,limit in enumerate(limits,1):
                try:
                    value,usage=_call(
                        client,model=model,
                        # Haiku does not accept Anthropic's effort parameter. The
                        # control model (Sonnet by default) does and benefits from it.
                        effort=(
                            os.getenv('OPERATIONAL_MODEL_EFFORT','medium')
                            if model==detail_model else None
                        ),
                        max_tokens=limit,system_prompt=system_prompt,context=context,
                        instruction=instruction,schema=process_fragment_schema(kinds),cached=cache,
                    )
                    break
                except StageFailure as exc:
                    if exc.reason=='max_tokens' and attempt<len(limits):
                        usages.append({
                            'part':f'{part}_failed','attempt':attempt,'model':model,
                            'reason':exc.reason,**exc.usage,
                        })
                        instruction += (
                            '\nTENTATIVO COMPATTO: il primo output era troppo esteso. '
                            'Mantieni soltanto gli elementi indispensabili, elimina ripetizioni, '
                            'usa frasi brevi e non superare una evidenza per elemento.'
                        )
                        continue
                    exc.stage='process'
                    exc.process_name=process['name']
                    exc.model=model
                    exc.part=part
                    exc.prior_usage=usages
                    exc.partial_parts=saved_parts
                    raise
                except Exception as exc:
                    # Preserve the original provider exception type so the public
                    # error can still distinguish billing, rate limits and network
                    # failures, while retaining the completed process slice.
                    exc.stage='process'
                    exc.process_name=process['name']
                    exc.model=model
                    exc.part=part
                    exc.prior_usage=usages
                    exc.partial_parts=saved_parts
                    raise
            if value.get('process_id')!=process['id']:
                failure=StageFailure(
                    'Il dettaglio restituito non appartiene al processo richiesto.',
                    stage='process',process_name=process['name'],usage=usage,
                    reason='validation',
                )
                failure.part=part
                failure.prior_usage=usages
                failure.partial_parts=saved_parts
                raise failure
            saved_parts[part]=value
            fragments.extend(value.get('elements') or [])
            usages.append({'part':part,'model':model,**usage})
        fragment={'schema_version':'1.0','process_id':process['id'],'elements':fragments}
        try:
            _merge(company_map,{process['id']:fragment},context)
        except StageFailure as exc:
            exc.stage='process'
            exc.process_name=process['name']
            exc.prior_usage=usages
            raise
        return process,fragment,usages

    # Finish one request before parallel fan-out so subsequent calls can read the cache.
    failures=[]
    if pending:
        first=pending.pop(0)
        process_rows[next(i for i,r in enumerate(process_rows) if r['id']==first['id'])]['status']='processing'
        progress({'phase':'process','label':f"Approfondisco: {first['name']}",'processes':process_rows},state)
        try:
            process,value,usages=detail(first)
            state['details'][process['id']]=value
            state['partials'].pop(process['id'],None)
            for usage in usages:
                state['usage'].append({'phase':'process','process_id':process['id'],**usage})
            process_rows[next(i for i,r in enumerate(process_rows) if r['id']==process['id'])]['status']='complete'
            progress({'phase':'process_complete','label':f"Processo pronto: {process['name']}",'processes':process_rows},state)
        except StageFailure as exc:
            process_rows[next(i for i,r in enumerate(process_rows) if r['id']==first['id'])]['status']='failed'
            if getattr(exc,'partial_parts',None):
                state['partials'][first['id']]=exc.partial_parts
            for prior in getattr(exc,'prior_usage',[]):
                state['usage'].append({'phase':'process','process_id':first['id'],**prior})
            if exc.usage:state['usage'].append({'phase':'process_failed','process_id':first['id'],'model':getattr(exc,'model',detail_model),**exc.usage})
            failures.append(exc)
            progress({'phase':'process_failed','label':f"Da riprovare: {first['name']}",'processes':process_rows},state)
        except Exception as exc:  # Preserve other completed fragments before surfacing API failures.
            process_rows[next(i for i,r in enumerate(process_rows) if r['id']==first['id'])]['status']='failed'
            if getattr(exc,'partial_parts',None):
                state['partials'][first['id']]=exc.partial_parts
            for prior in getattr(exc,'prior_usage',[]):
                state['usage'].append({'phase':'process','process_id':first['id'],**prior})
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
                    process,value,usages=future.result()
                    state['details'][process['id']]=value
                    state['partials'].pop(process['id'],None)
                    for usage in usages:
                        state['usage'].append({'phase':'process','process_id':process['id'],**usage})
                    process_rows[next(i for i,r in enumerate(process_rows) if r['id']==process['id'])]['status']='complete'
                    progress({'phase':'process_complete','label':f"Processo pronto: {process['name']}",'processes':process_rows},state)
                except StageFailure as exc:
                    process_rows[next(i for i,r in enumerate(process_rows) if r['id']==expected['id'])]['status']='failed'
                    if getattr(exc,'partial_parts',None):
                        state['partials'][expected['id']]=exc.partial_parts
                    for prior in getattr(exc,'prior_usage',[]):
                        state['usage'].append({'phase':'process','process_id':expected['id'],**prior})
                    if exc.usage:state['usage'].append({'phase':'process_failed','process_id':expected['id'],'model':getattr(exc,'model',detail_model),**exc.usage})
                    failures.append(exc)
                    progress({'phase':'process_failed','label':f"Da riprovare: {expected['name']}",'processes':process_rows},state)
                except Exception as exc:  # Let sibling futures finish and checkpoint their output.
                    process_rows[next(i for i,r in enumerate(process_rows) if r['id']==expected['id'])]['status']='failed'
                    if getattr(exc,'partial_parts',None):
                        state['partials'][expected['id']]=exc.partial_parts
                    for prior in getattr(exc,'prior_usage',[]):
                        state['usage'].append({'phase':'process','process_id':expected['id'],**prior})
                    failures.append(exc)
                    progress({'phase':'process_failed','label':f"Da riprovare: {expected['name']}",'processes':process_rows},state)
    if failures:
        raise failures[0]
    ontology=_merge(company_map,state['details'],context)
    totals=_sum_usage(state['usage'])
    progress({'phase':'complete','label':'Ricostruzione completa e validata.','processes':process_rows,'usage':totals},state)
    pipeline='single_process_staged_v2' if single_name else 'map_then_processes_v2'
    return ontology,{'model':detail_model,'map_model':map_model,'calls':len(state['usage']),'stages':state['usage'],**totals,'structured_output':True,'pipeline':pipeline},state
