# Customer Return Agent

Prototipo portfolio di Return Operations per Shopify. Due demo automatiche
mostrano dall'inizio alla fine il workflow realmente usato dall'e-commerce:
identificazione del cliente, storico ordini, policy, prove, approvazione umana,
Sendcloud, rientro in magazzino e chiusura con swap o rimborso.

Il progetto è un prototipo demo-safe: non emette rimborsi, non modifica ordini
o inventario e non chiama Sendcloud o Make reali. Le due storie principali e i
sei casi della sandbox funzionano anche senza credenziali Shopify o Anthropic.

## Cosa dimostra

- intake conversazionale e memoria dei messaggi per pratica;
- recupero dei dati ordine tramite Shopify Admin API;
- identificazione tramite ordine o email e Customer 360 dimostrativo;
- separazione tra estrazione AI e decisioni deterministiche;
- garanzia di 2 anni per i difetti e recesso di 14 giorni;
- Evidence Center per foto, video e controllo fisico;
- approvazione umana e checkpoint fisico in magazzino;
- orchestrazione verificabile di Shopify, Sendcloud, logistica e Make in mock;
- state machine, audit trail e database operativo SQLite;
- importazione policy in regole strutturate con conferma umana;
- esecuzione pubblica sicura tramite snapshot anonimizzati dello store test.

## Architecture

```mermaid
flowchart LR
    Intake[Richiesta cliente] --> Shopify[Ordine + storico Shopify]
    Shopify --> AI[AI: intento + bozza]
    AI --> Rules[Policy: 14 giorni / 2 anni]
    Rules --> Evidence[Foto + video]
    Evidence --> Human[Approvazione umana]
    Human --> Sendcloud[Etichetta Sendcloud mock]
    Sendcloud --> Warehouse[Tracking + magazzino]
    Warehouse --> Resolution[Swap o rimborso]
    Resolution --> DB[(SQLite + audit)]
    Resolution --> Make[Make mock]
```

- **LLM:** comprende il testo e genera la bozza;
- **Rule engine:** decide secondo `policies.md`;
- **Human:** approva, modifica o escala;
- **SQLite:** mantiene pratica, stato, metriche e timeline;
- **Mock integrations:** simulano etichetta, tracking, azioni Shopify, Make e logistica.

L'audit tecnico dettagliato è in
[`docs/return-agent-architecture.md`](docs/return-agent-architecture.md).

## Tech stack

- Python 3.12 e Flask;
- SQLite tramite `sqlite3`, senza ORM;
- Anthropic API per classificazione e testo;
- Shopify Admin API collegata allo store di test;
- HTML/CSS/JavaScript senza framework frontend.

## Setup

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

Aprire `http://127.0.0.1:5000`.

Senza chiavi API landing, dashboard, Workbench, Policies, Database e Analytics
restano utilizzabili. Il simulatore cliente continua le pratiche registrate
senza chiamate a pagamento; l'intake di un nuovo ordine richiede invece Shopify
e Claude live.

## Sezioni del prototipo

- `/`: landing portfolio e percorso di verifica;
- `/demo/doa`: demo automatica dal difetto allo swap;
- `/demo/recesso`: demo automatica dal recesso al rimborso;
- `/dashboard`: panoramica operativa e coda prioritaria;
- `/workbench`: conversazione, bozza AI, revisione umana e feedback;
- `/policies`: importazione documenti, regole strutturate, eccezioni, simulazione e versioning sandbox;
- `/database`: pratiche persistenti, memoria conversazionale e stato;
- `/analytics`: metriche calcolate da pratiche, audit e feedback.

## Environment variables

- `ANTHROPIC_API_KEY`: chiave API per classificazione e bozza;
- `SHOPIFY_STORE`: dominio dello store Shopify di test;
- `SHOPIFY_TOKEN`: token Shopify; la dashboard legge gli ordini. Lo script
  opzionale di popolamento richiede anche scrittura clienti/ordini;
