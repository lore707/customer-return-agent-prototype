// Optional DOM interaction checks: npm install --no-save jsdom; node tests/memory_ui.cjs
// These complement the Python integration tests; they do not verify pixel layout.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const script = fs.readFileSync(path.join(__dirname, '../static/memory.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const processFixture = {
  id:'p1', name:'Approvazione acquisti', area:'Amministrazione', summary:'Verifica le richieste.',
  owner:'Responsabile acquisti', trigger:'Richiesta', completion:'Ordine registrato', status:'approved',
  steps:[{id:'s1',title:'Verifica',action:'Verifica il totale',owner:'Responsabile',output:'Importo verificato'}],
  fields:[{id:'amount',label:'Importo',type:'number'}],
  rules:[{id:'r1',title:'Limite di spesa',when:'Entro 200 euro',action:'Autorizzare',mode:'conditions',
    criteria:[{field:'amount',operator:'lte',value:200}],source:'Procedura',evidence:'Limite 200 euro',origin:'explicit',approved:true}],
  gaps:[],notes:''
};
async function boot(section) {
  let saved;
  const document = {company:{name:'Acme',description:'Attività aziendale',challenges:'Documenti dispersi'},areas:[],
    sources:[{id:'src',name:'<img src=x onerror=alert(1)>',text:'Testo fonte'}],processes:[structuredClone(processFixture)]};
  const state={draft:document,revision:1,published_version:1,versions:[],coverage:{processes:1,approved:1,rules:1,gaps:0}};
  const context={published:{version:1,document},cases:[],signals:[],proposals:[],academy:{version:1,lessons:[]},
    analytics:{total:0,decisions:0,actions:0,outcomes:0,processes:{},missing:{},feedback:0,cases:[],first_decision_median_seconds:null,first_decision_sample:0}};
  const dom=new JSDOM('<div id="memNotice" hidden></div><main id="knowledgeApp"></main><dialog id="memDialog"><div id="dialogContent"></div></dialog><script id="memoryInitial" type="application/json"></script>',
    {url:`http://localhost/workspace/${section}`,runScripts:'outside-only'});
  const w=dom.window;
  w.document.getElementById('memoryInitial').textContent=JSON.stringify({state,section});
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
  w.HTMLDialogElement.prototype.close=function(){this.open=false;};
  w.fetch=async(url,options)=>{
    if(url==='/api/memory'&&options?.method==='POST'){
      saved=JSON.parse(options.body);Object.assign(state,{draft:saved.document,revision:state.revision+1});
      return {ok:true,json:async()=>structuredClone(state)};
    }
    assert.equal(url,'/api/ops/context');return {ok:true,json:async()=>structuredClone(context)};
  };
  w.eval(script);await tick();
  const click=async selector=>{const el=w.document.querySelector(selector);assert.ok(el,selector);el.click();await tick();};
  const input=(selector,value)=>{const el=w.document.querySelector(selector);assert.ok(el,selector);el.value=value;el.dispatchEvent(new w.Event('input',{bubbles:true}));};
  return {w,click,input,saved:()=>saved,close:()=>w.close()};
}
(async()=>{
  for(const section of ['memory','assist','cases','analytics','radar','academy']){
    const page=await boot(section);
    assert.ok(page.w.document.querySelector('#knowledgeApp h2'),section);
    assert.equal(page.w.document.querySelector('#knowledgeApp').getAttribute('aria-busy'),'false');
    assert.equal(page.w.document.querySelector('#knowledgeApp img'),null,'Source name must be escaped');
    page.close();
  }
  const page=await boot('memory');
  assert.ok(page.w.document.body.textContent.includes('Documenti dispersi'));
  await page.click('[data-action="edit"]');
  page.input('[data-bind="company.goals"]','Ridurre le richieste incomplete');
  await page.click('[data-action="save"]');
  assert.equal(page.saved().document.company.goals,'Ridurre le richieste incomplete');
  await page.click('[data-action="select"][data-id="p1"]');
  await page.click('[data-action="edit"]');
  page.input('[data-bind="processes.0.rules.0.action"]','Richiedere approvazione aggiornata');
  assert.equal(page.w.document.querySelector('[data-bind="processes.0.rules.0.approved"]').checked,false);
  await page.click('[data-action="add-criterion"]');
  assert.ok(page.w.document.querySelector('[data-bind="processes.0.rules.0.criteria.1.value"]'));
  await page.click('[data-action="new-process"]');
  page.input('#newProcessName','Inserimento nuovi collaboratori');
  await page.click('[data-action="create-process"]');
  assert.ok(page.w.document.body.textContent.includes('Inserimento nuovi collaboratori'));
  await page.click('[data-action="add-step"]');
  assert.ok(page.w.document.querySelector('[data-bind="processes.1.steps.0.action"]'));
  page.close();
  console.log('DOM: 6 sections, company editing, source escaping, rule approval invalidation and process creation passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
