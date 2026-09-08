#!/usr/bin/env python3
"""
LVVI Contribution_Remittances - historical remittance_channel backfill
------------------------------------------------------------------------
One-off script. Sets remittance_channel = "Tax & Contributions Fund" on every
Contribution_Remittances row (LVVI Contributions doc) that doesn't have a
channel value yet.

Why this default for every historical row: every one of these rows was
migrated from Dagupan/Pozorrubio Cash_disbursements or split out of the PHIC
Liquidation report -- all of it was originally paid using LVVI's own cash
custody (till or Fund), never through a bypass channel like Direct-Chinabank,
Direct-Unionbank, or a client's own account. There is no evidence of any
contribution-side bypass case (unlike the tax side's known EFPS-direct
clients), so a single blanket default is correct here, and cheaper than
resolving 1,275 rows individually. If a bypass case ever turns up, it can be
corrected by hand afterward via the Contribution Remittances entry widget's
Edit action.

Idempotent: only touches rows where remittance_channel is currently empty, so
it's safe to re-run (e.g. if it's interrupted partway through).

Requires env var GRIST_API_KEY (same secret already used by fund_refresh.py).
Intended to be run once via workflow_dispatch, not on a schedule.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

GRIST_KEY = os.environ["GRIST_API_KEY"]
BASE = "https://docs.getgrist.com/api/docs"

CONTRIB_DOC = "uAi5sxhezG9CgfF2cjtGRy"
TABLE = "Contribution_Remittances"
DEFAULT_CHANNEL = "Tax & Contributions Fund"

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
    url = f"{BASE}/{doc_id}/tables/{table_id}/records"
    data = _req("GET", url, params={"limit": 20000})
    return data["records"]


def update_records(doc_id, table_id, id_field_pairs):
    if not id_field_pairs:
        return
    url = f"{BASE}/{doc_id}/tables/{table_id}/records"
    for i in range(0, len(id_field_pairs), CHUNK):
        chunk = id_field_pairs[i:i + CHUNK]
        _req("PATCH", url, body={"records": [{"id": rid, "fields": f} for rid, f in chunk]})


def main():
    rows = list_all(CONTRIB_DOC, TABLE)
    blank_ids = [r["id"] for r in rows if not (r["fields"].get("remittance_channel") or "").strip()]

    print(f"{len(rows)} total rows in {TABLE}; {len(blank_ids)} have a blank remittance_channel.")

    updates = [(rid, {"remittance_channel": DEFAULT_CHANNEL}) for rid in blank_ids]
    update_records(CONTRIB_DOC, TABLE, updates)

    print(f"Backfilled {len(blank_ids)} rows to remittance_channel = '{DEFAULT_CHANNEL}'.")


if __name__ == "__main__":
    main()