- `DEMO_MODE`: `true` abilita gli scenari portfolio riproducibili;
- `RETURN_SHIPPING_PROVIDER`: deve restare `mock` nel prototipo;
- `DATABASE_PATH`: percorso SQLite facoltativo, default `data/returns.db`.

Non usare credenziali o dati di produzione.

## Portfolio demo verificabile

Il percorso principale contiene due workflow automatici. **DOA / Garanzia**
parte dalla richiesta, identifica il cliente tramite email, controlla lo
storico e i 730 giorni, acquisisce foto e video, crea l'etichetta, simula il
rientro, chiude il reso senza rimborso, crea l'ordine sostitutivo e registra il
passaggio a Make. **Recesso** verifica i 14 giorni, simula etichetta e rientro,
controlla le condizioni e chiude con rimborso.

Ogni avanzamento usa la vera state machine e viene scritto in SQLite. I sei
scenari precedenti restano nella sandbox per provare liberamente casi aperti,
richieste di informazioni, esclusioni ed escalation. I dati ordine derivano
dal manifest `data/shopify_experiment_return-agent-20260901.json`.

Nel dettaglio pratica sono visibili:

- origine e modalità del dato (`live_api` oppure `recorded_fixture`);
- Shopify order ID e timestamp di acquisizione;
- payload Shopify ridotto e anonimizzato;
- risultato atteso dello scenario e risultato del motore;
- policy applicata, modalità AI, conversazione e audit trail.

Riavviare una demo guidata ripristina soltanto la relativa pratica. Il comando
**Ripristina demo** ricrea invece i sei casi della sandbox senza toccare le
pratiche inserite manualmente.

## Mock mode e dati demo

Il provider spedizioni è sempre mock. Dopo l'approvazione crea identificativi
`MOCK-RET-*`, tracking `MOCK*` e un URL simbolico, senza traffico esterno.

Per popolare un database vuoto:

```powershell
python scripts/seed_demo.py
```

Per preparare su Shopify 20 clienti sintetici e 20 ordini test, prima eseguire
il controllo senza modifiche e poi il caricamento esplicito:

```powershell
python scripts/seed_shopify_experiment.py --allow-store mindroute.myshopify.com
python scripts/seed_shopify_experiment.py --execute --allow-store mindroute.myshopify.com
```

Servono `read/write_customers` e `read/write_orders`. Gli ordini hanno
`test=true`, pagamento in attesa e notifiche disabilitate; lo script non crea
transazioni o spedizioni. Al termine genera in `data/` una guida con i messaggi
cliente già pronti da copiare nella dashboard.

## Demo workflow

1. Dalla landing scegliere **Guarda il caso DOA** oppure **Guarda il recesso**.
2. La pratica parte automaticamente; è possibile mettere in pausa o avanzare.
3. Durante il percorso osservare Shopify, storico cliente, prove, stato delle
   integrazioni e audit trail.
4. Alla chiusura aprire la stessa pratica nel Workbench o nel Database.
5. Usare la dashboard e il Workbench come sandbox libera per modificare bozze,
   continuare conversazioni o provare gli altri casi.

La sezione **Database resi** (`/database`) mostra il registro SQLite completo
in forma tabellare. Ogni e-mail o messaggio aggiunto alla pratica viene salvato
in `case_messages`; una nuova risposta sullo stesso ordine aggiorna la pratica
esistente invece di crearne automaticamente una duplicata.

Le prove foto/video sono considerate ricevute soltanto quando l'operatore usa
il comando esplicito nella pratica. Per rasoi e spazzolini viene chiesto lo
stato del sigillo prima di decidere.

## ReturnCase state machine

Le transizioni sono definite in `src/domain.py`. Il percorso principale è:

```text
NEW → ANALYZED → WAITING_HUMAN_APPROVAL → APPROVED
    → LABEL_CREATED → WAITING_FOR_RETURN → RETURN_RECEIVED
    → RETURN_VALIDATED → REFUND_PENDING/REPLACEMENT_PENDING
    → REFUNDED/REPLACED → CLOSED
```

