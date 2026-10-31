# API walkthrough

Start the API and worker using the README. The interactive OpenAPI documentation
is at [localhost:8000/docs](http://localhost:8000/docs).

```sh
curl http://localhost:8000/health
curl http://localhost:8000/health/details
curl http://localhost:8000/scenarios
curl -X POST http://localhost:8000/investigations \
  -H "Content-Type: application/json" \
  --data-binary @examples/request.json
```

These examples use Compose's API port 8000. The supervised native launcher uses
18000 by default. `GET /health/details` reports database, worker heartbeat and
configured Ollama/model availability. A reachable API does not alone establish
that a worker or model is ready. The UI shows these states separately; baseline
investigations do not need the model.

`POST /investigations` returns HTTP 202 with `id` and queued status. It does not
wait for the model. Replace `JOB_ID` below with the returned ID:

```sh
curl http://localhost:8000/investigations/JOB_ID
curl http://localhost:8000/investigations/JOB_ID/report.json -o report.json
curl http://localhost:8000/investigations/JOB_ID/report.html -o report.html
curl http://localhost:8000/investigations
```

Exports return HTTP 409 while no final report is available. A terminal report can
be `completed`, `partial` or `blocked_data_quality`; a job can also be `failed`.
Read limitations and errors rather than treating every terminal state as a
successful business diagnosis. `GET /replay` reads a previously saved report and
does not start new work.

To upload a compatible dataset:

```sh
curl -X POST http://localhost:8000/datasets \
  -F "orders=@data/mobile_drop/orders.csv;type=text/csv" \
  -F "loads=@data/mobile_drop/loads.csv;type=text/csv" \
  -F "config=<data/mobile_drop/config.json"
```

The result contains a new dataset `id` and validated `config`. Use both in a new
investigation request. The metric definition must match the definition stored
with the validated dataset. `loads` may be omitted; that leaves completeness
unverified and is reported explicitly. A malformed CSV/configuration is rejected
before analysis. Uploaded filenames never determine the server path.

`examples/request.json` chooses the live agent. To run without a model, change
`mode` to `baseline`. To explore a recorded model run without starting one, use
the UI's **Reproducción grabada** page. These are distinct modes.

Default upload limit is 10 MiB combined; ordinary request bodies are bounded
separately. The API is a trusted local MVP with no user authentication. Compose
publishes loopback ports and persistent development volumes.
