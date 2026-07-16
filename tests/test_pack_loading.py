import pytest

from localpulse.context.models import OfferingType
from localpulse.packs.base import PackLoadError, VerticalPack, load_pack


def test_bakery_pack_loads():
    pack = load_pack("bakery")
    assert isinstance(pack, VerticalPack)
    assert pack.ref == "bakery"
    assert pack.family == 1


def test_bakery_pack_exports_all_contract_pieces():
    pack = load_pack("bakery")
    assert pack.templates, "pack must ship content templates"
    assert pack.onboarding_questions, "pack must ship an onboarding question set"
    assert OfferingType.PRODUCT in pack.offering_schema.allowed_types
    assert pack.calendar_weights
    assert pack.playbook.cadence, "pack must define its own cadence"
    assert pack.guardrails.banned_terms


def test_bakery_calendar_is_maharashtra_weighted():
    pack = load_pack("bakery")
    assert pack.calendar_weights["ganesh chaturthi"] >= 1.5
    assert pack.calendar_weights["diwali"] >= 1.5


def test_unknown_pack_fails_loudly():
    with pytest.raises(PackLoadError):
        load_pack("florist")


def test_malicious_pack_ref_rejected():
    for ref in ["../etc", "Bakery", "bakery; import os", ""]:
        with pytest.raises(PackLoadError):
            load_pack(ref)
