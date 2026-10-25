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


# Formats from an external probe of the public gate (2026-10-25), all synthetic.
MUST_REDACT = [
    ("Call +1 902 555 0134.", "phone"),
    ("Call +19025550134 tomorrow", "phone"),
    ("Reach me at 902-555-0134.", "phone"),          # trailing period used to defeat the pattern
    ("Office: (902) 555-0134", "phone"),
    ("Site at 44.6366 N, -63.5917 W", "coordinates"),
    ("Site at 44.6366° N 63.5917° W", "coordinates"),
    ("GPS 44.6366, -63.5917 for the mooring", "coordinates"),
    ("Mooring at 44°38'12\"N 63°35'30\"W", "coordinates"),
    ("write to parth [at] dal [dot] ca please", "email"),
    ("write to parth (at) dal (dot) ca", "email"),
    ("IBAN GB82 WEST 1234 5698 7654 32 for transfers", "iban"),
    ("IBAN GB82WEST12345698765432 for transfers", "iban"),
    ("SIN 046 454 286 on file", "government_id"),
    ("SSN 123-45-6789", "government_id"),
    ("born on 1994-03-17 in Halifax", "date_of_birth"),
    ("DOB: 17/03/1994", "date_of_birth"),
    ("date of birth March 17, 1994", "date_of_birth"),
]
MUST_PASS = [
    "recall@5 0.750 vs 0.833, MRR 0.672",
    "AnyConnect 4.7.00136 and openconnect 9.12",
    "Run started 2026-10-24T16:44:40Z, took 152 ms",
    "Qwen3-Embedding-0.6B at revision 97b0c614",
    "lr 0.0003, 0.0001 decay over 1000 steps",
    "the hub listens on 127.0.0.1:8765",
    "We met at noon to discuss the plan",
]


@pytest.mark.parametrize("text,kind", MUST_REDACT)
def test_probe_formats_are_withheld_for_public_and_cloud(text, kind):
    for recipient in ("public", "cloud_model", "mcp_server"):
        r = gate(text, recipient, chunk_tier="public")
        assert any(v["kind"] == kind for v in r.violations), (recipient, text, r.text)


@pytest.mark.parametrize("text", MUST_PASS)
def test_ordinary_technical_text_is_untouched(text):
    assert gate(text, "public", chunk_tier="public").text == text


def test_checksums_reject_lookalikes():
    assert gate("IBAN GB00 WEST 1234 5698 7654 32", "public", chunk_tier="public").decision == "allow"
    assert gate("SIN 046 454 287", "public", chunk_tier="public").decision == "allow"
