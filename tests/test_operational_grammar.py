import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import operational_grammar
import anthropic_staged
from operational_model_service import (
    AnthropicOperationalModelService,
    LocalOperationalModelService,
    ResilientOperationalModelService,
    get_operational_model_service,
    public_provider_error,
)


def element(kind, item_id, name, **changes):
    value = {
        "kind": kind,
        "id": item_id,
        "name": name,
        "summary": "Operational definition",
        "condition": "When the documented trigger occurs",
        "action": "Follow the documented action",
        "owner": "Process owner",
        "details": [],
        "links": [],
        "order": 0,
        "required": False,
        "source_type": "derived",
        "confidence": 0.8,
        "evidence": ["Supplied operating notes"],
        "requires_confirmation": True,
    }
    value.update(changes)
    return value


def payload():
    return {
        "schema_version": "2.0",
        "operation": {
            "name": "Client Process Redesign",
            "objective": "Deliver a validated and measurable target operating model.",
            "scope": "One client business process",
            "trigger": "A sponsor starts an engagement",
            "completion_definition": "The target process is launched and monitored",
            "source_type": "explicit",
            "confidence": 1.0,
            "evidence": ["Transform the client business process"],
            "requires_confirmation": False,
        },
        "elements": [
            element("case_type", "CASE-01", "Process redesign engagement"),
            element("actor", "ACT-01", "Consultant", details=["Map the current process"]),
            element("system", "SYS-01", "SOP repository"),
            element("input", "IN-01", "Existing procedures", required=True),
            element("stage", "STG-01", "Discovery", order=1, links=["IN-01"]),
            element("decision_rule", "RULE-01", "Validation gate", order=1, condition="The future process is ready", action="Obtain process owner validation"),
            element("escalation", "ESC-01", "Ownership conflict"),
            element("outcome", "OUT-01", "Validated target model"),
            element("metric", "MET-01", "Cycle time"),
        ],
    }


def context():
    return {
        "company": {
            "name": "Example Company",
            "description": "A consultancy improving internal operations.",
            "industry": "Professional services",
            "markets": ["Italy"],
            "business_model": "B2B",
            "team_size": "11-50",
        },
        "operation": {
            "name": "",
            "description": "Redesign a client business process.",
            "objective": "Deliver a validated and measurable target operating model.",
            "current_process": "Discover, map, design, validate and monitor.",
        },
        "knowledge_sources": [{"id": "SRC-01", "name": "Notes", "content": "The process owner validates the design."}],
        "privacy": {"redactions": 0, "external_provider_used": True},
    }


def map_payload():
    return {
        'schema_version':'1.0',
        'operation':payload()['operation'],
        'domains':[
            {
                'id':'DOMAIN-01','name':'Operations','summary':'Operational work',
                'objective':'Deliver consistent operations','source_type':'derived',
                'confidence':.8,'evidence':['Supplied operating notes'],
                'requires_confirmation':True,
            },
        ],
        'processes':[
            {
                'id':'PROCESS-01','name':'Client process redesign',
                'summary':'Map and redesign a client process',
                'trigger':'A sponsor starts an engagement',
                'completion':'The target process is launched','owner':'Process owner',
                'domain_id':'DOMAIN-01','source_type':'derived','confidence':.8,
                'evidence':['Supplied operating notes'],'requires_confirmation':True,
            },
        ],
    }


def detail_payload(process_id='PROCESS-01', part='flow'):
    kinds={
        'flow':{'case_type','actor','system','input','stage','outcome'},
        'controls':{'decision_rule','exception','escalation','constraint','metric','feedback_loop','ambiguity','missing_knowledge'},
    }[part]
    return {
        'schema_version':'1.0',
        'process_id':process_id,
        'elements':[item for item in payload()['elements'] if item['kind'] in kinds],
    }


def stream_for(value, input_tokens, output_tokens, *, cache_read=0, cache_creation=0, stop_reason='end_turn'):
    response=SimpleNamespace(
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens,output_tokens=output_tokens,
                              cache_read_input_tokens=cache_read,cache_creation_input_tokens=cache_creation),
    )
    stream=MagicMock();stream.get_final_text.return_value=json.dumps(value);stream.get_final_message.return_value=response
    manager=MagicMock();manager.__enter__.return_value=stream
    return manager


