# Ops Copilot

Prototipo di Operational Decision Intelligence configurato sulle procedure di
una singola azienda. Non impone un workflow di customer care, agenzia o back
office: l’azienda descrive chi è, come lavora e ciò che sa già; il sistema
costruisce una prima memoria operativa esplicita, modificabile e verificabile.

```text
Conoscenza aziendale → memoria in bozza → revisione → versione pubblicata
→ assistenza sui casi → decisione umana → azione → esito e riscontro
→ Analisi / Radar → proposta verificata → nuova versione della memoria
                                  ↘ Academy dalla stessa memoria pubblicata
```

Non sono necessarie integrazioni o credenziali Shopify e il prototipo non esegue
azioni esterne. La ricostruzione può funzionare con il motore locale gratuito
oppure, quando configurato esplicitamente, con Claude tramite API Anthropic.

## Onboarding 0–6

Il percorso `/onboarding` salva progressivamente ogni passaggio in SQLite:

0. benvenuto e creazione del workspace;
1. identità essenziale dell’azienda;
2. core business, attività operative e difficoltà interne;
3. documenti o note operative facoltativi;
4. ricostruzione della memoria operativa;
5. lettura della ricostruzione generale e dei processi identificati;
6. salvataggio nella Memoria Operativa come bozza, senza attivare le regole.

## Memoria aziendale e ciclo operativo

`/memory` separa identità, attività, difficoltà, persone e strumenti dai singoli
processi. Ogni processo contiene una spiegazione, passaggi, responsabilità,
informazioni da verificare, regole, fonti, eccezioni e lacune. Tutto è modificabile
manualmente. È possibile aggiungere processi manualmente oppure ricostruirli da
documenti e note con il provider configurato.

La ricostruzione non approva le proprie regole e non inventa condizioni eseguibili.
Il responsabile configura i confronti sui fatti oppure mantiene la valutazione
umana. La pubblicazione crea una versione immutabile; solo i processi approvati
e le loro regole confermate sono utilizzati nell’area operativa e in Academy.
Una fonte cambiata richiede la revisione delle regole pertinenti: non viene
reinterpretata automaticamente come nuova istruzione.

- `/workspace/assist`: conversazione, raccolta di fatti tipizzati, fonti e
  condizioni verificate. La lettura locale riconosce «Nome campo: valore»; il
  pulsante AI propone fatti con estratti del messaggio. Entrambi richiedono
  conferma umana. La valutazione delle condizioni non usa un LLM.
- `/workspace/cases`: casi con snapshot della versione utilizzata, decisione,
  azione ed esito distinti e storico degli aggiornamenti. Modificare i fatti
  invalida la precedente approvazione. I casi conclusi restano immutabili.
- `/workspace/analytics`: conteggi reali, processi utilizzati, informazioni
  mancanti, riscontri e mediana del tempo alla prima decisione. Nessuna stima
  inventata di risparmio o produttività.
- `/workspace/radar`: lacune e riscontri collegati ai casi, proposte, revisione
  motivata, aggiunta alle note del processo in bozza e successiva pubblicazione.
  Il responsabile deve aggiornare anche le regole interessate. Le osservazioni
  esterne si inseriscono manualmente: non c’è ricerca web automatica.
- `/workspace/academy`: schede ed esercizi generati dalle regole pubblicate,
  inclusi casi al di fuori delle soglie numeriche. Le risposte vengono valutate
  con lo stesso motore dei casi; i tentativi conservano la versione studiata.

Il motore confronta valori di tipo testo, numero, data e booleano. Tutte le
condizioni di una regola devono essere vere; dati mancanti e azioni contrastanti
richiedono raccolta di informazioni o valutazione umana. Non esegue azioni esterne.
Gli endpoint precedenti della sandbox restano disponibili per la demo storica,
ma non elaborano più le nuove configurazioni aziendali tramite la vecchia logica.

La verifica tramite scenari non appesantisce più l’onboarding: verrà proposta
successivamente come simulazione guidata usando la stessa memoria pubblicata.

Gli upload supportano PDF, DOCX, TXT e MD. Il contenuto resta server-side; il
browser riceve metadati e stato durante l’elaborazione; nella Memoria Operativa
può consultare le fonti della propria organizzazione. La generazione con
Claude viene avviata come job in background: la pagina interroga un endpoint di
stato fino al completamento, evitando i timeout dei proxy di hosting durante le
ricostruzioni più lunghe.

La ricostruzione Claude è suddivisa in passaggi controllabili:

1. un modello rapido produce una mappa compatta di aree e processi, senza
   generare prematuramente regole e fasi;
2. ogni processo viene approfondito separatamente dal modello principale;
3. i frammenti superano la grammatica e i controlli di provenienza prima di
   essere accettati;
4. i processi completati vengono salvati in un checkpoint SQLite: dopo un
   errore o un riavvio, il nuovo tentativo paga ed elabora solo quelli mancanti;
5. il contesto comune usa la cache del prompt e, dopo la prima richiesta, gli
   approfondimenti rimanenti procedono con concorrenza limitata.

Il primo onboarding approfondisce al massimo sei processi. Gli altri possono
essere aggiunti in seguito dalla Memoria Operativa con una singola chiamata
mirata, evitando di ricostruire l’intera azienda. La schermata mostra fasi reali,
processi completati e token dichiarati dal provider; non simula una percentuale
di avanzamento.

## Grammatica operativa universale

