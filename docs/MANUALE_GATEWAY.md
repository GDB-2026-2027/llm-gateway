# Manuale operativo del LLM Gateway

Questo manuale parte da una copia appena clonata del repository. Ogni studente esegue **un gateway sul proprio computer**. I client, inclusi i binding dei graph database, chiamano le sue API HTTP. Il gateway sceglie il provider in base al nome del modello, usa una cache SQLite locale e registra le chiamate in un log JSONL. La cache condivisa del gruppo su Backblaze B2 si scambia soltanto con comandi manuali.

Il repository fornisce già server HTTP, adapter dei provider, routing, validazione JSON, retry, cache, log e sincronizzazione B2. Gli studenti sviluppano invece la libreria degli operatori semantici, i binding o l'integrazione con il graph database e il codice che esegue le query e raccoglie i risultati. Il motore RDF/Neo4j valuta la query e i suoi binding; il codice degli operatori decide **quando** chiamare il gateway e costruisce prompt, schema e metadati; il gateway esegue la chiamata al modello. Le istanze degli studenti non comunicano direttamente fra loro: scambiano solo voci di cache mediante i comandi B2 della sezione 6.

## 1. Installare il gateway

Servono una shell Bash e `uv` disponibile nel terminale (`uv --version`). Se manca, installarlo prima di procedere; `setup.sh` segnala l'assenza e indica dove trovare le istruzioni di installazione. Il progetto richiede Python **almeno 3.12** e il setup seleziona 3.12 per impostazione predefinita (`SEMOPS_PYTHON_VERSION` permette di cambiarla). `uv` può scaricare Python se necessario. Gli script sono stati verificati su macOS; Linux con Bash è previsto dalla struttura degli script ma qui non è stato collaudato. Non c'è un setup nativo Windows documentato: usare una shell Unix compatibile e verificarla nel proprio ambiente.

Dalla **radice del repository clonato**: `setup.sh` crea `gateway.toml` e `models.toml` dalle copie `.example` solo se i file locali non esistono già.

```sh
bash setup.sh
source ./activate.sh
doctor
```

`bash setup.sh` è il comando che installa il gateway e le sue dipendenze nella `.venv`, usando `pyproject.toml` e `uv.lock`. `source ./activate.sh` aggiunge i comandi `start`, `cache`, `doctor` e `runs` al `PATH` del **solo terminale corrente**; ripeterlo in ogni nuovo terminale. `doctor` controlla file, TOML, rotte e listino, ma non prova l'autenticazione Google, i provider o B2. Gli avvisi su Azure o B2 mancanti sono normali se non li si usa. In alternativa si possono eseguire direttamente `bin/start`, `bin/cache`, `bin/doctor` e `bin/runs` dalla radice del repository.

L'installazione predefinita comprende FastEmbed, il supporto ai provider configurati, B2 e pytest. **Non installa Neo4j, GraphDB/RDF4J, RDFLib o PyOxiGraph.** I graph database sono esterni al gateway. Per chi sviluppa con i binding RDF Python, i gruppi facoltativi sono indipendenti:

```sh
uv sync --locked --group rdflib
uv sync --locked --group oxigraph
uv sync --locked --group rdflib --group oxigraph  # entrambi
```

`uv sync` sincronizza esattamente la `.venv`: un successivo `bash setup.sh` senza gli stessi gruppi può rimuovere quelli facoltativi. Anche con `uv run` indicare i gruppi richiesti dallo script, per esempio `uv run --locked --group rdflib python script.py`.

## 2. Avviare e controllare il server

Nel primo terminale, dalla radice del repository:

```sh
source ./activate.sh
start server
```

Tenere aperto questo terminale. Il server ascolta all'indirizzo di `[server]` in `gateway.toml`, inizialmente `http://127.0.0.1:8000`. Si ferma con `Ctrl+C`. In un secondo terminale:

```sh
source ./activate.sh
start status
curl -sS http://127.0.0.1:8000/health
```

`/health` restituisce `{"status":"ok"}` se il gateway è avviato; **non prova** che Ollama, Google, Azure o B2 siano raggiungibili. La pagina `http://127.0.0.1:8000/docs` mostra anche gli schemi HTTP. Se si cambia la porta in `gateway.toml`, cambiare la porta negli URL di questo manuale. `start status` legge la porta dalla configurazione.