`RETURN_RECEIVED` significa soltanto **arrivato e da controllare**. Rimborso e
swap restano bloccati finché un operatore non conferma il controllo fisico e
porta la pratica a `RETURN_VALIDATED`.

Sono previsti anche `NEEDS_INFORMATION`, `REJECTED` ed `ESCALATED`. Ogni
transizione produce un evento in `audit_events`.

## Policies architecture

`policies.md` resta la fonte leggibile. `return_policies.json` ne espone i
parametri applicabili al codice e `src/rules.py` è l'unico motore decisionale,
usato sia dall'intake live sia dagli scenari portfolio. Ogni pratica salva
versione, regola, sezioni e vincoli applicati in `policy_decision`.

Sono operative anche la garanzia di 730 giorni, le scadenze a 15 giorni, lo
swap per DOA, il controllo del sigillo, il pagatore della spedizione, la
verifica fisica e il fallback a rimborso quando manca lo stock sostitutivo. Le voci marcate
`[DA CONFERMARE]` non vengono inventate: compaiono in dashboard e richiedono
una decisione umana.

Il wizard **Aggiungi policy** accetta testo, TXT/MD, PDF, DOCX oppure un URL
HTTPS pubblico. Produce una bozza di campi strutturati modificabili, separa le
ambiguità e richiede una conferma umana. Nella demo la pubblicazione crea solo
una versione sandbox e non sostituisce `return_policies.json`. Il tab
**Simulazioni** esegue invece il vero motore deterministico su uno snapshot
Shopify senza creare pratiche o azioni esterne.

## API integrations

- Shopify: lettura dell'ordine di test durante il workflow; scrittura usata
  soltanto dallo script demo lanciato esplicitamente;
- tracking: `deliveries.json` è il mock corrente;
- spedizione reso: `MockReturnShippingProvider`;
- le demo guidate registrano azioni mock per chiusura reso Shopify, ordine
  sostitutivo, rimborso, Sendcloud, Make e logistica;
- Sendcloud e Make reali non sono implementati.

## Testing

I test non richiedono API o credenziali:

```powershell
python -m unittest discover -s tests -v
```

I 41 test coprono regole, sigillo, prove, garanzia, confidence, scadenze, persistenza,
scenari portfolio, pagine separate, feedback, conversazione simulata,
transizioni, ispezione, importazione policy e i due workflow guidati completi.

## Deploy portfolio su Render

Il file `render.yaml` avvia l'app con Gunicorn, database effimero e modalità
demo. Non sono necessarie chiavi API per la demo guidata. A ogni nuova istanza
gli scenari vengono ricreati dal manifest versionato; `/health` espone soltanto
lo stato tecnico non sensibile.

## Current limitations

- applicazione locale senza autenticazione o ruoli;
- un solo prodotto principale salvato per pratica; gli ordini multiprodotto
  richiedono ancora un modello line-item più completo;
- nessun upload reale di foto/video;
- lo storico Customer 360 delle demo guidate è un dataset sintetico;
- nessun invio reale della risposta al cliente;
- sul piano Shopify Basic nome ed e-mail sono oscurati all'API; per gli ordini
  sintetici la dashboard usa esclusivamente i dati demo salvati nel manifest;
- Shopify REST dovrà essere migrato a GraphQL;
- SQLite e processo Flask singolo sono adatti alla demo, non alla produzione;
- le policy importate vengono pubblicate solo nella sandbox e non modificano il motore attivo;
- azioni Shopify, Sendcloud, Make, logistica, rimborso e swap sono simulate;
- il return rate non è calcolabile senza il totale degli ordini venduti.

## Future improvements

Prima di un uso reale: autenticazione operatori, PostgreSQL, gestione completa
dei line item, storage allegati, Sendcloud sandbox, canale email/helpdesk,
GraphQL Shopify e policy aziendali definitive.
