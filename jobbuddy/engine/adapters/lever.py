from jobbuddy.engine.adapters.base import ATSAdapter

ADAPTER = ATSAdapter(
    name="lever",
    url_patterns=(r"jobs\.lever\.co",),
    key_rules={
        "name": "personal.full_name",
        "email": "personal.email",
        "phone": "personal.phone",
        "org": "experience.0.company",
        "location": "__location__",
        "resume": "__file_resume__",
        "urls[LinkedIn]": "personal.linkedin",
        "urls[Portfolio]": "personal.website",
        "urls[Other]": "personal.website",
        "eeo[gender]": "application_answers.eeo.gender",
        "eeo[race]": "application_answers.eeo.race_ethnicity",
        "eeo[veteran]": "application_answers.eeo.veteran_status",
        "eeo[disability]": "application_answers.eeo.disability_status",
    },
    # Lever's "Additional information" textarea (name=comments) is where a cover letter goes.
    key_regex_rules=[(r"^comments$", "__text_cover_letter__")],
    confirmation_patterns=(r"application submitted", r"thanks for applying", r"thank you for (applying|your application)"),
)
