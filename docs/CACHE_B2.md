# Cache di gruppo su Backblaze B2

Ogni studente esegue il proprio gateway locale con cache SQLite. Gli studenti di uno stesso gruppo condividono un bucket/account B2 per scambiare **solo le voci di cache**; log JSONL e file di run non vengono sincronizzati. Il server non accede automaticamente a B2: il trasferimento avviene soltanto con i comandi manuali qui descritti.

## Configurazione

La sezione `[cache_sync]` di `gateway.toml` contiene `enabled = true`, bucket, endpoint B2, `compatibility_id`, percorso di `credentials_file` e limiti delle dimensioni degli archivi. Ogni studente deve creare localmente il file indicato (di default `b2.local.env`) con:

```dotenv
SEMOPS_STUDENT_ID=identificativo-univoco-studente
B2_ACCESS_KEY_ID=chiave-condivisa-del-gruppo
B2_SECRET_ACCESS_KEY=segreto-condiviso-del-gruppo
```

Il file non va committato. Gli studenti dello stesso gruppo usano bucket, endpoint e `compatibility_id` uguali, ma ciascuno usa un `SEMOPS_STUDENT_ID` distinto. La sincronizzazione rifiuta di cambiare destinazione o identità studente per un database locale già associato a un'altra destinazione. Gli schemi delle tabelle cache devono coincidere tra le istanze.

## Comandi

Dopo `source ./activate.sh`:

```sh
cache status
cache pull
cache push
```

I tre comandi accettano `--config PERCORSO` (default `gateway.toml`). `cache status` mostra conteggi di righe locali, sincronizzate, in coda, nuove, archivi noti e conflitti; è un controllo locale, ma richiede comunque il file delle credenziali perché la configurazione B2 viene validata all'avvio del comando. `cache pull` elenca e importa gli archivi del gruppo non ancora noti. `cache push` pubblica le voci locali nuove. Nessuno dei due comandi esegue automaticamente l'altro.

Una sequenza pratica prima di lavorare è `cache pull`; al termine, `cache push`. Ripetere `cache pull` dopo i push degli altri studenti per ricevere i loro aggiornamenti. Il solo `cache status` non modifica oggetti remoti.

## Aggiornamenti concorrenti e conflitti

Ogni push crea archivi compressi con una chiave B2 distinta che include `compatibility_id`, `SEMOPS_STUDENT_ID` e un UUID. Un push successivo di un altro studente non sovrascrive l'archivio precedente. I pull successivi importano le voci mancanti: **gli aggiornamenti con chiavi di cache diverse sono preservati**, anche se gli studenti fanno push senza aver fatto pull prima. L'importazione verifica dimensione e SHA-256 degli archivi.

Quando due istanze hanno una voce con la stessa combinazione `(tipo, cache_namespace, chiave)` ma righe diverse, il pull **mantiene la riga locale** e registra un conflitto in `cache status`; non esegue una sostituzione automatica. Il confronto riguarda l'intera riga, quindi anche differenze di testo generato, token, latenza o risposta grezza possono produrre conflitti. Se entrambi hanno già fatto push, entrambi gli archivi restano nel bucket, ma le cache locali possono continuare a dare risultati differenti. Se un'istanza importa la riga dell'altra prima di generare quella richiesta, il cache hit riduce questa possibilità. Le righe in conflitto sono escluse dalla selezione dei nuovi push locali; la risoluzione dei conflitti non è implementata.

Un fallback per parse error non viene memorizzato nella cache delle risposte e quindi non viene pubblicato su B2.
