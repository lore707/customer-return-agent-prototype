# Test Shopify - return-agent-20260901

Negozio: `mindroute.myshopify.com`

Copia un messaggio nella sezione **Nuova pratica** della dashboard. Gli ordini sono test, senza pagamento e senza e-mail.

## 01. recesso_entro_4g - ordine #1008

Cliente: Anna Demo 01  
Prodotto: Asciugacapelli Air Pro (`HAIR-AIR-PRO`)  
Esito iniziale atteso: `procedi_rimborso`

> Ordine #1008: l'asciugacapelli funziona, ma ho cambiato idea e vorrei restituirlo.

## 02. recesso_entro_12g - ordine #1009

Cliente: Marco Demo 02  
Prodotto: Asciugacapelli Air Pro (`HAIR-AIR-PRO`)  
Esito iniziale atteso: `procedi_rimborso`

> Vorrei esercitare il diritto di recesso per l'ordine #1009. Il prodotto non ha difetti.

## 03. recesso_fuori_termini - ordine #1010

Cliente: Giulia Demo 03  
Prodotto: Asciugacapelli Mini (`HAIR-MINI`)  
Esito iniziale atteso: `rifiuta_fuori_finestra`

> Ordine #1010: il prodotto funziona ma non mi serve piu e vorrei restituirlo.

## 04. rasoio_sigillato - ordine #1011

Cliente: Luca Demo 04  
Prodotto: Rasoio elettrico Smooth (`SHAVE-SMOOTH`)  
Esito iniziale atteso: `procedi_rimborso`

> Ordine #1011: ho cambiato idea. Rasoio mai aperto, confezione e sigillo sono ancora integri.

## 05. rasoio_aperto - ordine #1012

Cliente: Sara Demo 05  
Prodotto: Rasoio elettrico Smooth (`SHAVE-SMOOTH`)  
Esito iniziale atteso: `rifiuta_recesso_prodotto_escluso`

> Ordine #1012: vorrei fare il reso del rasoio, ma ho gia aperto la confezione e l'ho provato.

## 06. spazzolino_sigillo_ignoto - ordine #1013

Cliente: Paolo Demo 06  
Prodotto: Spazzolino Sonic Care (`DENTAL-SONIC`)  
Esito iniziale atteso: `chiedi_stato_sigillo`

> Vorrei restituire lo spazzolino dell'ordine #1013 perche ho cambiato idea.

## 07. difettoso_entro_termini - ordine #1014

Cliente: Elena Demo 07  
Prodotto: Asciugacapelli Air Pro (`HAIR-AIR-PRO`)  
Esito iniziale atteso: `chiedi_foto_video`

> L'asciugacapelli dell'ordine #1014 non si accende fin dal primo utilizzo.

Dopo **Conferma prove ricevute**: `procedi_swap`.

## 08. difettoso_oltre_termini - ordine #1015

Cliente: Davide Demo 08  
Prodotto: Aspirapolvere Compact (`HOME-VAC-COMPACT`)  
Esito iniziale atteso: `chiedi_foto_video`

> Ordine #1015: l'aspirapolvere ha smesso di funzionare e non parte piu.

Dopo **Conferma prove ricevute**: `procedi_swap`.

## 09. danneggiato_trasporto - ordine #1016

Cliente: Chiara Demo 09  
Prodotto: Bollitore Glass (`KITCHEN-KETTLE`)  
Esito iniziale atteso: `chiedi_foto_video`

> Il bollitore dell'ordine #1016 e arrivato rotto dentro il pacco.

Dopo **Conferma prove ricevute**: `procedi_rimborso`.

## 10. articolo_errato - ordine #1017

Cliente: Simone Demo 10  
Prodotto: Asciugacapelli Air Pro (`HAIR-AIR-PRO`)  
Esito iniziale atteso: `escalation_operatore`

> Ordine #1017: avevo ordinato il phon Air Pro ma ho ricevuto uno spazzolino.

