from jobbuddy.engine.adapters.base import ATSAdapter

ADAPTER = ATSAdapter(
    name="ashby",
    url_patterns=(r"jobs\.ashbyhq\.com",),
    key_rules={
        "_systemfield_name": "personal.full_name",
        "_systemfield_email": "personal.email",
        "_systemfield_phone": "personal.phone",
        "_systemfield_resume": "__file_resume__",
        "_systemfield_location": "__location__",
        "_systemfield_cover_letter": "__file_cover_letter__",
    },
    key_regex_rules=[(r"linkedin", "personal.linkedin")],
)
