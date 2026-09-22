"""API integration tests: creation, allocation correctness, rejection rules."""

import pytest
from fastapi.testclient import TestClient

from app.db import InvoiceStore
from app.main import app, get_store


@pytest.fixture()
def client(tmp_path):
    store = InvoiceStore(str(tmp_path / "test.db"))
    app.dependency_overrides[get_store] = lambda: store
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def valid_payload(**overrides):
    payload = {
        "invoice_number": "INV-2026-0001",
        "currency": "CNY",
        "lines": [
            {
                "description": "Consulting services",
                "quantity": "2",
                "unit_price": "100.00",
                "discount_rate": "0.10",
                "tax_regions": [
                    {"region_code": "CN-SH", "tax_rate": "0.13", "proportion": "0.6"},
                    {"region_code": "CN-BJ", "tax_rate": "0.13", "proportion": "0.4"},
                ],
            },
            {
                "description": "Hardware",
                "quantity": "1",
                "unit_price": "33.33",
                "discount_amount": "3.33",
                "tax_regions": [
                    {"region_code": "CN-SH", "tax_rate": "0.13", "proportion": "0.5"},
                    {"region_code": "US-CA", "tax_rate": "0.0725", "proportion": "0.5"},
                ],
            },
        ],
    }
    payload.update(overrides)
    return payload