## 11. accessorio_mancante - ordine #1018

Cliente: Francesca Demo 11  
Prodotto: Aspirapolvere Compact (`HOME-VAC-COMPACT`)  
Esito iniziale atteso: `escalation_operatore`

> Nell'ordine #1018 manca la bocchetta piccola prevista nella confezione.

## 12. ordine_non_consegnato - ordine #1019

Cliente: Andrea Demo 12  
Prodotto: Asciugacapelli Mini (`HAIR-MINI`)  
Esito iniziale atteso: `ordine_non_consegnato`

> Vorrei restituire l'ordine #1019, ma non mi e stato ancora consegnato.

## 13. richiesta_sostituzione - ordine #1020

Cliente: Valentina Demo 13  
Prodotto: Spazzolino Sonic Care (`DENTAL-SONIC`)  
Esito iniziale atteso: `chiedi_foto_video`

> Lo spazzolino dell'ordine #1020 non si ricarica. Vorrei una sostituzione.

Dopo **Conferma prove ricevute**: `procedi_swap`.

## 14. richiesta_rimborso_difetto - ordine #1021

Cliente: Matteo Demo 14  
Prodotto: Bollitore Glass (`KITCHEN-KETTLE`)  
Esito iniziale atteso: `chiedi_foto_video`

> Il bollitore dell'ordine #1021 perde acqua dalla base. Chiedo il rimborso.

Dopo **Conferma prove ricevute**: `procedi_swap`.

## 15. minaccia_chargeback - ordine #1022

Cliente: Alessia Demo 15  
Prodotto: Asciugacapelli Air Pro (`HAIR-AIR-PRO`)  
Esito iniziale atteso: `escalation_operatore`

> Ordine #1022: se non mi rimborsate subito apro un chargeback e procedo per vie legali.

## 16. richiesta_ambigua - ordine #1023

Cliente: Stefano Demo 16  
Prodotto: Asciugacapelli Mini (`HAIR-MINI`)  
Esito iniziale atteso: `richiesta_chiarimento_o_escalation`

> Ho un problema con l'ordine #1023, potete aiutarmi?

## 17. difetto_alto_valore - ordine #1024

Cliente: Ilaria Demo 17  
Prodotto: Styler Premium Pro (`HAIR-STYLER-PREMIUM`)  
Esito iniziale atteso: `chiedi_foto_video`

> Lo styler premium dell'ordine #1024 si spegne dopo pochi secondi.

## 18. secondo_articolo_errato - ordine #1025

Cliente: Roberto Demo 18  
Prodotto: Rasoio elettrico Smooth (`SHAVE-SMOOTH`)  
Esito iniziale atteso: `escalation_operatore`

> Per l'ordine #1025 mi avete inviato il modello sbagliato, diverso da quello acquistato.

## 19. secondo_danno_trasporto - ordine #1026

Cliente: Federica Demo 19  
Prodotto: Aspirapolvere Compact (`HOME-VAC-COMPACT`)  
Esito iniziale atteso: `chiedi_foto_video`

> Ordine #1026: il corpo dell'aspirapolvere e arrivato crepato e il pacco era schiacciato.

Dopo **Conferma prove ricevute**: `procedi_rimborso`.

## 20. secondo_recesso_standard - ordine #1027

Cliente: Giorgio Demo 20  
Prodotto: Bollitore Glass (`KITCHEN-KETTLE`)  
Esito iniziale atteso: `procedi_rimborso`

> Ordine #1027: il bollitore funziona bene, ma ho cambiato idea e desidero restituirlo.

## Due test senza ordine Shopify dedicato

> Vorrei restituire il prodotto perche ho cambiato idea.

Atteso: richiesta del numero d'ordine.

> L'ordine #000000 contiene un prodotto difettoso che non si accende.

Atteso: ordine non trovato e richiesta di verificare il numero.