Il server parte anche senza credenziali Google, Azure o B2. Le credenziali di un provider servono soltanto quando si usa una sua rotta; le credenziali B2 servono ai comandi di sincronizzazione. Non occorre installare un graph database per avviare o chiamare il gateway.

Se Neo4j gira in Docker e il gateway sull'host, `127.0.0.1` dentro il container indica **il container**, non l'host. Configurare `genai.openai.baseurl` con un indirizzo dell'host raggiungibile dal container (su Docker Desktop spesso `host.docker.internal`) e fare in modo che `[server].host` ascolti su un'interfaccia raggiungibile. Limitare l'accesso alla rete prevista: l'impostazione iniziale `127.0.0.1` accetta solo connessioni locali all'host.

## 3. Preparare un modello e fare la prima chiamata

`models.toml` definisce i **nomi pubblici** dei modelli, il backend, il modello upstream e le opzioni della rotta. Le richieste HTTP usano il nome pubblico. La rotta iniziale `local/qwen2.5:3b` punta a Ollama su `http://127.0.0.1:11434/v1`. Se si vuole provarla, installare e avviare Ollama separatamente, poi scaricare il modello:

```sh
ollama pull qwen2.5:3b
```

Ollama deve essere in esecuzione quando si invia la richiesta (su alcune installazioni parte come servizio; altrimenti usare `ollama serve`). Questo è un esempio completo per `POST /v1/responses`:

```sh
curl -sS http://127.0.0.1:8000/v1/responses \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "model": "local/qwen2.5:3b",
  "instructions": "Rispondi soltanto con JSON conforme allo schema.",
  "input": [{"role": "user", "content": "2 + 2 è uguale a 4?"}],
  "text": {"format": {
    "type": "json_schema",
    "name": "boolean_answer",
    "strict": true,
    "schema": {
      "type": "object",
      "properties": {"answer": {"type": "boolean"}},
      "required": ["answer"],
      "additionalProperties": false
    }
  }},
  "metadata": {"run_id": "prova-locale-1", "cache_namespace": "gruppo-demo"}
}
JSON
```

La risposta contiene `output[0].content[0].text`: è una **stringa che contiene JSON**, per esempio `{"answer":true}`. `parse_ok: true` indica che il modello ha prodotto un output valido. Ripetendo la stessa richiesta nello stesso `cache_namespace`, il gateway può servirla dalla cache senza richiamare il provider; `cache_hit` si vede nel log, non nella risposta HTTP.

`metadata.run_id` identifica una run nei log e nei limiti di tentativi; assegnare un identificatore riconoscibile a ogni esecuzione. `metadata.cache_namespace` separa le cache: usare lo stesso valore per richieste che devono condividere le risposte. `run_id` non fa parte della chiave della cache. **Entrambi i campi sono obbligatori** nelle richieste del gateway.

### Embedding

La rotta `local/nomic-embed-text` usa Ollama. Dopo `ollama pull nomic-embed-text`:

```sh
curl -sS http://127.0.0.1:8000/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"local/nomic-embed-text","input":["primo testo","secondo testo"],"metadata":{"run_id":"prova-embedding-1","cache_namespace":"gruppo-demo"}}'
```

La risposta contiene `data`, una lista di vettori nello stesso ordine dell'input, e `usage` quando i token delle nuove chiamate sono noti. Il gateway deduplica i testi identici nella stessa richiesta e conserva ciascun embedding in cache separatamente. La rotta `local/bge-small-en-v1.5` usa invece FastEmbed: il pacchetto è installato dal setup, ma i pesi del modello possono richiedere un download al primo uso.

### Quante chiamate partono da una query?

Il gateway non conosce i binding SPARQL/Cypher e **non implementa un batch di valutazioni indipendenti** per `/v1/responses`. Il campo `input` è una lista di messaggi di **una sola conversazione**: una richiesta HTTP produce un risultato strutturato, una chiave di cache e, su miss, una generazione (più gli eventuali retry). Se una query valuta un operatore su 1.000 binding e il codice dello studente invia una richiesta per binding, arrivano 1.000 richieste HTTP; i cache hit evitano chiamate upstream ma non le richieste HTTP. Il numero effettivo dipende dall'implementazione del binding o del plugin, non dalla sola cardinalità della query.