class OperationalGrammarTests(unittest.TestCase):
    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=True)
    def test_api_key_activates_anthropic_without_redundant_provider_setting(self):
        self.assertIsInstance(get_operational_model_service(), ResilientOperationalModelService)

    @patch.dict("os.environ", {}, clear=True)
    def test_no_api_key_keeps_the_free_local_provider(self):
        self.assertIsInstance(get_operational_model_service(), LocalOperationalModelService)

    @patch.dict("os.environ", {}, clear=True)
    def test_provider_errors_are_not_silently_replaced_by_local_output(self):
        primary = MagicMock()
        primary.build.side_effect = ValueError("invalid structured output")
        service = ResilientOperationalModelService(primary=primary, fallback=LocalOperationalModelService())
        with self.assertRaisesRegex(ValueError, "invalid structured output"):
            service.build(context())

    @patch.dict("os.environ", {"OPERATIONAL_MODEL_ALLOW_LOCAL_FALLBACK": "true"}, clear=True)
    def test_local_fallback_requires_explicit_configuration(self):
        primary = MagicMock()
        primary.build.side_effect = ValueError("invalid structured output")
        service = ResilientOperationalModelService(primary=primary, fallback=LocalOperationalModelService())
        model = service.build(context())["model"]
        self.assertEqual("local_evidence_extractor_after_provider_error", model["provider"])

    def test_provider_validation_error_has_safe_public_message(self):
        code, message = public_provider_error(ValueError("Invalid operational grammar"))
        self.assertEqual("invalid_model", code)
        self.assertIn("non ha superato la validazione", message)

    def test_map_token_limit_has_a_precise_public_message(self):
        failure=anthropic_staged.StageFailure(
            'token limit',stage='map',reason='max_tokens',
            usage={'output_tokens':2800},
        )
        code,message=public_provider_error(failure)
        self.assertEqual('map_token_limit',code)
        self.assertIn('secondo tentativo compatto',message)

    def test_process_token_limit_identifies_the_failed_section(self):
        failure=anthropic_staged.StageFailure(
            'token limit',stage='process',process_name='Trasmissione ordini',
            reason='max_tokens',usage={'output_tokens':7000},
        )
        failure.part='controls'
        code,message=public_provider_error(failure)
        self.assertEqual('incomplete_process',code)
        self.assertIn('regole e i controlli',message)
        self.assertIn('tentativo compatto',message)

    def test_schema_uses_supported_compact_shape(self):
        value = json.dumps(operational_grammar.schema())
        for unsupported in ('"minimum"', '"maximum"', '"maxItems"'):
            self.assertNotIn(unsupported, value)
        self.assertEqual(["schema_version", "operation", "elements"], operational_grammar.schema()["required"])

    def test_compact_payload_validates_and_expands(self):
        value = payload()
        self.assertEqual([], operational_grammar.validate(value))
        expanded = operational_grammar.expand(value)
        self.assertEqual("Discovery", expanded["lifecycle"][0]["name"])
        self.assertEqual("Consultant", expanded["actors"][0]["name"])
        self.assertEqual("Cycle time", expanded["metrics"][0]["name"])

    def test_invalid_provenance_is_rejected(self):
        value = payload()
        value["elements"][0]["source_type"] = "suggested"
        value["elements"][0]["requires_confirmation"] = False
        self.assertTrue(any("must require confirmation" in item for item in operational_grammar.validate(value)))

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("operational_model_service.anthropic.Anthropic")
    def test_anthropic_provider_requests_structured_output(self, anthropic_client):
        anthropic_client.return_value.messages.stream.side_effect=[
            stream_for(map_payload(),100,120),
            stream_for(detail_payload(part='flow'),110,250),
            stream_for(detail_payload(part='controls'),111,284),
        ]
        model = AnthropicOperationalModelService().build(context())["model"]
        requests=[call.kwargs for call in anthropic_client.return_value.messages.stream.call_args_list]
        self.assertEqual(3,len(requests))
        self.assertEqual("json_schema", requests[0]["output_config"]["format"]["type"])
        map_properties=requests[0]['output_config']['format']['schema']['properties']
        detail_properties=requests[1]['output_config']['format']['schema']['properties']
        self.assertIn('domains',map_properties)
        self.assertNotIn('elements',map_properties)
        self.assertIn('process_id',detail_properties)
        self.assertNotIn("effort", requests[0]["output_config"])
        self.assertNotIn("effort", requests[1]["output_config"])
        self.assertEqual("medium", requests[2]["output_config"]["effort"])
        self.assertEqual('claude-haiku-4-5',requests[1]['model'])
        self.assertEqual('claude-sonnet-5',requests[2]['model'])
        self.assertLess(requests[0]["max_tokens"],12_000)
        self.assertLess(requests[1]["max_tokens"],12_000)
        self.assertEqual("2.0", model["schema_version"])
        self.assertEqual(321, model["generation"]["input_tokens"])
        self.assertEqual(654, model["generation"]["output_tokens"])
        self.assertEqual("Discovery", model["process"]["steps"][0]["stage"])

    def test_staged_retry_reuses_map_and_completed_process(self):
        mapped=map_payload()
        second={
            'id':'PROCESS-02','name':'Invoice approval','summary':'Approve supplier invoices',
            'trigger':'An invoice arrives','completion':'The invoice is recorded',
            'owner':'Finance owner','domain_id':'DOMAIN-01','source_type':'derived',
            'confidence':.8,'evidence':['Supplied operating notes'],'requires_confirmation':True,
        }
        mapped['processes'].append(second)
        checkpoints=[]
        first_client=MagicMock()
        first_client.messages.stream.side_effect=[
            stream_for(mapped,80,100),
            stream_for(detail_payload(part='flow'),80,110,cache_creation=1400),
            stream_for(detail_payload(part='controls'),80,110,cache_read=1400),
            stream_for({},160,5000,cache_read=1400,stop_reason='max_tokens'),
            stream_for({},160,6000,cache_read=1400,stop_reason='max_tokens'),
        ]
        with self.assertRaises(anthropic_staged.StageFailure):
            anthropic_staged.build(
                first_client,context(),"System","claude-sonnet-5",
                progress=lambda update,state:checkpoints.append(json.loads(json.dumps(state))),
            )
        saved=checkpoints[-1]
        self.assertIn('PROCESS-01',saved['details'])
        self.assertNotIn('PROCESS-02',saved['details'])
        # The common company context is explicitly on detail calls only.
        detail_request=first_client.messages.stream.call_args_list[1].kwargs
        self.assertEqual('ephemeral',detail_request['messages'][0]['content'][0]['cache_control']['type'])

        second_client=MagicMock()
        second_client.messages.stream.side_effect=[
            stream_for(detail_payload('PROCESS-02','flow'),20,100,cache_read=1400),
            stream_for(detail_payload('PROCESS-02','controls'),20,100,cache_read=1400),
        ]
        ontology,usage,_=anthropic_staged.build(
            second_client,context(),"System","claude-sonnet-5",checkpoint=saved,
        )
        self.assertEqual(2,second_client.messages.stream.call_count)
        self.assertEqual(2,len([x for x in ontology['elements'] if x['kind']=='stage']))
        self.assertEqual(7,usage['calls'])  # failed calls remain visible in provider billing, when reported

    def test_oversized_map_is_retried_once_with_more_output_space(self):
        client=MagicMock()
        client.messages.stream.side_effect=[
            stream_for({},100,2800,stop_reason='max_tokens'),
            stream_for(map_payload(),100,700),
            stream_for(detail_payload(part='flow'),90,150),
            stream_for(detail_payload(part='controls'),90,150),
        ]
        updates=[]

        _,usage,_=anthropic_staged.build(
            client,context(),'System','claude-sonnet-5',
            progress=lambda update,state:updates.append(update),
        )

        calls=client.messages.stream.call_args_list
        self.assertEqual(4,len(calls))
        self.assertGreater(calls[1].kwargs['max_tokens'],calls[0].kwargs['max_tokens'])
        self.assertTrue(any(update.get('phase')=='map_retry' for update in updates))
        self.assertEqual(4,usage['calls'])
        self.assertEqual('max_tokens',usage['stages'][0]['reason'])

    def test_retry_reuses_a_completed_flow_when_controls_fail(self):
        checkpoints=[]
        supplied_context=context()
        first_client=MagicMock()
        first_client.messages.stream.side_effect=[
            stream_for(map_payload(),50,100),
            stream_for(detail_payload(part='flow'),60,150),
            stream_for({},70,5200,stop_reason='max_tokens'),
            stream_for({},70,7000,stop_reason='max_tokens'),
        ]

        with self.assertRaises(anthropic_staged.StageFailure):
            anthropic_staged.build(
                first_client,supplied_context,'System','claude-sonnet-5',
                progress=lambda update,state:checkpoints.append(json.loads(json.dumps(state))),
            )

        saved=checkpoints[-1]
        self.assertIn('PROCESS-01',saved['partials'])
        self.assertIn('flow',saved['partials']['PROCESS-01'])

        second_client=MagicMock()
        second_client.messages.stream.side_effect=[
            stream_for(detail_payload(part='controls'),40,180),
        ]
        _,usage,_=anthropic_staged.build(
            second_client,supplied_context,'System','claude-sonnet-5',checkpoint=saved,
        )

        self.assertEqual(1,second_client.messages.stream.call_count)
        self.assertEqual('claude-sonnet-5',second_client.messages.stream.call_args.kwargs['model'])
        self.assertEqual(5,usage['calls'])

    def test_process_section_retries_automatically_with_more_output_space(self):
        client=MagicMock()
        client.messages.stream.side_effect=[
            stream_for(map_payload(),50,100),
            stream_for({},60,4200,stop_reason='max_tokens'),
            stream_for(detail_payload(part='flow'),60,300),
            stream_for(detail_payload(part='controls'),70,180),
        ]

        _,usage,_=anthropic_staged.build(
            client,context(),'System','claude-sonnet-5',
        )

        calls=client.messages.stream.call_args_list
        self.assertEqual(4,len(calls))
        self.assertGreater(calls[2].kwargs['max_tokens'],calls[1].kwargs['max_tokens'])
        self.assertIn('TENTATIVO COMPATTO',calls[2].kwargs['messages'][0]['content'][1]['text'])
        self.assertEqual('flow_failed',usage['stages'][1]['part'])

    def test_provider_error_also_checkpoints_the_completed_flow(self):
        checkpoints=[]
        client=MagicMock()
        client.messages.stream.side_effect=[
            stream_for(map_payload(),50,100),
            stream_for(detail_payload(part='flow'),60,150),
            RuntimeError('temporary provider failure'),
        ]

        with self.assertRaisesRegex(RuntimeError,'temporary provider failure'):
            anthropic_staged.build(
                client,context(),'System','claude-sonnet-5',
                progress=lambda update,state:checkpoints.append(json.loads(json.dumps(state))),
            )

        saved=checkpoints[-1]
        self.assertIn('flow',saved['partials']['PROCESS-01'])
        self.assertEqual('flow',saved['usage'][-1]['part'])

    def test_single_process_reconstruction_uses_the_safe_staged_contract(self):
        focused=context()
        focused['generation']={'scope':'single_process','process_name':'Warranty claims'}
        client=MagicMock()
        client.messages.stream.side_effect=[
            stream_for(map_payload(),50,100),
            stream_for(detail_payload(part='flow'),60,150),
            stream_for(detail_payload(part='controls'),70,180),
        ]

        ontology,usage,_=anthropic_staged.build(
            client,focused,'System','claude-sonnet-5',
        )

        self.assertEqual(3,client.messages.stream.call_count)
        self.assertEqual('single_process_staged_v2',usage['pipeline'])
        process_id=next(item['id'] for item in ontology['elements'] if item['kind']=='process')
        linked=[item for item in ontology['elements'] if item['kind'] not in {'process','operational_domain'}]
        self.assertTrue(all(process_id in item['links'] for item in linked))
        self.assertEqual([1],[item['order'] for item in linked if item['kind']=='decision_rule'])


if __name__ == "__main__":
    unittest.main()
