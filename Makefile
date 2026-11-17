.PHONY: setup test run eval eval-estimate latency install-service
export VIRTUAL_ENV :=
UV := uv run --frozen

setup:
	uv sync

# Mocked Claude, real Graphene MCP. No API spend.
test:
	$(UV) pytest -q

run:
	$(UV) uvicorn app.server:app --host 127.0.0.1 --port 8080

# Token count + cost estimate for the golden questions (count_tokens is free; no answers generated).
eval-estimate:
	$(UV) python eval/run_eval.py --estimate

# Golden questions against the real Claude API (costs money: run eval-estimate first).
eval:
	$(UV) python eval/run_eval.py

# Graphene MCP tool latency on one warm session.
latency:
	$(UV) python scripts/measure_latency.py 4

# systemd user service (linger on: starts at boot, restarts on crash). See README "Service".
install-service:
	install -D -m 644 deploy/sap-assistant.service $(HOME)/.config/systemd/user/sap-assistant.service
	systemctl --user daemon-reload
	systemctl --user enable sap-assistant.service
