#!/usr/bin/env python3
"""
LVVI Fund Refresh
------------------
Keeps Cash_Fund_Injections (Dagupan doc) in sync with Tax_Remittances (LVVI Taxes doc)
and Contribution_Remittances (LVVI Contributions doc), and ALSO feeds the fund-level
monitor (Fund_Remittance_Payments / Fund_Accountability, Dagupan doc).

Logic:
  - Only rows tagged source == "Live entry" represent remittances encoded live,
    going forward, that should draw down the Fund. These get a matching negative
    ("debit") row inserted into Cash_Fund_Injections -- this feeds Gladys_Accountability,
    which nets ALL Cash_Fund_Injections activity (any purpose) against her cash-on-hand,
    so this part covers every contribution type (SSS/PHIC/HDMF/Blended), not just PHIC.
  - All other pending rows (historical batch-imported / migrated data that predates
    the Fund ledger) are simply flagged posted_to_fund = True with NO debit, so they
    are never reprocessed and never distort the running balance.
  - NEW: live-entry rows also get a mirrored POSITIVE row in Fund_Remittance_Payments,
    so Fund_Accountability's payments_out (and therefore Net Fund Balance) updates
    automatically instead of Ayk re-entering the same total by hand from the
    Liquidation report:
      * Every live Tax_Remittances row -> Fund_Remittance_Payments, fund="UB Tax Fund"
      * Every live Contribution_Remittances row (SSS, PHIC, HDMF, or Blended/Unspecified
        alike) -> Fund_Remittance_Payments, fund="Contributions Fund (SSS/PHIC/HDMF)".
        Confirmed 2026-08-22 by Ayk: unlike the UB Tax side, SSS/PHIC/HDMF remittances
        are NOT funded from separately itemized pools (there's no dedicated SSS/HDMF
        tab in the Liquidation workbook the way PHIC 2026 has one) -- they draw from
        the same general Fund the daily reports' "FUND (in)" column already feeds, and
        going forward will simply be listed here by batch as Contribution_Remittances
        entries come in. So this fund is NOT PHIC-only; it covers all contribution
        types together, with a single shared opening balance/net balance.
  - Idempotent: only rows where posted_to_fund is not yet true are touched, so this
    is safe to run repeatedly (e.g. every time the widget button triggers it). Both
    the Cash_Fund_Injections debit and the Fund_Remittance_Payments mirror (when
    applicable) are keyed off the same posted_to_fund flag on the source row, so a
    source row is never processed twice.

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


def process_table(doc_id, table_id, purpose_label, date_field, desc_fn, fund_fn):
    """
    fund_fn(fields) -> fund name ("UB Tax Fund" / "PHIC Fund") or None if this
    row is out of scope for the Fund_Remittance_Payments mirror.
    """
    rows = list_all(doc_id, table_id)
    pending = [r for r in rows if not r["fields"].get("posted_to_fund")]

    fund_inserts = []       # negative debits -> Cash_Fund_Injections (Gladys_Accountability)
    remittance_inserts = []  # positive mirror -> Fund_Remittance_Payments (Fund_Accountability)
    debit_ids = []
    grandfather_ids = []

    for r in pending:
        f = r["fields"]
        if f.get("source") == "Live entry":
            amount = f.get("amount") or 0
            office = f.get("office") or "Dagupan"
            date_val = f.get(date_field)
            desc = desc_fn(f, r["id"])

            fund_inserts.append({
                "encoded_by": "Ayk",
                "date": date_val,
                "office": office,
                "purpose": purpose_label,
                "description": desc,
                "amount": -amount,
            })

            fund_name = fund_fn(f)
            if fund_name:
                remittance_inserts.append({
                    "date": date_val,
                    "fund": fund_name,
                    "description": desc,
                    "amount": amount,
                    "encoded_by": "Ayk",
                })

            debit_ids.append(r["id"])
        else:
            grandfather_ids.append(r["id"])

    add_records(DAGUPAN_DOC, "Cash_Fund_Injections", fund_inserts)
    add_records(DAGUPAN_DOC, "Fund_Remittance_Payments", remittance_inserts)

    all_mark = [(rid, {"posted_to_fund": True}) for rid in (debit_ids + grandfather_ids)]
    update_records(doc_id, table_id, all_mark)

    return len(debit_ids), len(remittance_inserts), len(grandfather_ids)


def main():
    tax_debits, tax_fund_rows, tax_grandfathered = process_table(
        TAX_DOC, "Tax_Remittances", "Tax Remittance", "date_paid",
        lambda f, rid: f"{f.get('client_code_raw', '')} / {f.get('form', '')} (TaxRem#{rid})",
        lambda f: "UB Tax Fund",  # every live tax remittance is in scope
    )
    contrib_debits, contrib_fund_rows, contrib_grandfathered = process_table(
        CONTRIB_DOC, "Contribution_Remittances", "Contribution Remittance (SSS/PHIC/HDMF)", "date",
        lambda f, rid: f"{f.get('client_code_raw', '')} / {f.get('contribution_type', '')} (ContribRem#{rid})",
        lambda f: "Contributions Fund (SSS/PHIC/HDMF)",  # all types share one fund -- see note above
    )

    print(f"Tax_Remittances: {tax_debits} posted as new Fund debits "
          f"({tax_fund_rows} mirrored to Fund_Remittance_Payments), "
          f"{tax_grandfathered} historical rows flagged (no debit).")
    print(f"Contribution_Remittances: {contrib_debits} posted as new Fund debits "
          f"({contrib_fund_rows} mirrored to Fund_Remittance_Payments, all contribution types), "
          f"{contrib_grandfathered} historical rows flagged (no debit).")
    print(f"Total new Fund debit rows this run: {tax_debits + contrib_debits}")
    print(f"Total new Fund_Remittance_Payments rows this run: {tax_fund_rows + contrib_fund_rows}")


if __name__ == "__main__":
    main()
