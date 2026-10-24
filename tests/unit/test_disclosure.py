import pytest

from context_memory.policy.disclosure import detect, gate, highest_tier

ADDRESS = "123 Elm Street, Apt 4"


def test_address_withheld_from_cloud_model():
    r = gate(f"Pickup at {ADDRESS} on Saturday.", "cloud_model")
    assert r.decision == "transform"
    assert "Elm" not in r.text and "[WITHHELD:location-precise:street_address]" in r.text
    assert "Saturday" in r.text  # task utility preserved


def test_owner_sees_address():
    r = gate(f"Pickup at {ADDRESS}.", "owner")
    assert r.decision == "allow" and ADDRESS in r.text


@pytest.mark.parametrize("text,kind", [
    ("mail me at someone.person@example.org", "email"),
    ("call (902) 555-0142 after six", "phone"),
    ("call +44 20 7946 0958", "phone"),
    ("postal code B3H 4R2", "postal_code_ca"),
    ("card 4111 1111 1111 1111", "payment_card"),
])
def test_contact_location_financial_detected(text, kind):
    assert kind in {a.kind for a in detect(text)}
    assert "WITHHELD" in gate(text, "cloud_model").text


@pytest.mark.parametrize("text", [
    "SST anomaly at lat 44.6476, lon -63.5728 over 1993-2025",
    "Experiment 42 improved SSIM by 12% on 2026-10-18",
    "array shape (1, 720, 1440), chunk 4200000000000 bytes",
    "version 2.14.1+cu126 on driver 580.88",
    "see issue #1234 and PR 5678",
])
def test_research_text_untouched(text):
    assert gate(text, "cloud_model").decision == "allow"


def test_non_luhn_digit_runs_not_flagged_as_cards():
    assert "payment_card" not in {a.kind for a in detect("timestamp 1234567890123456")}


def test_public_blocks_research_private_chunks():
    assert gate("unpublished result", "public", "research-private").decision == "block"
    assert gate("unpublished result", "cloud_model", "research-private").decision == "allow"


def test_highest_tier_picks_most_sensitive():
    attrs = detect(f"x@y.org lives at {ADDRESS}")
    assert highest_tier(attrs, "research-private") == "location-precise"
    assert highest_tier([], "research-private") == "research-private"


def test_violation_report_has_no_values():
    r = gate("write to a.b@example.com", "cloud_model")
    assert "example.com" not in str(r.violations)


def test_unknown_recipient_rejected():
    with pytest.raises(ValueError):
        gate("x", "anyone")
