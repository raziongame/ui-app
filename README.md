# SAP Assistant · white-label demo on the Graphene engine

Built by the Graphene team to show how a customer-facing AI application runs on Graphene. The UI
is white-label: **Cloudbox · SAP Assistant** on `/` (never names Graphene) and **Graphene by
NiraamAI** on `/direct`, from `brands.yaml`. It takes a question, lets Claude call Graphene's MCP
tools, and answers in the first person, with a collapsed "How do I know?" for the sources. Every
SAP fact comes from Graphene; answers not backed by the facts are replaced by "I'm not certain…".

## Run

```bash
make setup        # uv sync (Python 3.12)
make test         # mocked Claude, real Graphene MCP — no API spend
make run          # http://127.0.0.1:8080 (localhost only)
make latency      # Graphene tool latency on one warm MCP session
make eval-estimate  # token count + cost estimate for the golden questions (free)
make eval-cases CASES="id ..."  # while fixing: only the affected golden questions, cap $1 per run
make eval         # every golden question in both skins (costs ~$0.9): only before a tag
```

## Service (systemd user unit)

The app runs as the user service `sap-assistant` (unit: `deploy/sap-assistant.service`). Linger is on
for `grpadm`, so it starts at boot without a login and restarts 5 s after a crash, with no limit on
how often. It opens 127.0.0.1:8080 at once. Questions wait until Graphene is ready: the app retries
`graphene-mcp` and Graphene's `status` with backoff (1, 2, 4 … 30 s) until Graphene reports Neo4j
and Postgres up. `/api/health` and the UI's status line show `ready` and the reason while it waits.

```bash
make install-service                          # install/update the unit, daemon-reload, enable at boot
systemctl --user start sap-assistant          # start
systemctl --user stop sap-assistant           # stop
systemctl --user restart sap-assistant        # restart (after a code change or git pull)
systemctl --user status sap-assistant         # status
journalctl --user -u sap-assistant -f         # logs, live (Graphene connect/retry, errors)
journalctl --user -u sap-assistant -b         # logs since boot
curl -s http://127.0.0.1:8080/api/health      # ready? ("graphene": {"ready": true, ...})
systemctl --user disable --now sap-assistant  # stop and don't start at boot
```

Questions, tool calls and timings still go to `logs/app.jsonl`. Stop the service before `make run`
(both use port 8080).

From the operator laptop: `ssh -p 2222 -L 8080:127.0.0.1:8080 grpadm@127.0.0.1`, then http://localhost:8080.

`.env` (chmod 600, gitignored) holds `ANTHROPIC_API_KEY`; see `.env.example`.

## How it works

- `app/mcp_client.py` — spawns `graphene-mcp` **once** at app start (stdio), keeps that session for all
  questions, watches the process and respawns only if it dies (one retry for a call cut off by that).
- `app/server.py` — readiness: until Graphene's own `status` says Neo4j and Postgres are up, questions
  get "Graphene isn't ready yet" and the app retries with backoff (it never connects to those itself).
- `app/agent.py` — per question: a fresh conversation with `claude-sonnet-5-5` (adaptive thinking,
  effort medium), Graphene's own tool schemas, max 8 tool calls, every call timed; the final answer
  is JSON (`answer`, `claims[].evidence_ids`) via structured output.
- `app/contract.py` — adds `tool_trace`, and `coverage` / `freshness` / `limits` copied verbatim from
  Graphene's results.
- `app/grounding.py` — flags any object name, pseudonym, number or evidence id in the answer that is
  not in this question's tool results; the skins then show the fallback answer (reasons in the log).
- `app/present.py` — what a skin shows: plain answer, "How do I know?" sources, learned areas; silent
  safety (unbacked names/numbers, "trained", tool names, JSON words, the product name in the partner
  skin → the fallback answer). `app/brand.py` + `brands.yaml` — skins, colours, wording rules.
- `app/static/index.html` — one self-contained template for both skins (no CDNs), dark/light,
  neutral progress lines. Logos go in `static/brand/` (only names listed in `brands.yaml` are served).

Logs (`logs/app.jsonl`) hold questions, tool calls, timings and token usage — never the key.
Problems: app issues are fixed here (`TEST_LOG.md`); wrong or missing facts go to `GRAPHENE_ISSUES.md`.