Se serve raggruppare più binding, la libreria degli operatori deve costruire e interpretare il prompt e lo schema per quel gruppo, oppure ridurre le valutazioni mediante un block join semantico nel proprio algoritmo. Il gateway tratterà comunque il gruppo come **una** risposta e non separerà cache, errori o retry per binding. Per esempio, un prompt che chiede le decisioni per 10 elementi può richiedere una lista nello schema, ma il codice dello studente deve associare ogni elemento alla decisione e gestire l'eventuale fallimento dell'intera risposta.

`/v1/embeddings` accetta invece una lista di testi in **una richiesta HTTP** e restituisce un vettore per posizione. Deduplica i testi uguali e consulta la cache **per testo**. Su miss, Ollama e FastEmbed ricevono un batch upstream di testi mancanti; Google riceve una chiamata upstream per ogni testo mancante (massimo 16 testi per richiesta HTTP nella configurazione iniziale). Il batch HTTP degli embedding, il batch upstream del provider e il block join semantico nel database sono quindi operazioni diverse. I retry e i limiti si applicano alle chiamate upstream effettive.

## 4. Cosa succede a ogni richiesta

1. Il gateway cerca il nome pubblico in `models.toml` e verifica il corpo della richiesta.
2. Calcola una chiave canonica e cerca la risposta nel namespace della cache SQLite (`data/cache.sqlite3` nella configurazione iniziale).
3. In caso di miss, applica limiti e retry, chiama il provider e verifica l'output richiesto. Per le risposte, una generazione con JSON non valido può essere ripetuta fino a `retries.invalid_output` volte (inizialmente 1, quindi al massimo 2 generazioni se non intervengono altri errori); per HTTP 429 e 5xx sono previsti fino a `retries.http_errors` tentativi aggiuntivi. Memorizza le risposte valide; **non memorizza un fallback di parsing come risposta valida**. Gli embedding non hanno un fallback di parsing.
4. Restituisce il risultato HTTP e scrive i dettagli in `data/calls.jsonl` (una riga JSON per evento secondo le regole di ciascun endpoint).

Per `/v1/responses`, `parse_ok: false` con `error_type: "parse_error"` indica che tutti i tentativi hanno prodotto un output non valido e il gateway ha restituito un fallback HTTP 200, se lo schema è supportato. Un `{"answer":false}` con `parse_ok: true` è invece una decisione negativa valida. Questi metadati sono **fuori** dalla stringa JSON in `output[0].content[0].text`. Per la classificazione, il fallback usa una label riservata derivata da `__PARSE_FAILURE__`, fuori dall'enum delle classi. Se lo schema non ammette un fallback implementato, il gateway restituisce HTTP 502 e registra `unsupported_fallback`.

Il client HTTP diretto può controllare `parse_ok`. Il plugin GenAI di Neo4j, nella versione verificata, espone in Cypher solo la mappa del contenuto strutturato: per distinguere un fallback booleano da un `false` valido occorre cercare la riga di log della chiamata tramite `run_id` e chiave/timestamp. Errori di trasporto, timeout o limiti restituiscono un errore HTTP, hanno `parse_ok: null` nel log quando la richiesta arriva alla fase di chiamata e non vanno contati come decisioni negative valide o fallback di parsing.

Il log JSONL riporta, tra gli altri, `run_id`, `cache_namespace`, `model`, chiave, `cache_hit`, `parse_ok`, tentativi, retry, token, costo stimato, `latency_s` ed eventuale `error_type`. Una richiesta `/v1/responses` arrivata a cache/provider produce una riga; un embedding riuscito produce una riga **per posizione di input**, anche se i testi sono duplicati. Le richieste rifiutate prima della fase di cache/provider possono non produrre una riga. Nei cache hit i conteggi di retry e la durata diagnostica provengono dalla chiamata originaria: `cache_hit: true` dice che **non** sono nuovi retry. `tokens_in` e `tokens_out` delle risposte aggregano l'usage noto dei tentativi; `tokens_in_final`/`tokens_out_final` indicano la generazione valida terminale, mentre `tokens_in_last`/`tokens_out_last` indicano l'ultima anche se non valida. `usage_complete` segnala se tutti i tentativi hanno usage noto.

Esempio sintetico di **una riga JSONL** coerente con un cache miss locale riuscito (chiave e token illustrativi):

```json
{"run_id":"prova-locale-1","endpoint":"responses","cache_namespace":"gruppo-demo","model":"local/qwen2.5:3b","key":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","cache_hit":false,"parse_ok":true,"tokens_in":12,"tokens_out":5,"latency_s":0.42,"cost_usd":0.0,"price_list_id":"local-dev-2026-11","attempts":1,"parse_retries":0,"other_retries":0,"ts":1794916800.0,"error_type":null,"error_detail":null,"tokens_in_final":12,"tokens_out_final":5,"usage_complete":true,"tokens_in_last":12,"tokens_out_last":5}
```

