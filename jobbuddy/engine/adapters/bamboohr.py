from jobbuddy.engine.adapters.base import ATSAdapter

ADAPTER = ATSAdapter(
    name="bamboohr",
    url_patterns=(r"\.bamboohr\.com/careers",),
    key_rules={
        "firstName": "personal.first_name",
        "lastName": "personal.last_name",
        "email": "personal.email",
        "phone": "personal.phone",
        "city": "personal.location.city",
        "state": "personal.location.state",
        "zip": "personal.location.postal_code",
        "countryId": "personal.location.country",
        "resumeFileId": "__file_resume__",
        "coverLetterFileId": "__file_cover_letter__",
        "linkedinUrl": "personal.linkedin",
        "websiteUrl": "personal.website",
        "dateAvailable": "application_answers.start_date",
        "desiredPay": "application_answers.salary_expectation_text",
        "referredBy": "application_answers.how_did_you_hear",
    },
)