La grammatica `2.0` stabilisce **quali oggetti può contenere un'operazione**, non
quali processi debba usare un'azienda. Gli oggetti principali sono:

- `Workspace` e relativo contesto aziendale;
- `Operation` e modello operativo attivo;
- `KnowledgeSource`;
- `OperationalDomain`, `Process`, `CaseType`, `Actor`, `System`, `Input`, `LifecycleStage` e `DecisionRule`;
- `Exception`, `Escalation`, `Constraint`, `Outcome`, `Metric` e `FeedbackLoop`;
- `Clarification` e `TestScenario`;
- casi, messaggi, feedback e audit trail già presenti nel prodotto.

Ogni elemento conserva provenienza (`explicit`, `derived` o `suggested`), livello
di confidenza, evidenza e necessità di conferma. Claude riceve una rappresentazione
compatta della grammatica e restituisce JSON vincolato dallo schema; l'app lo
espande nel documento operativo completo. Il motore locale resta un fallback
gratuito e deterministico. Entrambi sono incapsulati dietro
`operational_model_service.py` e alimentano la stessa UI e persistenza.

Prima della strutturazione, `context_privacy.py` minimizza il payload, limita la
quantità di testo e rimuove email, telefoni e segreti comuni. Il confine è:

```text
raw data → context/privacy layer → model service → structured response → app
```

## Workspace

- **Workbench** usa l’operazione configurata, raccoglie i campi richiesti dal
  suo modello e prepara la prossima azione per la conferma umana.
- **Casi** conserva richiesta minimizzata, fatti, decisione, feedback ed esito.
- **Analytics** aggrega soltanto i casi appartenenti all’operazione attiva.
- **Playbooks** mostra scopo, regole, escalation, ambiguità e scenari del
  modello generato.

Se il setup non è completo al 100%, un indicatore discreto resta disponibile
nella shell senza bloccare il Workbench. Le decisioni conservano già una
struttura che in futuro potrà diventare “trasforma questa decisione in regola”.

## Sandbox separata

I precedenti workflow dimostrativi restano disponibili solo come sandbox:

- `/workbench?demo=1`
- `/cases?demo=1`
- `/analytics?demo=1`
- `/playbooks?demo=1`
- `/demo/doa` e `/demo/recesso`

Non rappresentano più la configurazione di default del prodotto.

## Architettura

- Flask serve UI e API;
- Jinja, CSS e JavaScript implementano il prodotto senza framework frontend;
- SQLite conserva onboarding, knowledge metadata, modello, test e memoria
  operativa;
- il parser documentale gestisce PDF, DOCX, TXT e MD;
- il cookie `ops_workspace_id` mantiene il workspace demo sullo stesso browser
  e non costituisce un sistema di autenticazione.

## Avvio locale

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Apri `http://127.0.0.1:5000` e scegli **Configura la prima operazione**.

Per usare Claude, aggiungi a `.env`:

```text
OPERATIONAL_MODEL_PROVIDER=anthropic
OPERATIONAL_MODEL_MODEL=claude-sonnet-5
OPERATIONAL_MAP_MODEL=claude-haiku-4-5
OPERATIONAL_MODEL_EFFORT=medium
OPERATIONAL_MAP_MAX_TOKENS=2800
OPERATIONAL_PROCESS_MAX_TOKENS=5200
OPERATIONAL_PROCESS_CONCURRENCY=2
ANTHROPIC_API_KEY=la_tua_chiave
```

Con `OPERATIONAL_MODEL_PROVIDER=local` l'onboarding non effettua chiamate a
pagamento. Su Render `ANTHROPIC_API_KEY` va impostata come secret environment
variable; non deve essere salvata nel repository. Quando Claude è configurato,
un errore del provider viene mostrato come errore e non viene mascherato con un
output locale. Il fallback può essere abilitato soltanto in modo esplicito con
`OPERATIONAL_MODEL_ALLOW_LOCAL_FALLBACK=true`.

## Test

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

La suite copre onboarding end-to-end, persistenza, privacy layer, modello
generico, versioni della memoria, isolamento per workspace, condizioni reali,
Academy, ciclo di feedback e regressioni della sandbox. `tests/memory_ui.cjs`
verifica inoltre rendering DOM, editor e interazioni delle sei sezioni (richiede
`jsdom` nell’ambiente di sviluppo). Non sostituisce una verifica visuale browser.

## Deploy su Render

`render.yaml` usa:

```text
Build: pip install -r requirements.txt
Start: gunicorn --bind 0.0.0.0:$PORT app:app
```

Nel piano demo il database può essere effimero. Un SaaS reale richiederebbe
autenticazione, isolamento tenant, PostgreSQL, object storage, backup, ruoli,
retention e controlli privacy formali.

## Limiti dichiarati

- un workspace per browser, con più processi; nessuna gestione account;
- nessuna autenticazione o autorizzazione multi-tenant;
- provider locale o Claude configurabile; i test automatici non consumano API;
- redazione euristica, non sufficiente per dati reali sensibili;
- nessuna integrazione o azione esterna;
- SQLite e filesystem adatti a demo/portfolio, non a produzione distribuita.

I job AI sono temporanei nel processo web, non una coda persistente distribuita.
Il piano Render configurato usa SQLite in `/tmp`: un riavvio o deploy può perdere
i dati demo. Per dati aziendali reali servono prima persistenza e backup,
autenticazione, permessi, protezioni antiabuso delle chiamate API e verifica privacy.
