#!/usr/bin/env python3
"""
LVVI Fund Refresh
------------------
Keeps Fund_Remittance_Payments (Dagupan doc) in sync with Tax_Remittances (LVVI
Taxes doc) and Contribution_Remittances (LVVI Contributions doc).

Logic:
  - Only rows tagged source == "Live entry" represent remittances encoded live,
    going forward, that should draw down the Fund. These get a matching positive
    row inserted into Fund_Remittance_Payments -- an outflow log that
    Fund_Accountability's payments_out formula sums by `fund` (Choice values
    "Tax" / "Contributions (SSS/PHIC/HDMF)"), so amounts here are positive
    (payments_out is subtracted from opening + funding_in in net_balance).
  - All other pending rows (historical batch-imported / migrated data that predates
    the Fund ledger) are simply flagged posted_to_fund = True with NO payment row,
    so they are never reprocessed and never distort the running balance.
  - Idempotent: only rows where posted_to_fund is not yet true are touched, so this
    is safe to run repeatedly (e.g. on every scheduled run or firm-wide refresh).

Requires env var GRIST_API_KEY (a Grist personal API key, injected as a GitHub
Actions secret -- never written to disk or logged).
"""
import json
import os
import sys
import urllib.parse
import urllib.request

GRIST_KEY = os.environ["GRIST_API_KEY"]
BASE = "https://docs.getgrist.com/api/docs"

TAX_DOC = "144xnp6dgaMypPtF6vyRuL"
CONTRIB_DOC = "uAi5sxhezG9CgfF2cjtGRy"
DAGUPAN_DOC = "xcJuTqTrGePQUeBxAUmtVb"

CHUNK = 100


def _req(method, url, body=None, params=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {GRIST_KEY}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        print(f"HTTP {e.code} on {method} {url}: {detail}", file=sys.stderr)
        raise


def list_all(doc_id, table_id):
    # Grist's REST API supports `limit` but NOT `offset` on GET /records --
    # there is no offset-based pagination. Use one call with a limit comfortably
    # above any realistic table size instead of looping (a loop here would never
    # terminate, since every "page" would just re-return the same first rows).
    url = f"{BASE}/{doc_id}/tables/{table_id}/records"
    data = _req("GET", url, params={"limit": 20000})
    return data["records"]


def add_records(doc_id, table_id, field_dicts):
    if not field_dicts:
        return
    url = f"{BASE}/{doc_id}/tables/{table_id}/records"
    for i in range(0, len(field_dicts), CHUNK):
        chunk = field_dicts[i:i + CHUNK]
        _req("POST", url, body={"records": [{"fields": f} for f in chunk]})


def update_records(doc_id, table_id, id_field_pairs):
    if not id_field_pairs:
        return
    url = f"{BASE}/{doc_id}/tables/{table_id}/records"
    for i in range(0, len(id_field_pairs), CHUNK):
        chunk = id_field_pairs[i:i + CHUNK]
        _req("PATCH", url, body={"records": [{"id": rid, "fields": f} for rid, f in chunk]})


def process_table(doc_id, table_id, fund_label, date_field, desc_fn):
    rows = list_all(doc_id, table_id)
    pending = [r for r in rows if not r["fields"].get("posted_to_fund")]

    fund_inserts = []
    debit_ids = []
    grandfather_ids = []

    for r in pending:
        f = r["fields"]
        if f.get("source") == "Live entry":
            amount = f.get("amount") or 0
            fund_inserts.append({
                "date": f.get(date_field),
                "fund": fund_label,
                "description": desc_fn(f, r["id"]),
                "amount": amount,
                "encoded_by": "Ayk",
            })
            debit_ids.append(r["id"])
        else:
            grandfather_ids.append(r["id"])

    add_records(DAGUPAN_DOC, "Fund_Remittance_Payments", fund_inserts)

    all_mark = [(rid, {"posted_to_fund": True}) for rid in (debit_ids + grandfather_ids)]
    update_records(doc_id, table_id, all_mark)

    return len(debit_ids), len(grandfather_ids)


def main():
    tax_paid, tax_grandfathered = process_table(
        TAX_DOC, "Tax_Remittances", "Tax", "date_paid",
        lambda f, rid: f"{f.get('client_code_raw', '')} / {f.get('form', '')} (TaxRem#{rid})",
    )
    contrib_paid, contrib_grandfathered = process_table(
        CONTRIB_DOC, "Contribution_Remittances", "Contributions (SSS/PHIC/HDMF)", "date",
        lambda f, rid: f"{f.get('client_code_raw', '')} / {f.get('contribution_type', '')} (ContribRem#{rid})",
    )

    print(f"Tax_Remittances: {tax_paid} posted as new Fund_Remittance_Payments rows, "
          f"{tax_grandfathered} historical rows flagged (no payment row).")
    print(f"Contribution_Remittances: {contrib_paid} posted as new Fund_Remittance_Payments rows, "
          f"{contrib_grandfathered} historical rows flagged (no payment row).")
    print(f"Total new Fund_Remittance_Payments rows this run: {tax_paid + contrib_paid}")


if __name__ == "__main__":
    main()
