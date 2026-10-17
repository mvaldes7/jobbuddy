from jobbuddy.engine.adapters.base import ATSAdapter

ADAPTER = ATSAdapter(
    name="greenhouse",
    url_patterns=(r"boards\.greenhouse\.io", r"job-boards\.greenhouse\.io", r"grnh\.se", r"gh_jid="),
    key_rules={
        "first_name": "personal.first_name",
        "last_name": "personal.last_name",
        "preferred_name": "personal.preferred_name",
        "email": "personal.email",
        "phone": "personal.phone",
        "resume": "__file_resume__",
        "cover_letter": "__file_cover_letter__",
        "candidate-location": "__location__",
        "job_application[location]": "__location__",
    },
    key_regex_rules=[
        (r"linkedin", "personal.linkedin"),
        (r"(website|portfolio)", "personal.website"),
    ],
)
