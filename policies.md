# Policy Resi / Recessi — Agente Customer Care (MVP)

Regole valide per tutti gli store (Laifen Italia, Xiaomi Store Italia, Yimiki, Dreo). Gli store seguono le stesse regole.

## 1. Finestre temporali

- I 14 giorni per il recesso decorrono dalla **data di consegna risultante dal tracking**. Il tracking fa fede, non è contestabile dal cliente.
- **Entro 14 giorni dalla consegna** → il caso è tipicamente un RECESSO (rimborso).
- **Difetto entro 2 anni dalla consegna** → GARANZIA/DOA, gestita con SWAP, mai con rimborso, salvo indisponibilità di dispositivi con cui effettuare la sostituzione.
- **Difetto oltre 2 anni dalla consegna** → richiesta fuori garanzia; l’operatore comunica il mancato accoglimento o gestisce un’eventuale eccezione.
- Il recesso resta un flusso distinto: entro 14 giorni porta al rimborso, non allo swap.

## 2. Categorie di richiesta

| Categoria | Definizione | Esito standard | Spedizione reso a carico di |
|---|---|---|---|
| Recesso | Ripensamento entro 14gg dalla consegna | Rimborso | Cliente: etichetta generata da noi su Sendcloud, **costo scalato dal rimborso** (importo da comunicare al cliente prima della conferma) |
| DOA / Garanzia | Difetto o malfunzionamento riscontrabile | Swap con dispositivo di pari o superiore valore; rimborso solo se nessun dispositivo swappabile | Azienda |
| Arrivato rotto | Danno da trasporto, riscontrabile da foto/video inviate all'apertura pratica | Rimborso | Azienda |
| Articolo errato | Errore di spedizione nostro | `[DA CONFERMARE: reso + invio corretto? il cliente trattiene l'errato?]` | Azienda |

## 3. Prodotti esclusi dal recesso

- **Rasoi e spazzolini**: esclusi dal recesso se la **scatola è aperta** (prodotto non più rivendibile dopo apertura/primo utilizzo).
- Rasoio/spazzolino con **sigillo integro** → recesso ammesso.
- L'esclusione riguarda solo il RECESSO: un rasoio difettoso resta coperto da DOA/garanzia.
- `[DA CONFERMARE]` Lista esplicita SKU/categorie esclusi per store.

## 4. Condizioni del reso (recesso)

- Il dispositivo deve tornare **nelle stesse condizioni in cui è stato spedito**: il prodotto deve essere rivendibile.
- L'etichetta di spedizione **non va attaccata sulla scatola del prodotto** (va sull'imballo esterno). Questa istruzione va sempre inclusa nella risposta al cliente.
- `[DA CONFERMARE]` Se il prodotto torna svalutato (segni d'uso, scatola rovinata): rimborso parziale con trattenuta, o rifiuto del reso?

## 5. Prove richieste (DOA / Arrivato rotto)

- Servono **foto e video che dimostrino la problematica** dichiarata.
- Materiale non conclusivo o rifiuto di inviarlo → la pratica NON procede.
- Ticket senza risposta/prove → **chiusura dopo 15 giorni dall'apertura**.
- Dal materiale inviato si distingue anche il danno da trasporto (→ Arrivato rotto, rimborso) dal difetto prodotto (→ DOA, swap).

## 6. Flusso economico

- **Rimborso**: parte solo dopo che il dispositivo è tornato in sede ed è stato verificato che rientra nelle condizioni di un recesso regolare.
- **Swap**: parte solo dopo rientro in sede e verifica che il dispositivo sia completo e conforme alla dichiarazione. Lo swap viene spedito via Shopify dalla sede logistica che ha giacenza.
- **Swap = dispositivo di pari o superiore valore.** `[DA CONFERMARE]` Chi sceglie il modello sostitutivo se il medesimo non è disponibile: azienda o cliente?

## 7. Casi particolari

- **Reso parziale su ordine multiprodotto**: ogni prodotto è gestito singolarmente. Per lo swap di un singolo articolo si **duplica l'ordine su Shopify** contenente solo l'articolo sostitutivo.
- **Cliente che non spedisce**: dopo **15 giorni** dalla generazione dell'etichetta senza spedizione → etichetta annullata, pratica chiusa.
- **Pacco di reso che arriva in sede danneggiato o incompleto**: `[DA CONFERMARE]` come si procede (contestazione al cliente? apertura sinistro corriere?).

## 8. Escalation → operatore umano (l'agente NON decide)

`[DA CONFERMARE — proposta]`
- Minacce legali, menzione di chargeback, reclami a piattaforme/associazioni consumatori.
- Casi che non rientrano in nessuna regola di questo documento.
- Ordini sopra soglia di valore: € `___`.
- Cliente che contesta una decisione già comunicata.
- Prove ambigue dove la distinzione DOA / danno trasporto / uso improprio non è netta.

## 9. Modalità operativa dell'agente (fase MVP)

- L'agente **non invia mai nulla direttamente al cliente** e **non esegue azioni** su Shopify/Sendcloud/gestionale.
- Per ogni ticket produce un pacchetto per l'operatore:
  1. **Contesto**: riepilogo caso (ordine, prodotti, data consegna, giorni trascorsi, categoria richiesta, regole applicabili).
  2. **Bozza di risposta** al cliente, pronta da inviare o modificare.
    3. **Azione proposta**: genera etichetta / chiedi numero ordine o email acquisto / chiedi foto-video / rifiuta con motivazione / escalation.
- L'operatore approva, modifica o scarta. Ogni modifica dell'operatore è segnale per raffinare policy o prompt.