class TestCreateInvoice:
    def test_happy_path_exact_amounts(self, client):
        resp = client.post("/api/v1/invoices", json=valid_payload())
        assert resp.status_code == 201, resp.text
        body = resp.json()

        # Line 1: gross 200.00, discount 20.00, net 180.00
        line1 = body["lines"][0]
        assert line1["gross_amount"] == 200.00
        assert line1["discount_amount"] == 20.00
        assert line1["net_amount"] == 180.00
        regions1 = {r["region_code"]: r for r in line1["regions"]}
        assert regions1["CN-SH"]["taxable_amount"] == 108.00
        assert regions1["CN-SH"]["tax_amount"] == 14.04
        assert regions1["CN-BJ"]["taxable_amount"] == 72.00
        assert regions1["CN-BJ"]["tax_amount"] == 9.36

        # Line 2: gross 33.33, discount 3.33, net 30.00
        line2 = body["lines"][1]
        assert line2["net_amount"] == 30.00
        regions2 = {r["region_code"]: r for r in line2["regions"]}
        assert regions2["CN-SH"]["tax_amount"] == 1.95
        # 15.00 * 0.0725 = 1.0875 -> region total rounds to 1.09, allocated to this line
        assert regions2["US-CA"]["tax_amount"] == 1.09

        totals = body["totals"]
        assert totals["total_gross"] == 233.33
        assert totals["total_discount"] == 23.33
        assert totals["total_net"] == 210.00
        assert totals["total_tax"] == 26.44
        assert totals["grand_total"] == 236.44
        region_totals = {r["region_code"]: r for r in totals["region_totals"]}
        assert region_totals["CN-SH"]["tax_amount"] == 15.99
        assert region_totals["CN-BJ"]["tax_amount"] == 9.36
        assert region_totals["US-CA"]["tax_amount"] == 1.09

        assert body["status"] == "validated"
        assert body["validation"]["all_ok"] is True
        assert all(c["ok"] for c in body["validation"]["checks"])

    def test_rounding_residual_distributed_to_lines(self, client):
        # Three lines of net 33.33/33.33/33.34 at 13%: exact taxes
        # 4.3329/4.3329/4.3342 -> region total 13.00; largest remainder
        # assigns the extra cent to the third line.
        lines = [
            {
                "description": f"item {i}",
                "quantity": "1",
                "unit_price": price,
                "tax_regions": [
                    {"region_code": "CN-SH", "tax_rate": "0.13", "proportion": "1"}
                ],
            }
            for i, price in enumerate(["33.33", "33.33", "33.34"])
        ]
        resp = client.post(
            "/api/v1/invoices",
            json={"invoice_number": "INV-RR-1", "lines": lines},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        taxes = [l["regions"][0]["tax_amount"] for l in body["lines"]]
        assert taxes == [4.33, 4.33, 4.34]
        assert body["totals"]["region_totals"][0]["tax_amount"] == 13.00
        assert body["totals"]["total_tax"] == 13.00

    def test_matching_expected_totals_accepted(self, client):
        payload = valid_payload(
            invoice_number="INV-EXP-1",
            expected_totals={"net": "210.00", "tax": "26.44", "grand_total": "236.44"},
        )
        resp = client.post("/api/v1/invoices", json=payload)
        assert resp.status_code == 201, resp.text

    def test_mismatched_expected_totals_rejected(self, client):
        payload = valid_payload(
            invoice_number="INV-EXP-2",
            expected_totals={"grand_total": "999.00"},
        )
        resp = client.post("/api/v1/invoices", json=payload)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "totals_mismatch"
        assert any("grand_total" in m for m in detail["mismatches"])

    def test_proportions_not_summing_to_one_rejected(self, client):
        payload = valid_payload(invoice_number="INV-BAD-PROP")
        payload["lines"][0]["tax_regions"] = [
            {"region_code": "CN-SH", "tax_rate": "0.13", "proportion": "0.5"},
            {"region_code": "CN-BJ", "tax_rate": "0.13", "proportion": "0.4"},
        ]
        resp = client.post("/api/v1/invoices", json=payload)
        assert resp.status_code == 422
        assert "proportions must sum to 1" in resp.text

    def test_duplicate_region_code_rejected(self, client):
        payload = valid_payload(invoice_number="INV-DUP-REGION")
        payload["lines"][0]["tax_regions"] = [
            {"region_code": "CN-SH", "tax_rate": "0.13", "proportion": "0.5"},
            {"region_code": "CN-SH", "tax_rate": "0.13", "proportion": "0.5"},
        ]
        resp = client.post("/api/v1/invoices", json=payload)
        assert resp.status_code == 422

    def test_discount_exceeding_gross_rejected(self, client):
        payload = valid_payload(invoice_number="INV-BAD-DISC")
        payload["lines"][0]["discount_amount"] = "500.00"
        resp = client.post("/api/v1/invoices", json=payload)
        assert resp.status_code == 422

    def test_empty_lines_rejected(self, client):
        resp = client.post(
            "/api/v1/invoices", json={"invoice_number": "INV-EMPTY", "lines": []}
        )
        assert resp.status_code == 422

    def test_duplicate_invoice_number_rejected(self, client):
        assert client.post("/api/v1/invoices", json=valid_payload()).status_code == 201
        resp = client.post("/api/v1/invoices", json=valid_payload())
        assert resp.status_code == 409


class TestBatchCreate:
    def test_happy_path_all_persisted(self, client):
        payload = {
            "invoices": [
                valid_payload(invoice_number="INV-B-1"),
                valid_payload(invoice_number="INV-B-2"),
            ]
        }
        resp = client.post("/api/v1/invoices/batch", json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert [item["index"] for item in body["invoices"]] == [0, 1]
        assert [item["invoice_number"] for item in body["invoices"]] == [
            "INV-B-1",
            "INV-B-2",
        ]
        assert all(item["invoice_id"] for item in body["invoices"])
        assert all(item["grand_total"] == 236.44 for item in body["invoices"])

        # Both invoices are retrievable afterwards.
        for item in body["invoices"]:
            got = client.get(f"/api/v1/invoices/{item['invoice_id']}")
            assert got.status_code == 200
            assert got.json()["invoice_number"] == item["invoice_number"]

    def test_totals_mismatch_rolls_back_whole_batch(self, client):
        payload = {
            "invoices": [
                valid_payload(invoice_number="INV-B-OK"),
                valid_payload(
                    invoice_number="INV-B-BAD",
                    expected_totals={"grand_total": "999.00"},
                ),
            ]
        }
        resp = client.post("/api/v1/invoices/batch", json=payload)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error"] == "batch_rejected"
        assert detail["errors"][0]["index"] == 1
        assert detail["errors"][0]["invoice_number"] == "INV-B-BAD"
        assert detail["errors"][0]["error"] == "totals_mismatch"
        assert any(
            "grand_total" in m for m in detail["errors"][0]["mismatches"]
        )
        # Nothing was written, not even the valid first invoice.
        assert client.get("/api/v1/invoices").json() == []

    def test_duplicate_number_within_batch_rejected(self, client):
        payload = {
            "invoices": [
                valid_payload(invoice_number="INV-B-DUP"),
                valid_payload(invoice_number="INV-B-OTHER"),
                valid_payload(invoice_number="INV-B-DUP"),
            ]
        }
        resp = client.post("/api/v1/invoices/batch", json=payload)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["error"] == "batch_rejected"
        assert detail["errors"] == [
            {
                "index": 2,
                "invoice_number": "INV-B-DUP",
                "error": "duplicate_invoice_number",
                "message": "duplicates invoice at index 0 in this batch",
            }
        ]
        assert client.get("/api/v1/invoices").json() == []

    def test_duplicate_number_against_database_rejected(self, client):
        assert (
            client.post("/api/v1/invoices", json=valid_payload()).status_code
            == 201
        )
        payload = {
            "invoices": [
                valid_payload(invoice_number="INV-B-NEW"),
                valid_payload(),  # INV-2026-0001 already stored
            ]
        }
        resp = client.post("/api/v1/invoices/batch", json=payload)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["errors"][0]["index"] == 1
        assert detail["errors"][0]["invoice_number"] == "INV-2026-0001"
        # The new invoice from the failed batch was rolled back.
        numbers = {
            item["invoice_number"] for item in client.get("/api/v1/invoices").json()
        }
        assert numbers == {"INV-2026-0001"}

    def test_empty_batch_rejected(self, client):
        resp = client.post("/api/v1/invoices/batch", json={"invoices": []})
        assert resp.status_code == 422

    def test_invalid_invoice_in_batch_rejected(self, client):
        bad = valid_payload(invoice_number="INV-B-BADPROP")
        bad["lines"][0]["tax_regions"] = [
            {"region_code": "CN-SH", "tax_rate": "0.13", "proportion": "0.5"},
        ]
        resp = client.post(
            "/api/v1/invoices/batch",
            json={"invoices": [valid_payload(invoice_number="INV-B-OK2"), bad]},
        )
        assert resp.status_code == 422
        assert client.get("/api/v1/invoices").json() == []


class TestQuery:
    def test_get_invoice_returns_stored_result(self, client):
        created = client.post("/api/v1/invoices", json=valid_payload()).json()
        resp = client.get(f"/api/v1/invoices/{created['invoice_id']}")
        assert resp.status_code == 200
        assert resp.json() == created

    def test_get_missing_invoice_404(self, client):
        resp = client.get("/api/v1/invoices/does-not-exist")
        assert resp.status_code == 404

    def test_list_invoices(self, client):
        client.post("/api/v1/invoices", json=valid_payload())
        client.post(
            "/api/v1/invoices", json=valid_payload(invoice_number="INV-2026-0002")
        )
        resp = client.get("/api/v1/invoices")
        assert resp.status_code == 200
        numbers = {item["invoice_number"] for item in resp.json()}
        assert numbers == {"INV-2026-0001", "INV-2026-0002"}
        assert resp.json()[0]["grand_total"] == 236.44

    def test_health(self, client):
        assert client.get("/health").json() == {"status": "ok"}
