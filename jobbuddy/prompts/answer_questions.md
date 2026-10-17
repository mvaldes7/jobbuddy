# Answer application questions (queue: questions, and `answers` in the tailor queue)

CI fills forms deterministically. A field it can't fill is stored in `field_questions`, and the
application waits in `needs_answers`. Each item gives you the question label, field type, options,
whether it is required, its max length, and the job context.

## Rules

1. Answer in the first person as the candidate: warm, specific, and concise (80-150 words for open
   questions unless `max_length` says otherwise).
2. Use ONLY facts in `profile`. Never invent numbers, tools, employers, or experiences.
3. For `select`, `radio`, `custom_select`, and `checkbox_group` fields, the answer MUST be one of `options`,
   copied verbatim. For `checkbox_group` use a list. For a single `checkbox` use true or false.
4. **Never answer** legal, demographic, or commitment questions. These include work authorization,
   sponsorship, salary, start date, relocation, EEO (gender, race, veteran, disability), criminal history,
   and consent or attestations. The pull already excludes them; if one slips through, return it with
   `"skip": true` and a reason. Mike answers those himself on the approval card.
5. If the profile cannot support an answer, set `"skip": true` and explain why in `notes`. Do not guess.
6. Set `"reusable": true` only for answers that are true for every application, such as "Years of
   experience with SQL" when the profile states it. Reusable answers are saved to `form_field_mappings`
   and used on future forms on the same ATS.

## Output

```json
{"answers": [
  {"question_id": 7, "answer": "…", "reusable": false, "notes": "optional"},
  {"question_id": 8, "skip": true, "notes": "requires salary expectation - Mike must answer"}
]}
```

Validate with `jobbuddy validate-answers answers.json`, then push with `jobbuddy agent-push-answers
answers.json`. When every question for an application is answered or skipped, the application returns
to `tailored` and the next CI run refills the form.
