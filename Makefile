.PHONY: setup test run eval eval-cases eval-estimate latency install-service
export VIRTUAL_ENV :=
UV := uv run --frozen

setup:
	uv sync

# Mocked Claude, real Graphene MCP. No API spend.
test:
	$(UV) pytest -q

run:
	$(UV) uvicorn app.server:app --host 127.0.0.1 --port 8080

# Token count + cost estimate (count_tokens is free; no answers). CASES="id ..." to estimate only those.
eval-estimate:
	$(UV) python eval/run_eval.py --estimate $(if $(CASES),--only $(CASES))

# Cost rule (operator 2026-11-17): while fixing, only the affected golden questions, cap $1 per run:
#   make eval-cases CASES="impact_zs4_iot_data sm21_details"
eval-cases:
	@test -n "$(CASES)" || (echo 'CASES="id ..." required (golden case ids)'; exit 2)
	$(UV) python eval/run_eval.py --only $(CASES) --max-cost 1.00

# The FULL eval (every golden question, both skins; ~$0.9): only before a tag. Run eval-estimate first.
eval:
	$(UV) python eval/run_eval.py --full --max-cost 1.50

# Graphene MCP tool latency on one warm session.
latency:
	$(UV) python scripts/measure_latency.py 4

# systemd user service (linger on: starts at boot, restarts on crash). See README "Service".
install-service:
	install -D -m 644 deploy/sap-assistant.service $(HOME)/.config/systemd/user/sap-assistant.service
	systemctl --user daemon-reload
	systemctl --user enable sap-assistant.service
