# LLM Gateway

Gateway HTTP locale per il progetto universitario sui semantic operator. Ogni studente avvia la propria istanza; le chiamate ai modelli passano per `/v1/responses` o `/v1/embeddings`. Le risposte sono conservate in SQLite e le chiamate sono registrate in JSONL. La cache di gruppo su Backblaze B2 si sincronizza solo con `cache pull` e `cache push`.

Per partire da un repository appena clonato, seguire il [manuale operativo del gateway](docs/MANUALE_GATEWAY.md).

## Requisiti e installazione

- `uv` installato; `setup.sh` seleziona Python 3.12 (uv può scaricarlo se manca).
- Per i modelli Ollama, un server Ollama locale e i modelli indicati in `models.toml`.
- Le credenziali cloud e B2 servono solo per le rispettive funzionalità.

Da una copia pulita del repository, `setup.sh` crea `gateway.toml` e `models.toml` dagli omonimi file `.example` senza sovrascrivere eventuali configurazioni locali:

```sh
bash setup.sh
source ./activate.sh
doctor
```

`bash setup.sh` è l'unico comando necessario per installare il gateway: esegue `uv sync --locked --python 3.12` e installa il package, i provider configurabili, la sincronizzazione B2 e `pytest` nella `.venv`. **Non installa Neo4j, GraphDB/RDF4J, RDFLib o PyOxiGraph.** Neo4j e GraphDB sono server esterni che ogni gruppo installa e configura autonomamente. Per scegliere un altro Python compatibile: `SEMOPS_PYTHON_VERSION=3.13 bash setup.sh`. I comandi in `bin/` usano direttamente `.venv/bin/python`; non occorre impostare `PYTHONPATH`.

Chi usa i binding RDF Python può aggiungerli separatamente (sono facoltativi e non servono a chiamare le API HTTP del gateway):

```sh
uv sync --locked --group rdflib       # solo RDFLib
uv sync --locked --group oxigraph     # solo PyOxiGraph
uv sync --locked --group rdflib --group oxigraph  # entrambi
```

`uv sync` sincronizza esattamente l'ambiente: se si esegue nuovamente `bash setup.sh`, i gruppi facoltativi vengono rimossi e vanno selezionati di nuovo. Anche `uv run` va invocato con i gruppi facoltativi necessari allo script, per esempio `uv run --locked --group rdflib python script.py`. `source ./activate.sh` aggiunge i comandi `start`, `cache`, `doctor` e `runs` al `PATH`, ma non installa pacchetti.

## Configurazione e avvio

`gateway.toml` contiene porta, cache, log, limiti, retry e impostazioni dei provider. `models.toml` associa i nomi pubblici dei modelli al backend, al modello upstream e alle opzioni. Modificare le copie locali secondo il proprio ambiente; i file `.example` restano condivisi. Le rotte cloud possono restare nel file anche senza credenziali: un modello locale è utilizzabile senza configurare Google o Azure. Per Google GenAI, impostare `google.project_id` in `gateway.toml` e le credenziali Google richieste dall'ambiente; per Azure, completare `azure.local.env` con la variabile nominata da `api_key_env` nella rotta, oppure esportarla nell'ambiente. `start server` carica il file Azure se presente e anche `google.local.env` se presente. I file `*.local.env` sono ignorati da Git.

```sh
start server
```

In un secondo terminale:

```sh
source ./activate.sh
start status
curl http://127.0.0.1:8000/health
```

La risposta attesa da `/health` è `{"status":"ok"}`. Gli schemi delle richieste sono visibili anche su `http://127.0.0.1:8000/docs`. Le richieste HTTP richiedono i campi `metadata.run_id` e `metadata.cache_namespace`.

Il contratto effettivamente implementato, inclusi fallback e codici di errore, è in [docs/API.md](docs/API.md).

Esempio di chiamata HTTP a un modello locale, dopo aver avviato Ollama e scaricato `qwen2.5:3b`:

```sh
curl -X POST http://127.0.0.1:8000/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"local/qwen2.5:3b","instructions":"Rispondi in JSON","input":[{"role":"user","content":"2 + 2 = 4?"}],"text":{"format":{"type":"json_schema","name":"answer","strict":true,"schema":{"type":"object","properties":{"answer":{"type":"boolean"}},"required":["answer"],"additionalProperties":false}}},"metadata":{"run_id":"prova-1","cache_namespace":"locale"}}'
```

Per gli embedding, usare lo stesso `metadata` con `POST /v1/embeddings` e un corpo come `{"model":"local/nomic-embed-text","input":["primo testo","secondo testo"],"metadata":{"run_id":"prova-1","cache_namespace":"locale"}}` (richiede il modello Ollama corrispondente).

## Cache B2 e strumenti universitari

La cache locale funziona senza B2. Per sincronizzarla manualmente, impostare `enabled = true` e la destinazione in `[cache_sync]` di `gateway.toml`, completare `b2.local.env` con `SEMOPS_STUDENT_ID`, `B2_ACCESS_KEY_ID` e `B2_SECRET_ACCESS_KEY`, quindi usare `cache status`, `cache pull` o `cache push`. L'avvio del server non effettua sincronizzazioni.

La sequenza di lavoro tra studenti e la gestione dei conflitti sono descritte in [docs/CACHE_B2.md](docs/CACHE_B2.md).

I comandi `runs check` e `runs build` richiedono il file esterno `semops_check.py` nella radice del repository. Il checker viene distribuito separatamente dal gateway. La CLI del gateway non richiede RDFLib, PyOxiGraph, Neo4j o GraphDB.

## Test

```sh
uv run --locked pytest -q
```

I test usano provider simulati e directory temporanee; non richiedono credenziali cloud, Ollama o B2. Non verificano la raggiungibilità o il comportamento reale dei provider.