`latency_s` è la durata diagnostica dell'**ultima** invocazione al provider, non la somma dei retry. Per gli embedding serviti in batch la durata dell'ultima chiamata upstream è distribuita fra i testi in proporzione ai token (in parti uguali se i token non sono noti o sono zero). Nei run file e nei risultati l'unico tempo è `wallclock_s`, misurato dal codice che esegue l'intera query. Il log non contiene automaticamente quel tempo. Per il contratto completo, vedere [API.md](API.md).

## 5. Configurare rotte, provider e prezzi

- `gateway.toml` contiene porta, percorsi di cache e log, retry, limiti, listino, configurazione Google e destinazione B2.
- `models.toml` collega un nome pubblico a `backend`, `task`, `upstream_model` e, quando serve, `base_url`, `api_key_env` o opzioni di schema. Per aggiungere un modello dello stesso provider, aggiungere una rotta e richiamarne il nome pubblico. Le rotte cloud esistenti possono restare nel file anche senza credenziali.
- Il listino indicato in `[pricing].prices_path` serve per stimare i costi nei log; non è necessariamente la fattura effettiva del provider. Il listino iniziale `prices/test_prices.toml` contiene prezzi indicativi per le rotte cloud e zero per i modelli locali: aggiornare le tariffe prima di usare le stime economiche. Un modello senza prezzo nel listino può avere `cost_usd: null`.

Per una rotta Azure, impostare nell'ambiente la variabile nominata da `api_key_env` in `models.toml`, oppure inserirla in `azure.local.env` (letto da `start server` se presente). Gli URL Azure copiati da `models.toml.example` sono segnaposto: sostituirli con endpoint e deployment accessibili al gruppo. Non inserire segreti in `models.toml` o `gateway.toml` e non committare i file `*.local.env`. Le chiamate reali ai provider cloud possono avere un costo.

### Google Vertex AI

L'adapter `google_genai` usa l'SDK `google-genai` con `vertexai=True`, **progetto Google Cloud e località**: chiama Vertex AI, non la Gemini Developer API con API key. Nel proprio progetto Google Cloud, verificare che billing e Vertex AI API siano abilitati e che l'identità abbia i permessi necessari. Impostare il proprio `project_id` in `gateway.toml`, per esempio:

```toml
[google]
project_id = "ID_DEL_PROPRIO_PROGETTO"
location = "global"
```

