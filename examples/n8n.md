# Optional n8n example

Import `n8n-investigation.json` into an existing n8n instance. It is optional;
Metric Detective does not need n8n, and Compose does not install it.

1. Start the Metric Detective API and worker.
2. In **Configure request**, set `api_url`. The default reaches a host API from a
   Docker Desktop container. For a native n8n process use `http://127.0.0.1:8000`.
3. Confirm the scenario ID and configuration against `GET /scenarios`.
4. Run the workflow manually. It creates a baseline investigation, waits, polls
   the returned ID and prepares a notification payload.
5. Inspect the final output. It has `prepared_only: true` and `sent: false`; no
   message is sent. Add a destination only after deciding where it should go.

The workflow polls at five-second intervals, stops after 91 pending observations
or a terminal status, and has a ten-minute execution limit. Polling timeout does
not cancel the investigation. HTTP errors stop the workflow rather than being
reported as successful analyses. To use the live investigator, change `mode` to
`agent` after configuring and testing Ollama.

The JSON is a portable example using standard HTTP Request, Wait, If and Code
nodes. Import/execution in a live n8n server is not part of the application's CI;
its validation status is documented in the verification record.

References: [HTTP Request node](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/),
[Wait node](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.wait/).
