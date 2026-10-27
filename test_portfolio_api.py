"""The merge and update endpoints, exercised through the real FastAPI app.

/merge is stateless and needs no database, so it is fully hermetic here. The update
endpoint depends on storage, so these tests pin its contract when storage is OFF; the
ownership property of the SQL itself is tested live in test_storage.py.
"""
import pytest
from fastapi.testclient import TestClient

import api

client = TestClient(api.app)

HEAD = "Date,Symbol,Stock name,Type,Quantity,Price,Order status\n"
A = HEAD + "2026-01-05,INFY,Infosys,BUY,10,1500,Executed\n"
B = (HEAD + "2026-01-05,INFY,Infosys,BUY,10,1500,Executed\n"
     "2026-03-02,TCS,TCS,BUY,2,3800,Executed\n")


def test_merge_adds_only_what_is_new():
    r = client.post("/merge", json={"existing_csv": A, "incoming_csv": B})
    assert r.status_code == 200
    body = r.json()
    assert body["report"]["transactions_added"] == 1
    assert body["report"]["transactions_duplicate"] == 1
    assert "1 new transaction(s) added" in body["summary"]
    assert "TCS" in body["csv_text"]


def test_merge_with_nothing_existing_just_normalises_the_upload():
    r = client.post("/merge", json={"existing_csv": "", "incoming_csv": A})
    assert r.status_code == 200 and r.json()["report"]["transactions_added"] == 1


def test_merging_the_same_file_again_says_nothing_changed():
    first = client.post("/merge", json={"existing_csv": "", "incoming_csv": A}).json()
    again = client.post("/merge", json={"existing_csv": first["csv_text"],
                                        "incoming_csv": A}).json()
    assert again["report"]["changed"] is False
    assert again["summary"].startswith("Nothing new")


def test_merge_refuses_a_file_that_is_not_a_portfolio_with_a_reason():
    r = client.post("/merge", json={"existing_csv": A, "incoming_csv": "a,b\n1,2\n"})
    assert r.status_code == 400
    assert "does not look like" in r.json()["error"]


def test_merge_refuses_an_empty_upload():
    r = client.post("/merge", json={"existing_csv": A, "incoming_csv": "  "})
    assert r.status_code == 400


def test_the_merged_result_is_accepted_by_the_analysis_validator():
    """A merge must never hand back something the engine then cannot read."""
    merged = client.post("/merge", json={"existing_csv": A, "incoming_csv": B}).json()
    r = client.post("/portfolios", json={"name": "x", "csv_text": merged["csv_text"]},
                    headers={"X-Owner-Key": "k"})
    assert r.status_code in (200, 503)      # 503 only because saving is off in CI


def test_update_reports_saving_off_rather_than_pretending(monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    r = client.put("/portfolios/abc", json={"name": "n"}, headers={"X-Owner-Key": "k"})
    assert r.status_code == 503


def test_update_validates_the_new_contents_before_touching_storage(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://u:p@127.0.0.1:1/x")
    r = client.put("/portfolios/abc", json={"csv_text": "not,a,portfolio\n1,2,3\n"},
                   headers={"X-Owner-Key": "k"})
    assert r.status_code == 400


def test_update_with_a_wrong_key_or_dead_database_is_a_404_not_a_crash(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://u:p@127.0.0.1:1/x")
    monkeypatch.setattr("storage.CONNECT_TIMEOUT", 1)
    r = client.put("/portfolios/abc", json={"name": "n"}, headers={"X-Owner-Key": "k"})
    assert r.status_code == 404


# --- Excel -----------------------------------------------------------------------------------

def _xlsx(rows, title="Orders"):
    import io
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.title = title
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


XL_ROWS = [["Date", "Symbol", "Stock name", "Type", "Quantity", "Price", "Order status"],
           ["2026-01-05", "INFY", "Infosys", "BUY", 10, 1500, "Executed"]]


def test_convert_turns_a_workbook_into_text_the_other_endpoints_accept():
    r = client.post("/convert", files={"file": ("orders.xlsx", _xlsx(XL_ROWS),
                                                "application/octet-stream")})
    assert r.status_code == 200
    body = r.json()
    assert "Infosys" in body["csv_text"] and "“Orders” (1 rows)" in body["summary"]
    # ...and the converted text merges like any CSV would.
    m = client.post("/merge", json={"existing_csv": "", "incoming_csv": body["csv_text"]})
    assert m.status_code == 200 and m.json()["report"]["transactions_added"] == 1


def test_convert_refuses_a_non_workbook_with_a_reason():
    r = client.post("/convert", files={"file": ("orders.xlsx", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 400 and "not an .xlsx" in r.json()["error"]


def test_convert_refuses_a_workbook_with_no_portfolio_in_it():
    r = client.post("/convert", files={"file": ("n.xlsx", _xlsx([["hello"], ["world"]], "Notes"),
                                                "application/octet-stream")})
    assert r.status_code == 400 and "Sheets seen" in r.json()["error"]


def test_convert_never_reads_more_than_the_limit(monkeypatch):
    import excel_import
    monkeypatch.setattr(excel_import, "MAX_BYTES", 100)
    r = client.post("/convert", files={"file": ("big.xlsx", b"PK" + b"x" * 5000,
                                               "application/octet-stream")})
    assert r.status_code == 400 and "limit" in r.json()["error"]
