# Graphene by NiraamAI · SAP assistant

A chat app (built by the Graphene team) that runs on the Graphene engine over MCP: a full SAP expert
assistant that has learned one SAP system. It answers questions about that system (facts only from
Graphene's tools), general SAP questions (as general knowledge), and questions about pasted SAP Notes,
errors and logs. Chats are stored per browser on the server. Statements about the system are checked
silently; unbacked ones are rewritten or held back. `?debug=1` shows sources, grounding and the trace.

## Run

```bash
make setup        # uv sync (Python 3.12)
make test         # mocked Claude, real Graphene MCP — no API spend
make run          # http://127.0.0.1:8080 (localhost only)
make latency      # Graphene tool latency on one warm MCP session
make eval-estimate  # token count + cost estimate for the golden questions (free)
make eval-cases CASES="id ..."  # while fixing: only the affected golden questions, cap $1 per run
make eval         # every golden question (costs ~$0.55): only before a tag
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
- `app/server.py` — readiness: until Graphene's own `status` says Neo4j and Postgres are up, messages
  get "I'm still connecting to your SAP system" and the app retries with backoff (it never connects to those itself).
- `app/agent.py` — per message: the chat's earlier turns as context, `claude-sonnet-5-5` (adaptive
  thinking, effort low), Graphene's own tool schemas, max 8 tool calls, every call timed; one silent
  rewrite if a statement about this system isn't backed; the final answer
  is JSON (`answer` in markdown, `system_facts[]` citing results by number) via structured output.
- `app/contract.py` — adds `tool_trace`, and `coverage` / `freshness` / `limits` copied verbatim from
  Graphene's results.
- `app/grounding.py` — checks the statements about this system against the chat's results and the
  user's own messages (general SAP knowledge is not checked).
- `app/present.py` — the markdown the user sees (plus the app's history table), holding back what is
  still unbacked after the one rewrite; the debug record. `app/wording.py` — wording rules.
- `app/store.py` — chats per browser in SQLite (`data/chats.sqlite3`), with each chat's engine results.
- `app/static/index.html` — the chat page (no CDNs, inline markdown renderer), dark/light. The logo is
  `static/brand/logo.png`.

Logs (`logs/app.jsonl`) hold questions, tool calls, timings and token usage — never the key.
Problems: app issues are fixed here (`TEST_LOG.md`); wrong or missing facts go to `GRAPHENE_ISSUES.md`.