La rotta `gemini-3.5-flash` in `models.toml` seleziona il modello per `/v1/responses`; `google/gemini-embedding-001` seleziona quello per `/v1/embeddings` e specifica `location = "europe-west4"`, che prevale sulla località generale. La presenza della rotta nel TOML non prova che modello e regione siano disponibili per il proprio progetto: verificarli nel catalogo Vertex AI del progetto. Il gateway non richiede `GOOGLE_API_KEY`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` o `GOOGLE_GENAI_USE_VERTEXAI`: progetto, località e scelta Vertex AI arrivano dal TOML e dal codice.

L'autenticazione prevista è [Application Default Credentials](https://docs.cloud.google.com/docs/authentication/application-default-credentials). Sul computer dello studente, un'opzione è eseguire `gcloud auth application-default login`; in alternativa, configurare un file di credenziali autorizzato tramite `GOOGLE_APPLICATION_CREDENTIALS`. Se si usa quest'ultima opzione, si può esportare la variabile nella shell **prima** di `start server` oppure creare `google.local.env` nella radice del gateway:

```dotenv
GOOGLE_APPLICATION_CREDENTIALS=/percorso/assoluto/alle/credenziali.json
```

`start server` legge quel file se esiste e dà precedenza alle variabili già esportate nell'ambiente. Non caricare il file di credenziali o `google.local.env` nel repository. Per un controllo senza inferenza, eseguire `doctor` e `start status`, quindi, se si usa `gcloud`, `gcloud auth application-default print-access-token >/dev/null`: quest'ultimo controlla l'accesso alle credenziali, **non** l'autorizzazione a uno specifico modello Vertex AI. Il gateway non offre un test di entitlement senza chiamata al modello. Per prerequisiti e disponibilità consultare la [documentazione Vertex AI](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/quickstarts/quickstart-multimodal) e quella delle [ADC](https://docs.cloud.google.com/docs/authentication/application-default-credentials).

Il corso indica circa **50 € di crediti Google Education** per studente: verificare nel proprio account Billing le condizioni, la scadenza e se il credito copre proprio l'uso di Vertex AI. Non è una garanzia fornita dal gateway. Google Cloud può avere budget e avvisi configurati nel progetto; i normali [avvisi di budget](https://docs.cloud.google.com/billing/docs/how-to/budgets) non sono di per sé un blocco automatico della spesa.

### Limiti e contabilità

`gateway.toml` configura dimensioni massime dei corpi, token massimi di output, frequenza, concorrenza, attesa massima e `guardrails.run_limits.default_max_attempts_per_model` (inizialmente 10.000 tentativi). Sono limiti **locali** dell'istanza: il conteggio per `(run_id, model)` è in memoria e riparte con il server. Non c'è nel gateway un limite di spesa in euro/dollari, né un budget cumulativo fra i gateway dei membri del gruppo. Eventuali quote e limiti del provider sono separati e vanno configurati presso il provider.

`cost_usd` nel log è una **stima** calcolata dall'usage che il provider comunica e dal listino locale; non è la fattura. Anche i tentativi falliti o con usage ignoto possono avere un costo reale non osservato. Un cache hit non invoca di nuovo il provider, ma il log riporta l'usage della voce originaria e ne ricalcola il costo **teorico** con il listino corrente. Nei run file il checker distingue il costo attribuito alle chiamate della run dal costo del piano che include il valore teorico delle risposte riutilizzate; nessuno dei due sostituisce la fatturazione del provider. Usare `cache_hit`, `usage_complete` e il listino identificato da `price_list_id` per interpretare questi numeri.

Neo4j e GraphDB si installano a parte. Il loro codice deve inviare richieste HTTP al gateway. Per il plugin GenAI di Neo4j, configurare `genai.openai.baseurl=http://127.0.0.1:8000/v1` in `genai.conf` e inoltrare `instructions`, `run_id` e `cache_namespace` tramite le opzioni del plugin. Controllare il comportamento con la versione di Neo4j usata dal gruppo. Cypher espone il contenuto strutturato, ma non `parse_ok` o `error_type`: per distinguere un fallback booleano da un `false` autentico consultare il log tramite `run_id`.

## 6. Sincronizzare manualmente la cache del gruppo

La cache funziona localmente senza B2. Per condividere le voci con i compagni, ogni studente imposta `enabled = true` e la destinazione comune del gruppo in `[cache_sync]` di `gateway.toml`, e completa **sul proprio computer** il file `b2.local.env`:

```dotenv
SEMOPS_STUDENT_ID=identificativo-unico-dello-studente
B2_ACCESS_KEY_ID=chiave-del-gruppo
B2_SECRET_ACCESS_KEY=segreto-del-gruppo
```

Gli studenti del gruppo condividono account/bucket, endpoint e `compatibility_id`, ma hanno `SEMOPS_STUDENT_ID` diversi. Devono concordare anche i nomi pubblici, gli upstream, le versioni dei modelli, la configurazione delle rotte e i namespace delle cache. `compatibility_id` è un prefisso degli oggetti remoti e una protezione contro destinazioni diverse; **non** verifica automaticamente che i modelli o le configurazioni coincidano. La chiave canonica incorpora richiesta e alcuni parametri della rotta, ma non `base_url` né la revisione effettiva dei pesi di un modello: una configurazione divergente può quindi riusare impropriamente una voce con la stessa chiave. Per esperimenti incompatibili usare un namespace distinto e, se cambia la destinazione B2 associata a una cache SQLite, una cache locale distinta.

Dopo `source ./activate.sh`:

```sh
cache pull    # prima di lavorare: importa le nuove voci del gruppo
cache status  # controlla lo stato locale
cache push    # dopo aver generato nuove risposte: pubblicale
```

