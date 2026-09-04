# Ops Copilot

Prototipo di Operational Decision Intelligence configurato sulle procedure di
una singola azienda. Non impone un workflow di customer care, agenzia o back
office: l’azienda descrive il proprio contesto, l’operazione da migliorare e la
conoscenza disponibile; il sistema costruisce un primo modello operativo
esplicito e verificabile.

```text
Company context + operation + knowledge
→ context/privacy layer
→ structured operational model
→ smart clarifications
→ synthetic tests
→ active human-guided workspace
```

Non sono necessarie integrazioni, credenziali Shopify o chiavi AI. Il prototipo
non esegue azioni esterne.

## Onboarding 0–8

Il percorso `/onboarding` salva progressivamente ogni passaggio in SQLite:

0. benvenuto e creazione del workspace;
1. contesto aziendale;
2. prima operazione e obiettivo;
3. documenti o note operative facoltativi;
4. strutturazione del modello;
5. chiarimenti generati soltanto sui punti non univoci;
6. review manageriale di case type, campi, regole ed escalation;
7. valutazione di tre scenari sintetici;
8. attivazione del workspace con un livello di completezza non forzato al 100%.

Gli upload supportano PDF, DOCX, TXT e MD. Il contenuto resta server-side; il
browser riceve soltanto metadati e stato dell’elaborazione.

## Modello operativo generico

Gli oggetti principali sono:

- `Workspace` e relativo contesto aziendale;
- `Operation` e modello operativo attivo;
- `KnowledgeSource`;
- `CaseType`, `RequiredField`, `Rule` ed `Escalation`;
- `Clarification` e `TestScenario`;
- casi, messaggi, feedback e audit trail già presenti nel prodotto.

Il motore locale produce una configurazione deterministica e coerente. È
incapsulato dietro `operational_model_service.py`, così un provider AI futuro
può sostituirlo senza spostare logica nelle pagine o nei controller.

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

## Test

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

La suite copre onboarding end-to-end, persistenza, privacy layer, modello
generico, Workbench configurato e tutte le regressioni della sandbox.

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

- un solo workspace e una sola operazione per browser nel prototipo;
- nessuna autenticazione o autorizzazione multi-tenant;
- motore di strutturazione locale, non un LLM esterno;
- redazione euristica, non sufficiente per dati reali sensibili;
- nessuna integrazione o azione esterna;
- SQLite e filesystem adatti a demo/portfolio, non a produzione distribuita.

Academy, Radar e la promozione assistita delle decisioni in nuove regole restano
estensioni successive.
