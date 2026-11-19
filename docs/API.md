# Contratto HTTP del gateway

Il server locale espone `GET /health`, `POST /v1/responses` e `POST /v1/embeddings` (di norma su `http://127.0.0.1:8000`). I nomi di modello pubblici e i relativi task sono in `models.toml`. Gli endpoint accettano JSON; i corpi con campi sconosciuti sono rifiutati. L'API è **specifica di questo progetto**: il nome `/v1/responses` non implica la piena compatibilità con la Responses API di OpenAI.

## `POST /v1/responses`

Campi obbligatori del corpo:

| Campo | Tipo e vincoli |
| --- | --- |
| `model` | Stringa non vuota; nome pubblico di una rotta con task `responses`. |
| `instructions` | Stringa non vuota con le istruzioni per il modello. |
| `input` | Lista non vuota di messaggi `{ "role": "system" | "user" | "assistant", "content": "..." }`; ogni `content` è non vuoto. |
| `text.format` | Oggetto con `type: "json_schema"`, `name` non vuoto, `strict: true` e `schema` (JSON Schema valido secondo Draft 2020-12). |
| `metadata` | Oggetto con `run_id` e `cache_namespace`, entrambi stringhe non vuote. |

Campi opzionali: `temperature` (numero fra `0` e `2`, oppure `null`) e `top_logprobs` (intero non negativo, oppure `null`). Non sono previsti streaming, tool call o `max_output_tokens` per richiesta: il limite di output è configurato in `gateway.toml`.

Esempio con la rotta locale `local/qwen2.5:3b`; richiede Ollama e il modello già disponibili:

```sh
curl -sS http://127.0.0.1:8000/v1/responses \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "model": "local/qwen2.5:3b",
  "instructions": "Rispondi soltanto in JSON.",
  "input": [{"role": "user", "content": "2 + 2 = 4?"}],
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
  "metadata": {"run_id": "prova-1", "cache_namespace": "gruppo-1"}
}
JSON
```

La risposta HTTP 200 valida ha questo involucro. Il valore di `output[0].content[0].text` è **una stringa contenente JSON**, da analizzare una seconda volta. Qui `false` è una decisione negativa autentica perché `parse_ok` è `true`:

```json
{
  "output": [{"type": "message", "content": [{"type": "output_text", "text": "{\"answer\":false}"}]}],
  "parse_ok": true,
  "provider_response": {"id": "esempio-provider"}
}
```

`provider_response` contiene l'oggetto grezzo restituito dal provider, quindi la forma e gli eventuali campi di usage o log-probability dipendono dal backend. Non esiste un campo normalizzato per token, costi o log-probability nella risposta di `/v1/responses`; questi dati sono registrati nel log quando disponibili.

### Output non valido e fallback

Il gateway analizza il testo del modello come JSON e lo verifica contro lo schema richiesto. Se fallisce, riprova la generazione secondo `retries.invalid_output` in `gateway.toml`. Se un retry produce un output valido, la risposta finale è `parse_ok: true`; il log conserva il numero di `parse_retries`.

Quando tutti i tentativi producono output non valido e lo schema è supportato per il fallback, il gateway risponde comunque **HTTP 200**:

```json
{
  "output": [{"type": "message", "content": [{"type": "output_text", "text": "{\"answer\":false}"}]}],
  "parse_ok": false,
  "provider_response": {"id": "ultima-generazione-non-valida"},
  "error_type": "parse_error"
}
```

`parse_ok` ed `error_type` sono campi esterni al JSON contenuto in `text`; non vengono aggiunti a `{"answer":false}`. Un client HTTP diretto deve leggere `parse_ok`, non dedurre il successo dal solo valore booleano. `provider_response` si riferisce all'ultimo tentativo del provider, non al fallback.

Per una classificazione con schema `{ "label": { "type": "string", "enum": [...] } }`, il fallback usa una label riservata basata su `__PARSE_FAILURE__`; se quella stringa è già nell'enum viene aggiunto un prefisso `_` finché non collide più. La label di fallback è deliberatamente fuori dalle classi valide e può non soddisfare l'enum richiesto. Per altri schemi privi di fallback, dopo i retry il gateway restituisce HTTP 502 (`No fallback available`) e registra `parse_ok: false` con `error_type: "unsupported_fallback"`.

Una generazione valida viene memorizzata nella cache SQLite, comprese le informazioni di usage, retry e la risposta grezza del provider. Un fallback non viene memorizzato. Su cache hit il client riceve di nuovo `parse_ok: true`, lo stesso `output` e la risposta grezza conservata. Il log del cache hit riporta `attempts`, `parse_retries` e `other_retries` della chiamata originaria: per `responses`, `other_retries` è ricavato da `attempts - 1 - parse_retries`; per gli embeddings, da `attempts - 1` sulla voce che rappresenta la chiamata upstream. `cache_hit: true` indica che quei retry non sono stati eseguiti durante la nuova richiesta. Non esiste un indicatore `cache_hit` nel corpo HTTP: è nel log. La cache è separata per `cache_namespace`; `run_id` identifica la run per log e limite di tentativi, ma non fa parte della chiave di cache. Le chiavi dipendono da modello, messaggi, schema e parametri configurati.

### Log-probabilities

