# Prompts for the on-box AI agent

jobbuddy does not call any LLM API. An AI assistant on Mike's machine runs on a schedule, pulls work
from the database over Neon's HTTPS SQL endpoint, follows these files, and pushes the results back.

| Queue | Pull | Follow | Validate | Push |
|---|---|---|---|---|
| score | `jobbuddy agent-pull --kind score --limit 10` | `score_rubric.md` | (schema check on push) | `jobbuddy agent-push-scores scores.json` |
| tailor | `jobbuddy agent-pull --kind tailor --limit 3` | `tailor_resume.md`, `cover_letter.md` | `jobbuddy validate-tailoring tailoring.json` | `jobbuddy agent-push-tailoring tailoring.json` |
| questions | `jobbuddy agent-pull --kind questions --limit 20` | `answer_questions.md` | `jobbuddy validate-answers answers.json` | `jobbuddy agent-push-answers answers.json` |
| inbox | (agent reads Mike's JobAlerts mail itself) | — | — | `jobbuddy ingest-alert-email msg.eml` or `--json msg.json` |

Every pull prints one JSON document: `instructions` (the prompt text), `profile` (facts only, no
contact details), `items`, `output_schema`, and the `push_command`. Put the JSON you produce in a
file under a private temp directory, run the validator, fix anything it reports, then push. Delete the
temp files afterwards because they contain resume content.

`cover_letter.md` contains `{{VOICE_SAMPLE}}` and `{{PERSONAL_HOOK}}` placeholders. `agent-pull --kind
tailor` fills them from `profile/cover_letter_sample.txt` (or the `cover_letter_sample` document in the
DB) and `profile.voice.personal_hook`. Mike's real letter is PII, so it is never committed to this folder.
