"""Phase 2A planning rules, pure functions only (no database)."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from rpt_agent.services.keap_desired import (
    compute_desired,
    desired_hash,
    mask_desired,
    split_name,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
START = NOW - timedelta(days=1)
PENDING, GAVE_UP = "144", "186"
MAPPING = [
    {"stride_appointment_type_id": 2, "booked_tag_id": "180", "completed_tag_id": "172",
     "date_field_id": "6", "time_field_id": "8", "clinician_field_id": "10"},
    {"stride_appointment_type_id": 1, "booked_tag_id": "184", "completed_tag_id": "176",
     "date_field_id": "6", "time_field_id": "8", "clinician_field_id": "10"},
]
SETTINGS = {
    "keap_pending_tag_id": PENDING, "keap_gave_up_tag_id": GAVE_UP, "keap_contact_type": "Lead",
    "keap_sync_start_at": START, "keap_include_existing_patients": True,
    "keap_remove_pending_when_booked": True, "keap_remove_booked_on_cancel": True,
    "stride_location_timezone": "America/New_York",
}


def lead(**kw):
    base = {"id": "11111111-2222-3333-4444-555555555555", "full_name": "Asha Rao",
            "first_name": "Asha", "last_name": "Rao", "email": "asha@example.test",
            "phone_e164": "+15555550101", "date_of_birth": None, "status": "in_progress"}
    return {**base, **kw}


def patient(**kw):
    base = {"first_name": "Asha", "last_name": "Rao", "date_of_birth": None,
            "personal_email": None, "work_email": None, "preferred_email": None,
            "mobile_phone": None, "home_phone": None, "work_phone": None, "preferred_phone": None,
            "address_1": None, "address_2": None, "city": None, "state": None, "zip": None,
            "deleted_at": None}
    return {**base, **kw}


def appt(type_id=2, days=2, status="Unmarked", clinician="Jane Clinician", deleted=False,
         local=None, **kw):
    start = NOW + timedelta(days=days)
    return {"appointment_type_id": type_id, "status": status, "start_utc": start,
            "starts_at_local": local or (start - timedelta(hours=4)).replace(tzinfo=None),
            "clinician_id": 3, "clinician_name": clinician,
            "deleted_at": NOW if deleted else None, **kw}


def run(lead_=None, patient_=None, appts=(), settings=None, mapping=None, users=None,
        synced_before=False):
    return compute_desired(
        lead=lead_, patient=patient_, patient_id=50 if patient_ else None,
        appointments=list(appts), users=users or {}, settings={**SETTINGS, **(settings or {})},
        mapping=MAPPING if mapping is None else mapping, synced_before=synced_before, now=NOW,
    )


def test_sheet_lead_without_appointment_is_pending():
    built = run(lead())
    d = built.desired
    assert d["tags"] == [PENDING]
    assert d["contact"]["email"] == "asha@example.test"
    assert d["contact"]["contact_type"] == "Lead"
    assert d["fields"] == {"6": {"kind": "date", "value": None},
                           "8": {"kind": "time", "value": None},
                           "10": {"kind": "text", "value": None}}
    assert d["owned_fields"] == ["10", "6", "8"]
    assert set(d["owned_tags"]) == {PENDING, GAVE_UP, "180", "172", "184", "176"}


def test_full_name_only_is_split():
    built = run(lead(first_name=None, last_name=None, full_name="Asha Rao"))
    assert built.desired["contact"]["given_name"] == "Asha"
    assert built.desired["contact"]["family_name"] == "Rao"
    assert split_name("Asha Kumar Rao") == ("Asha", "Kumar Rao")
    assert split_name("devyansh") == ("devyansh", "")


def test_booking_replaces_pending_and_sets_fields():
    d = run(lead(), appts=[appt(2, days=1, local=datetime(2026, 10, 15, 14, 30))]).desired  # noqa: DTZ001
    assert d["tags"] == ["180"]
    assert d["fields"]["6"]["value"] == "2026-10-15"
    assert d["fields"]["8"]["value"] == "2:30 PM"
    assert d["fields"]["10"]["value"] == "Jane Clinician"


def test_pending_can_stay_when_the_setting_says_so():
    d = run(lead(), appts=[appt(2)], settings={"keap_remove_pending_when_booked": False}).desired
    assert d["tags"] == sorted([PENDING, "180"])


def test_voice_booked_lead_without_a_stride_appointment_has_no_pending_and_no_booked():
    assert run(lead(status="booked")).desired["tags"] == []


def test_walk_in_patient_never_gets_pending():
    d = run(None, patient(preferred_email="a@example.test"), appts=[appt(1)]).desired
    assert d["tags"] == ["184"]


def test_checked_in_adds_completed_and_keeps_booked():
    d = run(lead(), appts=[appt(2, days=-1, status="Checked In")]).desired
    assert d["tags"] == sorted(["180", "172"])
    assert run(lead(), appts=[appt(2, days=-1, status="C")]).desired["tags"] == ["172", "180"]


def test_no_show_keeps_booked_without_completed():
    assert run(lead(), appts=[appt(2, days=-1, status="No Show")]).desired["tags"] == ["180"]


def test_cancelled_appointment_removes_booked_and_returns_to_pending():
    d = run(lead(status="needs_attention"), appts=[appt(2, status="Cancel")]).desired
    assert d["tags"] == [PENDING]
    assert all(f["value"] is None for f in d["fields"].values())


def test_cancel_keeps_booked_when_the_setting_is_off():
    d = run(lead(), appts=[appt(2, status="Late Cancel")],
            settings={"keap_remove_booked_on_cancel": False}).desired
    assert "180" in d["tags"]
    assert d["fields"]["6"]["value"] is None  # a cancelled visit never fills the date


PELVIC = {"stride_appointment_type_id": 5, "booked_tag_id": "190", "male_booked_tag_id": "192",
          "completed_tag_id": "194", "date_field_id": "6", "time_field_id": "8",
          "clinician_field_id": "10"}


def test_pelvic_booking_tag_follows_the_patients_sex():
    mapping = [*MAPPING, PELVIC]
    female = run(lead(), patient(gender="F"), appts=[appt(5)], mapping=mapping).desired
    male = run(lead(), patient(gender="Male"), appts=[appt(5)], mapping=mapping).desired
    done = run(lead(), patient(gender="M"), appts=[appt(5, days=-1, status="Checked In")],
               mapping=mapping).desired
    assert female["tags"] == ["190"]
    assert male["tags"] == ["192"]
    assert done["tags"] == ["192", "194"]  # one Completed tag for both
    assert {"190", "192", "194"} <= set(male["owned_tags"])  # a cancel removes either one
    # Types without a male tag stay one tag for everyone.
    assert run(lead(), patient(gender="M"), appts=[appt(2)]).desired["tags"] == ["180"]


# The sandbox mapping (supabase/dev/keap_sandbox_mapping.sql), Keap admin's tag list 2026-10-20.
def _row(type_id, booked, done=None, male=None):
    return {"stride_appointment_type_id": type_id, "booked_tag_id": booked,
            "male_booked_tag_id": male, "completed_tag_id": done,
            "date_field_id": "6", "time_field_id": "8", "clinician_field_id": "10"}


# Initial Evaluation (2), Progress Note / POC (3) and Re-Evaluation (4) are not handled for now
# (client, 2026-10-20).
SANDBOX = [_row(1, "184", "176"), _row(5, "166", "164"), _row(6, "162", "158", male="160")]


@pytest.mark.parametrize(("type_id", "gender", "booked", "completed"), [
    (1, "F", "184", "176"),   # PT Appt Booked / PT Appt Completed
    (5, "M", "166", "164"),   # PT Premium Appt Booked / Completed (same tag for both sexes)
    (6, "F", "162", "158"),   # Pelvic Appt Booked - Female / Pelvic Appt Completed
    (6, "M", "160", "158"),   # Pelvic Appt Booked - Male / Pelvic Appt Completed
])
def test_every_admin_tag_on_book_complete_and_cancel(type_id, gender, booked, completed):
    def tags(status, days=2):
        return run(lead(), patient(gender=gender), appts=[appt(type_id, days=days, status=status)],
                   mapping=SANDBOX).desired["tags"]

    assert run(lead(), patient(gender=gender), mapping=SANDBOX).desired["tags"] == [PENDING]
    assert tags("Unmarked") == [booked]
    assert tags("Checked In", days=-1) == sorted([booked, completed])
    assert tags("Cancel") == [PENDING]


@pytest.mark.parametrize("type_id", [2, 3, 4])
def test_evaluation_re_evaluation_and_poc_get_no_keap_tag(type_id):
    for status, days in (("Unmarked", 2), ("Checked In", -1)):
        d = run(lead(), patient(gender="F"), appts=[appt(type_id, days=days, status=status)],
                mapping=SANDBOX).desired
        assert d["tags"] == [PENDING]  # ignored entirely: no Booked, no Completed
        assert not {"180", "172", "178", "174", "156"} & set(d["owned_tags"])


def test_unknown_gender_on_a_pelvic_booking_gets_the_default_tag():
    d = run(lead(), patient(gender=None), appts=[appt(6)], mapping=SANDBOX).desired
    assert d["tags"] == ["162"]


def test_deleted_appointment_counts_as_cancelled():
    assert run(lead(), appts=[appt(2, deleted=True)]).desired["tags"] == [PENDING]


def test_rescheduled_appointment_moves_the_date():
    first = run(lead(), appts=[appt(2, days=1)]).desired["fields"]["6"]["value"]
    moved = run(lead(), appts=[appt(2, days=5)]).desired["fields"]["6"]["value"]
    assert first != moved


def test_nearest_upcoming_appointment_wins_across_types():
    near = appt(1, days=1, clinician="Near")
    far = appt(2, days=9, clinician="Far")
    d = run(lead(), appts=[far, near]).desired
    assert d["fields"]["10"]["value"] == "Near"
    assert d["tags"] == ["180", "184"]  # a second type adds its own Booked tag


def test_only_past_appointment_shows_its_date():
    past = appt(2, days=-3, status="Checked In", local=datetime(2026, 10, 11, 9, 0))  # noqa: DTZ001
    d = run(lead(), appts=[past], settings={"keap_sync_start_at": NOW - timedelta(days=9)}).desired
    assert d["fields"]["6"]["value"] == "2026-10-11"
    assert d["fields"]["8"]["value"] == "9:00 AM"


def test_appointment_before_go_live_is_ignored():
    old = appt(2, days=-10, status="Checked In")
    assert run(lead(), appts=[old]).desired["tags"] == [PENDING]


def test_unmapped_appointment_type_is_ignored():
    assert run(lead(), appts=[appt(99)]).desired["tags"] == [PENDING]


def test_stride_email_wins_but_the_leads_phone_wins():
    d = run(lead(), patient(preferred_email="stride@example.test",
                            mobile_phone="+15555550199")).desired["contact"]
    assert d["email"] == "stride@example.test"
    assert d["phone"] == "+15555550101"


def test_address_only_when_there_is_no_email_or_phone():
    p = patient(address_1="1 Test St", city="Miami", state="fl", zip="33101")
    d = run(None, p).desired["contact"]
    assert d["email"] is None and d["phone"] is None
    assert d["address"]["region_code"] == "US-FL" and d["address"]["country_code"] == "USA"


def test_no_contact_details_is_skipped():
    built = run(lead(email=None, phone_e164=None))
    assert built.desired is None and built.skip_reason == "no contact details"


def test_sync_not_started_is_skipped():
    built = run(lead(), settings={"keap_sync_start_at": None})
    assert built.skip_reason == "keap sync not started"


def test_deleted_patient_is_frozen_after_sync_and_skipped_before():
    gone = patient(deleted_at=NOW, preferred_email="a@example.test")
    assert run(None, gone, synced_before=True).freeze is True
    assert run(None, gone, synced_before=False).skip_reason == "deleted in Stride before sync"


def test_deleted_patient_of_a_lead_falls_back_to_the_lead():
    gone = patient(deleted_at=NOW, preferred_email="stride@example.test")
    assert run(lead(), gone).desired["contact"]["email"] == "asha@example.test"


def test_ended_cadence_swaps_pending_for_the_gave_up_tag():
    assert run(lead(status="closed_no_response")).desired["tags"] == [GAVE_UP]
    assert run(lead(status="declined")).desired["tags"] == [GAVE_UP]


def test_do_not_contact_or_wrong_number_never_reaches_keap():
    # A Pending tag in Keap can start marketing emails/texts to someone who asked us to stop.
    for status in ("do_not_contact", "invalid_phone"):
        built = run(lead(status=status))
        assert built.desired is None and "kept out of Keap" in built.skip_reason


def test_do_not_contact_already_in_keap_is_left_exactly_as_it_is():
    built = run(lead(status="do_not_contact"), synced_before=True)
    assert built.freeze and built.desired is None


def test_ended_lead_that_booked_in_stride_is_booked_not_gave_up():
    assert run(lead(status="closed_no_response"), appts=[appt(2)]).desired["tags"] == ["180"]


def test_missing_gave_up_tag_keeps_pending():
    assert run(lead(status="closed_no_response"),
               settings={"keap_gave_up_tag_id": None}).desired["tags"] == [PENDING]


def test_daylight_saving_time_is_the_clinic_wall_time_in_march_and_november():
    for wall in (datetime(2026, 3, 10, 14, 30), datetime(2026, 11, 10, 14, 30)):  # noqa: DTZ001
        a = appt(2, days=1, local=wall)
        assert run(lead(), appts=[a]).desired["fields"]["8"]["value"] == "2:30 PM"


def test_time_falls_back_to_utc_conversion_when_no_wall_time():
    a = appt(2, days=1, starts_at_local=None)
    a["start_utc"] = datetime(2026, 10, 15, 18, 30, tzinfo=UTC)  # 2:30 PM EDT
    a["starts_at_local"] = None
    assert run(lead(), appts=[a]).desired["fields"]["8"]["value"] == "2:30 PM"


def test_clinician_name_comes_from_stride_users_when_missing():
    a = appt(2, clinician=None, clinician_id=3)
    d = run(lead(), appts=[a], users={3: "Pat Therapist"}).desired
    assert d["fields"]["10"]["value"] == "Pat Therapist"


def test_hash_is_stable_and_sensitive():
    one = run(lead()).desired
    two = run(lead()).desired
    assert desired_hash(one) == desired_hash(two)
    assert desired_hash(one) != desired_hash(run(lead(), appts=[appt(2)]).desired)


def test_mask_hides_personal_data_but_keeps_ids():
    d = run(lead(date_of_birth=datetime(1990, 2, 3).date()), appts=[appt(2)]).desired  # noqa: DTZ001
    text = json.dumps(mask_desired(d))
    for secret in ("Asha", "Rao", "asha@", "5555550101", "1990-02-03", "Jane"):
        assert secret not in text
    assert '"180"' in text and '"6"' in text


def test_names_are_clipped_to_the_length_keap_stores():
    d = run(lead(first_name="G" * 100, last_name="F" * 60)).desired
    assert d["contact"]["given_name"] == "G" * 40 and d["contact"]["family_name"] == "F" * 40
    assert d["match"]["given_name"] == "G" * 40 and d["match"]["family_name"] == "F" * 40