Una rotta può dichiarare `top_logprobs` in `models.toml`. Una richiesta con `top_logprobs` maggiore della capacità configurata riceve HTTP 422. Quando la rotta ha un valore positivo, l'adapter chiede al provider la profondità **configurata sulla rotta**, anche se il client omette il campo o chiede un valore inferiore. Le log-probability non vengono normalizzate dal gateway: se il provider le restituisce, si trovano nel suo `provider_response`, anche su cache hit. Il backend `microsoft_decision` non supporta token log-probability. La disponibilità effettiva dipende dal provider e dal modello.

## `POST /v1/embeddings`

Campi obbligatori: `model` (stringa non vuota, rotta con task `embeddings`), `input` (stringa non vuota oppure lista non vuota di stringhe non vuote) e `metadata` con `run_id` e `cache_namespace` come sopra. Campo opzionale: `encoding_format`, il cui unico valore ammesso è `"float"` (default). I campi sconosciuti sono rifiutati.

```sh
curl -sS http://127.0.0.1:8000/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "model": "local/nomic-embed-text",
  "input": ["primo testo", "secondo testo"],
  "metadata": {"run_id": "prova-1", "cache_namespace": "gruppo-1"}
}
JSON
```

Con il modello Ollama corrispondente disponibile, la risposta ha questa forma (vettori e conteggi qui solo illustrativi):

```json
{
  "object": "list",
  "model": "local/nomic-embed-text",
  "data": [
    {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
    {"object": "embedding", "index": 1, "embedding": [0.3, 0.4]}
  ],
  "usage": {"prompt_tokens": 4, "total_tokens": 4}
}
```

`data` mantiene ordine e indici dell'input. I testi identici nella stessa richiesta vengono deduplicati per le chiamate upstream, ma ciascuna posizione conserva il proprio elemento `data` e la propria riga di log. La cache SQLite è per singolo testo e namespace. `usage` è presente solo se il gateway conosce i token delle nuove chiamate effettuate per **questa** richiesta; i cache hit non vengono conteggiati. Può quindi essere assente oppure riportare zero quando tutto arriva dalla cache. Google GenAI accetta al massimo `guardrails.max_google_embedding_texts` testi per richiesta e invia separatamente ogni testo mancante.

## Errori e log

| HTTP | Casi principali |
| --- | --- |
| 200 | Risposta valida, cache hit o fallback supportato dopo parse error; controllare `parse_ok` per `/v1/responses`. |
| 413 | Corpo oltre il limite configurato (`max_body_bytes` per responses, `max_embeddings_body_bytes` per embeddings). |
| 404 | Modello pubblico sconosciuto. |
| 422 | Corpo non valido, schema JSON non valido, task errato o opzione non supportata dalla rotta. |
| 429 | Limite di frequenza/concorrenza del provider o tentativi per `(run_id, model)` esauriti. |
| 501 | Backend non implementato per il task richiesto. |
| 502 | Errore upstream/trasporto, risposta upstream malformata o fallback non disponibile. |
| 503 | Voce di cache corrotta; il gateway non effettua una nuova chiamata upstream. |

Gli errori HTTP hanno in genere un corpo `{"detail": ...}`. Errori di trasporto, timeout e budget non diventano `parse_error`: nei log hanno `parse_ok: null` e un `error_type` distinto. Il file JSONL configurato in `[logging]` contiene una riga per chiamata a `/v1/responses` che raggiunge la fase di cache/provider e una per posizione di input di un batch `/v1/embeddings` completato; richieste rifiutate prima di questa fase possono non avere una riga. I campi `run_id`, `cache_namespace`, `model`, `key`, `cache_hit`, `parse_ok`, `parse_retries`, `other_retries`, `attempts`, `error_type`, usage, `latency_s` e costi consentono di identificare i fallback e contare i retry effettivamente eseguiti. Nelle risposte `latency_s` è la durata dell'ultima invocazione del provider. Nei batch di embedding è la quota della durata dell'ultima invocazione attribuita a ciascun testo in proporzione ai suoi token, come per il costo; con token ignoti o totali pari a zero, la durata è ripartita in parti uguali. Il valore è conservato per diagnostica nella cache e nel log; un cache hit copia la quota originaria. Le righe di cache preesistenti mantengono il valore di latenza registrato al momento della creazione. Il fallback registra `parse_ok: false` e `error_type: "parse_error"`. Il checker esterno esporta il fallback come record `calls` senza `error_type` né `latency_s`, accetta più invocazioni della stessa chiave con esiti diversi e ricava `n_parse_retries` e `n_other_retries` dai log. Il solo tempo nei run file e nei risultati è `run.wallclock_s`. I campi `tokens_in_final`/`tokens_out_final` e `tokens_in_last`/`tokens_out_last` identificano i token del tentativo terminale da usare nel run file; i retry precedenti non vengono sommati nelle metriche principali.

## Binding dei database

Per Neo4j GenAI configurare `genai.openai.baseurl` verso il gateway locale e inoltrare `instructions`, `metadata.run_id` e `metadata.cache_namespace` tramite `vendorOptions`. Il plugin richiede `output[0].type: "message"`. Cypher restituisce la mappa validata, per esempio `{answer: false}` o `{label: "__PARSE_FAILURE__"}`: `parse_ok` ed `error_type` non sono esposti nella mappa. Per il booleano, il log JSONL identificato da `run_id` permette di distinguere il fallback da una decisione negativa autentica. Un client HTTP diretto può invece leggere `parse_ok` nel corpo della risposta.

Per RDFLib e Oxigraph, le funzioni SPARQL devono effettuare richieste HTTP al gateway. Il repository non include gli operatori degli studenti.
