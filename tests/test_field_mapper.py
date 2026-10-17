import json

from jobbuddy.engine.adapters.base import detect_adapter, get_adapter
from jobbuddy.engine.field_mapper import FieldMapper, MapperContext, low_confidence_items, match_option, needs_cover_letter
from jobbuddy.engine.form_inspector import classify_button
from jobbuddy.engine.models import Button, FormField, Option


def F(fid, label, type="text", options=None, **kw):
    kw.setdefault("name", fid)
    return FormField(field_id=fid, jb_id=fid, selector=f"#{fid}", tag="input", type=type, label=label,
                     options=[Option(label=o, value=o) for o in (options or [])], **kw)


def mapper(profile, adapter="greenhouse", llm=None, learned=None, overrides=None):
    return FieldMapper(MapperContext(profile=profile, adapter=get_adapter(adapter), job_title="PSA", company="Acme",
                                     llm=llm, learned=learned or {}, overrides=overrides or {},
                                     cover_letter_text=lambda: "Dear Team, ..."))


def test_match_option_synonyms():
    opts = [Option(label=x) for x in ["Please select", "Male", "Female", "I don't wish to answer"]]
    assert match_option("Decline to answer", opts)[0].label == "I don't wish to answer"
    yn = [Option(label="Yes"), Option(label="No")]
    assert match_option("No", yn)[0].label == "No"
    src = [Option(label=x) for x in ["LinkedIn", "Company Website", "Referral"]]
    assert match_option("Company careers page", src)[0].label == "Company Website"


def test_deterministic_rules_and_adapter(profile):
    fields = [F("first_name", "First Name"), F("last_name", "Last Name"), F("email", "Email", "email"),
              F("phone", "Phone", "tel"), F("resume", "Resume/CV", "file", required=True),
              F("cover_letter", "Cover Letter", "file"), F("q_li", "LinkedIn Profile"),
              F("gender", "Gender", "select", ["Please select", "Male", "Female", "Decline To Self Identify"])]
    ms = {m.field_id: m for m in mapper(profile).map_fields(fields)}
    assert ms["first_name"].value == "Alex" and ms["first_name"].source == "adapter"
    assert ms["email"].value == "alex.example@example.com"
    assert ms["resume"].file_kind == "resume" and ms["cover_letter"].file_kind == "cover_letter"
    assert ms["q_li"].value.endswith("/alex-example/") and ms["q_li"].source == "rule"
    assert ms["gender"].value == "Decline To Self Identify" and ms["gender"].confidence >= 0.8


def test_todo_values_never_filled(profile):
    f = F("auth", "Are you legally authorized to work in the United States?", "select", ["Yes", "No"], required=True)
    (m,) = mapper(profile).map_fields([f])
    assert m.value is None and m.source == "profile_todo" and m.confidence == 0
    items = low_confidence_items([f], [m], 0.75)
    assert items and items[0]["required"] and "TODO" in items[0]["suggestions"][0]


def test_human_override_and_learned_mapping(profile):
    class Row:
        profile_key = None
        static_value = "Oncology"
        confidence = 0.95

    f = F("q9", "Favorite clinical domain")
    (m,) = mapper(profile, learned={"favorite clinical domain": Row()}).map_fields([f])
    assert m.value == "Oncology" and m.source == "learned"
    (m2,) = mapper(profile, overrides={"q9": "Cardiology"}).map_fields([f])
    assert m2.value == "Cardiology" and m2.source == "human" and m2.confidence == 1.0


def test_llm_fallback_validated_and_capped(profile, fake_llm_factory):
    fields = [F("q_team", "Preferred team size", "select", ["1-5", "6-20", "20+"]),
              F("q_x", "Shoe size"), F("q_ghost", "Clinical specialty interest")]
    reply = {"mappings": [
        {"field_id": "q_team", "value": "6-20", "confidence": 0.99, "source": "inferred"},
        {"field_id": "q_x", "value": None, "confidence": 0.0, "source": "unknown"},
        {"field_id": "hallucinated", "value": "x", "confidence": 1, "source": "profile"},
        {"field_id": "q_ghost", "value": "Not an option", "confidence": 0.7, "source": "inferred"},
    ]}
    llm = fake_llm_factory({"field_map": json.dumps(reply)})
    ms = {m.field_id: m for m in mapper(profile, llm=llm).map_fields(fields)}
    assert ms["q_team"].value == "6-20" and ms["q_team"].confidence == 0.8 and ms["q_team"].source == "llm"
    assert ms["q_x"].value is None
    assert "hallucinated" not in ms
    prompt = llm.calls[0]["messages"][1]["content"]
    assert profile.personal.email not in prompt


def test_sensitive_questions_skip_llm(profile, fake_llm_factory):
    llm = fake_llm_factory({})  # would raise if called
    f = F("crim", "Have you ever been convicted of a felony?", "select", ["Yes", "No"])
    (m,) = mapper(profile, llm=llm).map_fields([f])
    assert m.value is None and "sensitive" in (m.reason or "")


def test_cover_letter_detection_and_text_field(profile):
    fields = [F("cl", "Cover Letter", "textarea")]
    assert needs_cover_letter(fields)
    (m,) = mapper(profile).map_fields(fields)
    assert m.value.startswith("Dear Team")


def test_button_classification_text_beats_automation_id():
    nxt_ids = {"bottom-navigation-next-button"}
    b = Button(text="Submit", jb_id="1", selector="x", automation_id="bottom-navigation-next-button")
    assert classify_button(b, set(), nxt_ids) == "submit"
    b2 = Button(text="Save and Continue", jb_id="2", selector="y", automation_id="bottom-navigation-next-button")
    assert classify_button(b2, set(), nxt_ids) == "next"
    assert classify_button(Button(text="Submit Application", jb_id="3", selector="z")) == "submit"


def test_detect_adapter():
    assert detect_adapter("https://boards.greenhouse.io/acme/jobs/1").name == "greenhouse"
    assert detect_adapter("https://acme.wd5.myworkdayjobs.com/External/job/x").name == "workday"
    assert detect_adapter("https://jobs.lever.co/acme/1/apply").name == "lever"
    assert detect_adapter("https://example.com/careers/apply").name == "generic"
