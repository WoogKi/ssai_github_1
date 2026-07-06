# AGENTS.md

## Project

SSAI / SIMS AI project for pharmaceutical distribution ERP.

Main stack:

- Python
- Streamlit
- MSSQL
- LM Studio OpenAI-compatible API
- Company-specific ERP DB switching
- User-specific chat/storage separation

## Language

- Respond in Korean.
- Explain changes in this order:
  1. 수정 위치
  2. 수정 코드
  3. 테스트 방법
  4. 합격 기준

## Important files

- app/Lmstudio_SSAI_chat_main.py
- app/ui/sims_panel.py
- app/ui/chat_middleware.py
- app/ui/sims_entry.py
- app/ui/sims_hub.py
- app/ui/sims_table_display.py
- app/ui/current_table_followups/action_dispatcher.py
- app/db/mssql_client.py
- app/db/schema_map.py
- app/db/labels_map.py
- app/services/rddbc060_service.py
- app/services/ssai_auth_service.py
- app/services/ssai_storage_service.py
- app/services/ssai_permission_policy.py

## Safety rules

Never commit or expose:

- .env
- app/.env.sample
- logs/
- uploads/
- data/
- chat_rooms/
- database passwords
- real DB server IPs
- customer data
- exported Excel/CSV/ZIP files

Do not expose SIMS sensitive columns:

- Rd06_Password
- Rd06_Password_ENCrypt
- 비밀번호
- 주민번호
- Rd06_Jumin
- SMS/POL/Work password fields

Company isolation is mandatory:

- Current table context must belong to the selected company.
- Company change must clear previous current-table, context, export cache, and stale payload.
- Never reuse a previous company's table after company switch.

## SIMS UX policy

- SIMS panel is for query condition input and execution only.
- Query results must appear once in the chat.
- Query condition, summary, table, and download should be included in the chat message.
- Current-table follow-up context should remain available only for the current company.
- Avoid duplicate rendering.

## Development rules

- Prefer small, reviewable patches.
- Avoid broad common-module changes unless impact is clear.
- For stable screens, patch narrow service/view layer first.
- Keep existing Korean labels and ERP terminology consistent.
- Preserve left-pinned columns and current-table follow-up behavior.
- Do not change working UX unless explicitly requested.

## LM Studio rules

- Use LM Studio OpenAI-compatible endpoint from .env.
- Do not hardcode production secrets.
- Phase 3 operating assumptions:
  - Loaded model: google/gemma-3-27b
  - Parallel / Max Concurrent Predictions: 1
  - LLM_TIMEOUT_S=90
  - LLM_MAX_RETRY=1
  - SIMS_CODE_CACHE_TTL_S=300

## Required checks

Before reporting success for Python changes, run py_compile with:

.\venv\Scripts\python.exe -m py_compile app\Lmstudio_SSAI_chat_main.py app\ui\sims_panel.py app\ui\chat_middleware.py app\services\rddbc060_service.py app\services\ssai_auth_service.py app\services\ssai_storage_service.py app\db\mssql_client.py

For SIMS query changes, check logs for:

- WARNING
- ERROR
- Traceback
- company.change.clear_sims
- chat.sims.push
- stashed table
- llm.request

## Git rules

- Do not run git add .
- Add files selectively.
- Do not push to any remote unless explicitly approved.
- Phase 3 stable baseline:
  - commit: 33bc167
  - tag: phase3-stable-20260706

## Done means

A task is done only when:

- Changed files are listed.
- Syntax check passes.
- Relevant manual test passes.
- Logs contain no unexpected WARNING / ERROR / Traceback.
- Sensitive files are not staged.
