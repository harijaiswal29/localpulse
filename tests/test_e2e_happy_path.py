"""P0 thin slice end-to-end: onboard -> content drafts -> WhatsApp approve/edit/
skip -> publish (semi-manual GBP queue) -> insights report. Publish is mocked;
no real external calls (spec §12.2)."""

from fastapi.testclient import TestClient

from localpulse.api.main import create_app
from localpulse.packs.base import load_pack
from tests.conftest import PILOT_ANSWERS, make_test_settings

OWNER = PILOT_ANSWERS["owner_whatsapp"]


def make_client() -> TestClient:
    return TestClient(create_app(make_test_settings()))


def onboard(client: TestClient) -> dict:
    response = client.post(
        "/clients/pilot-1/onboard", json={"pack_ref": "bakery", "answers": PILOT_ANSWERS}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_full_pilot_flow():
    client = make_client()

    # 1. Onboarding produces a valid Client Context
    context = onboard(client)
    assert context["client_id"] == "pilot-1"
    assert context["vertical_pack_ref"] == "bakery"
    assert len(context["offerings"]) == 3
    assert context["business"]["owner_whatsapp"] == OWNER

    # 2. Content Agent generates a week of drafts into the queue
    response = client.post("/clients/pilot-1/content/run", json={"week_start": "2026-07-20"})
    assert response.status_code == 200
    drafts = response.json()["drafts"]
    assert len(drafts) == 3
    assert all(d["state"] == "pending_approval" for d in drafts)

    # 3. Owner drives Approve / Edit / Skip entirely over WhatsApp
    listing = client.post("/webhooks/whatsapp", json={"from_number": OWNER, "text": "LIST"}).json()[
        "reply"
    ]
    assert drafts[0]["short_id"] in listing

    new_caption = "Our famous cake is back!"
    edit_reply = client.post(
        "/webhooks/whatsapp",
        json={"from_number": OWNER, "text": f"EDIT {drafts[0]['short_id']} {new_caption}"},
    ).json()["reply"]
    assert "Updated" in edit_reply

    approve_reply = client.post(
        "/webhooks/whatsapp",
        json={"from_number": OWNER, "text": f"APPROVE {drafts[0]['short_id']}"},
    ).json()["reply"]
    assert "published" in approve_reply.lower()

    skip_reply = client.post(
        "/webhooks/whatsapp",
        json={"from_number": OWNER, "text": f"SKIP {drafts[1]['short_id']}"},
    ).json()["reply"]
    assert "Skipped" in skip_reply

    # 4. Approved item is published (with the edited caption); skipped is discarded
    queue = client.get("/clients/pilot-1/queue").json()["items"]
    by_id = {item["short_id"]: item for item in queue}
    assert by_id[drafts[0]["short_id"]]["state"] == "published"
    assert by_id[drafts[0]["short_id"]]["caption"] == "Our famous cake is back!"
    assert by_id[drafts[1]["short_id"]]["state"] == "discarded"
    assert by_id[drafts[2]["short_id"]]["state"] == "pending_approval"

    # 5. Insights: collect metrics, then a plain-language monthly report
    assert client.post("/clients/pilot-1/insights/collect").status_code == 200
    report = client.get("/clients/pilot-1/report/2026/7").json()["report"]
    assert "Mane's Bakehouse" in report
    assert "month in review" in report


def test_unknown_whatsapp_number_is_rejected():
    client = make_client()
    onboard(client)
    reply = client.post(
        "/webhooks/whatsapp", json={"from_number": "+911111111111", "text": "LIST"}
    ).json()["reply"]
    assert "isn't linked" in reply


def test_double_approve_is_safe():
    client = make_client()
    onboard(client)
    drafts = client.post("/clients/pilot-1/content/run", json={"week_start": "2026-07-20"}).json()[
        "drafts"
    ]
    short_id = drafts[0]["short_id"]
    first = client.post(
        "/webhooks/whatsapp", json={"from_number": OWNER, "text": f"APPROVE {short_id}"}
    ).json()["reply"]
    assert "published" in first.lower()
    second = client.post(
        "/webhooks/whatsapp", json={"from_number": OWNER, "text": f"APPROVE {short_id}"}
    ).json()["reply"]
    assert "already published" in second.lower()


def test_incomplete_onboarding_answers_are_a_client_error():
    """A missing required answer is the caller's mistake, not a server fault: it
    comes back as 422 naming the fields, so a self-serve form can highlight them."""
    client = make_client()
    response = client.post(
        "/clients/pilot-1/onboard",
        json={"pack_ref": "salon", "answers": {"city": "Pune"}},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    expected = {q.id for q in load_pack("salon").onboarding_questions if q.required} - {"city"}
    assert set(detail["missing"]) == expected
    assert "salon_name" in detail["error"]


def test_unknown_pack_ref_is_a_client_error():
    client = make_client()
    response = client.post(
        "/clients/pilot-1/onboard", json={"pack_ref": "butcher", "answers": PILOT_ANSWERS}
    )
    assert response.status_code == 422, response.text
    assert "butcher" in response.json()["detail"]


def test_malformed_pack_ref_is_rejected_not_traversed():
    client = make_client()
    response = client.post(
        "/clients/pilot-1/onboard", json={"pack_ref": "../evil", "answers": PILOT_ANSWERS}
    )
    assert response.status_code == 422, response.text
    assert "invalid pack ref" in response.json()["detail"]
