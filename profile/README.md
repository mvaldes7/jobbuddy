# profile/

This folder holds Mike's PII and is git-ignored except for this README and `profile.example.yaml`.

| File | Purpose | Committed? |
|---|---|---|
| `profile.yaml` | Source of truth for every fact jobbuddy may claim | **No** |
| `resume.pdf` | Original resume (upload to DB with `jobbuddy upload-document`) | **No** |
| `resume_or_cl.docx` | Sample cover letter showing Mike's voice | **No** |
| `cover_letter_sample.txt` | Plain text extracted by `jobbuddy extract-style` | **No** |
| `profile.example.yaml` | Fictional example used by tests | Yes |

In CI the profile comes from the `CANDIDATE_PROFILE_JSON` secret
(`jobbuddy profile-json > profile.json` prints it) and documents come from the
`candidate_documents` table.
