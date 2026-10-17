from jobbuddy.engine.adapters.base import ATSAdapter

ADAPTER = ATSAdapter(
    name="workday",
    url_patterns=(r"myworkdayjobs\.com", r"\.workday\.com", r"wd\d+\.myworkday"),
    key_rules={
        "legalNameSection_firstName": "personal.first_name",
        "legalNameSection_lastName": "personal.last_name",
        "preferredNameSection_firstName": "personal.preferred_name",
        "email": "personal.email",
        "phone-number": "personal.phone",
        "addressSection_city": "personal.location.city",
        "addressSection_postalCode": "personal.location.postal_code",
        "addressSection_countryRegion": "personal.location.state",
        "countryDropdown": "personal.location.country",
        "file-upload-input-ref": "__file_resume__",
        "linkedinQuestion": "personal.linkedin",
        "sourceDropdown": "application_answers.how_did_you_hear",
        "gender": "application_answers.eeo.gender",
        "ethnicityDropdown": "application_answers.eeo.race_ethnicity",
        "hispanicOrLatino": "application_answers.eeo.hispanic_latino",
        "veteranStatus": "application_answers.eeo.veteran_status",
        "disabilityStatus": "application_answers.eeo.disability_status",
    },
    next_automation_ids={"bottom-navigation-next-button", "pageFooterNextButton"},
    submit_automation_ids={"bottom-navigation-submit-button", "pageFooterSubmitButton"},
    confirmation_patterns=(r"application submitted", r"thank you for applying", r"successfully submitted"),
)
