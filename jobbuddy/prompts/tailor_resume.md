# Tailor the resume (queue: tailor)

For each item, produce a tailored resume, a cover letter (see `cover_letter.md`), and, if the posting
lists application questions, their answers (see `answer_questions.md`).

## Hard rules: no fabrication

1. You may only **reorder** and **reword** the candidate's existing bullets. Each output bullet must cite
   the `source_id` of the bullet it came from (`profile.experience[].roles[].bullets[].id`). Never merge
   facts from different bullets.
2. Never add numbers, tools, employers, certifications, technologies, or outcomes that are not in that
   bullet. Keep every number exactly as written.
3. Reword only to mirror the job's vocabulary where the meaning is identical, for example "spearheaded"
   vs. "led". If in doubt, keep the original text.
4. `skills_order` must be a reordering of `profile.skills`. You may drop irrelevant skills but never add one.
5. `summary` may only restate facts from `profile.summary` and the bullets.
6. List the bullets most relevant first. At most `max_bullets_per_role` per role (the pull output gives
   the limit).

`jobbuddy validate-tailoring FILE.json` checks every bullet against its source (number and term checks
plus a minimum word overlap). It also checks the summary and cover letter against the full profile, and
each answer. Any error blocks the push. Fix it and re-run until it prints `"ok": true`.

## Output

```json
{"items": [
  {"application_id": 42,
   "summary": "…",
   "skills_order": ["…"],
   "bullets": [{"source_id": "psa-mobile-app", "text": "…"}],
   "cover_letter": "Dear Hiring Team,\n\n…\n\nSincerely,\nFull Name",
   "answers": {"Why do you want to work here?": "…"}}
]}
```

`agent-push-tailoring` renders the DOCX and PDF locally, uploads them to `candidate_documents` linked to
the application, stores the text, and moves the application to `tailored`. CI then fills the form on its
next run.
