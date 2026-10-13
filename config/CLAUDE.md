# config — provider configuration kept in git

| Path | Purpose |
| --- | --- |
| `vapi_tools.json` | Vapi tool definitions (`check_availability`, `create_appointment`, `update_lead_status`). Pushed to the Vapi assistant by `scripts/sync_vapi.py`. |
| `vapi_assistant_prompt.md` | The voice assistant's system prompt. Also synced by `scripts/sync_vapi.py`. |
| `n8n/workflows/*.json` | **Older** exports of the n8n workflows (intake, recovery, Sheet webhook, profile sync). |

## Notes

- Tool names and argument shapes in `vapi_tools.json` must match `routes/` and `vapi_contract.py`; see
  `tests/test_vapi_tool_config.py`.
- The n8n exports here are out of date: the live Sheet renamed "Action Status" to "Outcome Status" and the
  live workflows differ. The current production copies are in `../../n8n/` (outside this repo), and the live
  versions can be read through the `n8n-mcp` server. Do not import these files over the live workflows.