Il gateway **non sincronizza automaticamente** all'avvio o durante le richieste HTTP. Il server non deve essere avviato per usare questi comandi. `cache pull` e `cache push` non si richiamano reciprocamente. Il solo `cache status` legge lo stato locale ma richiede comunque la configurazione B2 completa. Fare pull prima di iniziare e dopo i push degli altri; fare push dopo aver prodotto nuove risposte. Archivi diversi si accumulano senza sovrascriversi e voci con chiavi diverse vengono preservate. Se due studenti generano righe differenti per la stessa combinazione di tipo, namespace e chiave, il pull mantiene la riga locale e registra un conflitto: le due istanze possono continuare a restituire risposte diverse, senza risoluzione automatica. Si sincronizzano solo voci di cache valide, non fallback, log o run file. Per i dettagli operativi e i limiti, vedere [CACHE_B2.md](CACHE_B2.md).

## 7. Test e run file

Per eseguire i test automatici senza credenziali cloud:

```sh
uv run --locked pytest -q
```

I test usano provider simulati. Il gateway non produce da solo run file completi: gli studenti devono eseguire la query, registrare `started_at`, `finished_at` e `wallclock_s`, conservare la risposta finale in **SPARQL 1.1 Query Results JSON**, e associare le chiamate del gateway con lo stesso `run_id` (e il corretto `cache_namespace`). La chiave canonica della cache identifica una richiesta/modello, non la run: lo stesso `run_id` può contenere miss e hit. Nei run file `semops-run/1`, `calls` deriva dalle righe JSONL filtrate della run, proiettando soltanto i campi ammessi dalla specifica; `error_type`, `latency_s`, segreti e prompt non vanno copiati nei record `calls`. Il checker ricava fra l'altro `parse_failures` dalle chiavi `responses` distinte con `parse_ok: false`; un retry interno riuscito non conta come fallback definitivo. Usare la specifica `semops-run/1` fornita con il checker.

Il wrapper `runs` cerca `semops_check.py` nella **radice del gateway**. Il checker viene fornito separatamente dal gateway: collocarlo lì prima di usare i comandi. Per controllare run file già prodotti:

```sh
runs check
```

`runs check` invoca il checker sulla radice del repository e scrive `results.csv` con una riga per run utilizzabile. I run file devono già trovarsi nei percorsi `runs/` previsti dal checker, insieme ai file di supporto richiesti dalla specifica del corso.

`runs build` accetta:

```sh
runs build --manifest /percorso/manifest.json --answer /percorso/answer.json
```

Il manifest deve contenere i metadati effettivi della run: identificativi di gruppo, dominio, dataset, query, run e piano; `engine`, `model_oracle`, `language_expression`, `n_input_bindings`, `started_at`, `finished_at` e `wallclock_s`. Campi opzionali o dipendenti dal piano sono definiti dallo schema `RUN_FIELDS` del checker; il checker aggiunge i conteggi di retry dal log. `answer.json` deve contenere la risposta SPARQL JSON già prodotta dal codice degli studenti. Il checker legge log e listino configurati e crea `runs/<domain>/<data_set>/<query_id>/<run_id>.json`. Accetta anche `--candidates`, `--oracle-check`, `--log-path`, `--prices-path` e `--overwrite`. Il codice degli studenti resta responsabile dei metadati e del risultato della query. Usare metadati e risultati della propria esecuzione.

## 8. Problemi frequenti

| Sintomo | Controllo |
| --- | --- |
| `uv` non trovato | Installare `uv`, poi ripetere `bash setup.sh`. |
| `.venv` mancante | Eseguire `bash setup.sh` dalla radice del repository. |
| `start` non trovato | Eseguire `source ./activate.sh` nel terminale corrente oppure `bin/start server` dalla radice. |
| `/health` non risponde | Verificare che `start server` sia ancora in esecuzione e controllare host/porta in `gateway.toml`. |
| `/health` funziona ma Ollama no | Verificare che Ollama sia avviato, che il modello sia stato scaricato e che `base_url` in `models.toml` sia corretto. |
| HTTP 404 sul modello | Usare esattamente un nome pubblico presente in `models.toml`. |
| HTTP 422 | Controllare campi obbligatori, `metadata`, JSON Schema e task della rotta. |
| HTTP 429 o 502 | Controllare limiti, retry, provider e `data/calls.jsonl`; non interpretarlo automaticamente come parse error. |
| `cache` segnala credenziali mancanti | Creare `b2.local.env` e verificare `[cache_sync]`; senza B2, saltare i comandi `cache`. |
| `runs` segnala checker mancante | Collocare nella radice del repository il file `semops_check.py` distribuito separatamente. |
| Google restituisce un errore benché `/health` funzioni | Controllare `project_id`, ADC, API, permessi, località e disponibilità del modello nel proprio progetto; `doctor` non li verifica. |
