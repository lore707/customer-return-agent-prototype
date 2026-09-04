# Return Agent — architettura del prototipo

## Flusso operativo

```mermaid
flowchart LR
    C[Messaggio cliente] --> SH[Shopify: ordine + storico]
    SH --> AI[Claude: estrazione + bozza]
    AI --> RE[Policy: 14 giorni / 730 giorni]
    RE --> EV[Foto e video]
    EV --> H[Approvazione umana]
    H --> SC[Sendcloud mock]
    SC --> WH[Tracking + controllo fisico]
    WH --> RS[Swap o rimborso]
    RS --> MK[Make + logistica mock]
    RS --> DB[(ReturnCase + audit SQLite)]
    DB --> UI[Demo guidata + sandbox]
```

Responsabilità separate:

- **Claude** struttura il linguaggio naturale e prepara il testo;
- **Shopify** è la fonte dei dati di ordine, cliente e prodotto;
- **Customer 360** raccoglie ordini, resi e sostituzioni precedenti;
- **Rule engine** decide l'idoneità secondo `policies.md`;
- **Evidence Center** separa allegati dichiarati e controllo fisico;
- **Operatore** approva il messaggio e valida fisicamente il reso;
- **SQLite** conserva pratica, conversazione, stato e audit trail;
- **Mock integrations** simulano etichetta, tracking, azioni Shopify, Make e logistica.

## Modalità portfolio

Le route `/demo/doa` e `/demo/recesso` avviano due pratiche dedicate. Il
browser richiama un endpoint di avanzamento; il server applica le transizioni,
aggiorna Customer 360, Evidence Center e stato delle integrazioni e registra
ogni evento. La conclusione è quindi ispezionabile nel Workbench e nel Database,
non è una semplice animazione frontend.

La dashboard può essere aperta senza credenziali. Sei scenari riproducibili
usano il manifest creato durante l'esperimento sullo store Shopify test:

`data/shopify_experiment_return-agent-20260901.json`

Ogni pratica registra:

- `data_source`: sistema sorgente dichiarato;
- `source_mode`: `live_api` oppure `recorded_fixture`;
- `source_fetched_at`: momento di acquisizione;
- `source_payload`: snapshot ridotto e privo di credenziali;
- `ai_mode`: elaborazione Claude live oppure risultato demo registrato;
- `scenario_slug`: scenario portfolio, se presente.
- `policy_decision`: versione, regola, sezioni e vincoli realmente applicati.

Il dettaglio pratica espone questi campi, il risultato atteso e la timeline.
In questo modo il visitatore può verificare la provenienza dei dati senza
accedere allo store privato.

## State machine

```text
NEW → ANALYZED → WAITING_HUMAN_APPROVAL → APPROVED
    → LABEL_CREATED → WAITING_FOR_RETURN → RETURN_RECEIVED
    → RETURN_VALIDATED → REFUND_PENDING/REPLACEMENT_PENDING
    → REFUNDED/REPLACED → CLOSED
```

Sono previsti anche `NEEDS_INFORMATION`, `REJECTED`, `RETURN_IN_TRANSIT` ed
`ESCALATED`. Le transizioni non previste vengono rifiutate lato server.

`RETURN_RECEIVED` significa soltanto che il tracking o l'operatore hanno
registrato l'arrivo. Rimborso e sostituzione rimangono bloccati fino alla
transizione manuale `RETURN_VALIDATED`.

## Scelte intenzionalmente semplici

- Flask con template server-side e JavaScript minimo;
- SQLite senza ORM;
- CSS proprietario senza framework frontend;
- snapshot versionato per la demo pubblica;
- nessuna azione economica o modifica Shopify;
- nessun account o ruolo, perché non necessari per il portfolio.

## Verifica

La suite contiene 41 test automatici per regole, garanzia, confidence, scadenze,
persistenza, memoria conversazionale, reset selettivo, ispezione fisica e i due
workflow guidati completi.

```powershell
python -m unittest discover -s tests -v
```

Per un'eventuale evoluzione produttiva servirebbero autenticazione, storage
allegati, PostgreSQL, Shopify GraphQL, email/helpdesk e provider logistico reale.
