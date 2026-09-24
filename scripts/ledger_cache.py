#!/usr/bin/env python3
"""
LVVI Tax & Contribution Ledger Cache
------------------------------------
Builds `Tax_Contri_Ledger` in each OFFICE doc (Dagupan, Pozorrubio) from the
LVVI Taxes doc (Tax_Collections, Tax_Remittances) and the LVVI Contributions doc
(Contribution_Collections, Contribution_Remittances).

Why a cache: a Grist custom widget can only read its own document, and the
Dagupan doc is on the Free plan (5,000-row cap). Mirroring the ~2,900 raw
Dagupan tax/contribution rows would nearly fill it, so instead each office doc
gets ONE ROW PER CLIENT, with that client's transactions packed as JSON in
`txns_json`. The "Tax & Contribution Transactions" search widget reads it.

Rules (match the Tax/Contribution variance tables in the Taxes/Contributions docs):
  - Cash basis: collections by `date`; tax remittances by `date_paid`;
    contribution remittances by `date`.
  - Contribution collections count SSS + PHIC + HDMF only (other_fees excluded),
    split into one line per nonzero type.
  - A transaction belongs to an office by its own `office` field; if blank,
    by its client's office in the source doc.
  - Office client link: source_dagupan_id / source_pozorrubio_id first, then an
    exact (case-insensitive) client_name match; otherwise the row is keyed by name
    only ("n:<NAME>") with no client link.

Idempotent full rebuild: upserts by `ledger_key`, deletes stale keys.

Also (ensure_variance_rows) adds any missing (client, year, month) rows to
Tax_Variance_Monthly_ByClient and Contribution_Variance_Monthly_ByClient so new
activity always shows up on the variance pages.
Called from fund_refresh.py main(); can also be run standalone:
    GRIST_API_KEY=... python3 scripts/ledger_cache.py
"""
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

BASE = "https://docs.getgrist.com/api/docs"
TAX_DOC = "144xnp6dgaMypPtF6vyRuL"
CONTRIB_DOC = "uAi5sxhezG9CgfF2cjtGRy"
OFFICE_DOCS = {
    "Dagupan": ("xcJuTqTrGePQUeBxAUmtVb", "source_dagupan_id"),
    "Pozorrubio": ("o5AfuUgWmt2ho4wm24gq35", "source_pozorrubio_id"),
}
TABLE = "Tax_Contri_Ledger"
CHUNK = 25  # rows carry sizeable JSON blobs -- keep request bodies modest


def _key():
    return os.environ["GRIST_API_KEY"]


def _req(method, url, body=None, params=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {_key()}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} on {method} {url}: {e.read().decode()}", file=sys.stderr)
        raise


def list_all(doc_id, table_id):
    # No offset pagination on GET /records -- one call with a high limit.
    return _req("GET", f"{BASE}/{doc_id}/tables/{table_id}/records", params={"limit": 20000})["records"]


def _chunks(seq):
    for i in range(0, len(seq), CHUNK):
        yield seq[i:i + CHUNK]


def add_records(doc_id, table_id, fields_list):
    for chunk in _chunks(fields_list):
        _req("POST", f"{BASE}/{doc_id}/tables/{table_id}/records",
             body={"records": [{"fields": f} for f in chunk]})


def update_records(doc_id, table_id, pairs):
    for chunk in _chunks(pairs):
        _req("PATCH", f"{BASE}/{doc_id}/tables/{table_id}/records",
             body={"records": [{"id": rid, "fields": f} for rid, f in chunk]})


def delete_records(doc_id, table_id, ids):
    for chunk in _chunks(ids):
        _req("POST", f"{BASE}/{doc_id}/tables/{table_id}/data/delete", body=chunk)


# ---------------------------------------------------------------- helpers
def iso(ts):
    if ts in (None, "", 0):
        return None
    try:
        return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def norm(name):
    return " ".join(str(name or "").upper().split())


def money(x):
    return round(float(x or 0), 2)


def clients_by_id(doc_id):
    return {r["id"]: r["fields"] for r in list_all(doc_id, "Clients")}


# ---------------------------------------------------------------- build
def collect_transactions():
    """Return a flat list of normalized transactions from both source docs."""
    tax_clients = clients_by_id(TAX_DOC)
    con_clients = clients_by_id(CONTRIB_DOC)
    out = []

    def base(src_clients, f, date_val, raw_code=""):
        cid = f.get("client") or 0
        c = src_clients.get(cid) or {}
        return {
            "office": f.get("office") or c.get("office") or "",
            "src_client": c,
            "name": c.get("client_name") or c.get("A") or raw_code or "(no client)",
            "code": c.get("A") or raw_code or "",
            "d": iso(date_val),
        }

    for r in list_all(TAX_DOC, "Tax_Collections"):
        f = r["fields"]
        t = base(tax_clients, f, f.get("date"))
        t.update({"g": "T", "k": "C", "t": "", "a": money(f.get("amount")),
                  "ch": "", "m": "", "e": "", "p": None, "id": r["id"]})
        if t["a"]:
            out.append(t)

    for r in list_all(TAX_DOC, "Tax_Remittances"):
        f = r["fields"]
        t = base(tax_clients, f, f.get("date_paid"), f.get("client_code_raw") or "")
        t.update({"g": "T", "k": "R", "t": (f.get("form") or "").strip(), "a": money(f.get("amount")),
                  "ch": f.get("remittance_channel") or "", "m": "", "e": f.get("encoded_by") or "",
                  "p": iso(f.get("return_period")), "id": r["id"]})
        if t["a"]:
            out.append(t)

    for r in list_all(CONTRIB_DOC, "Contribution_Collections"):
        f = r["fields"]
        for col, label in (("sss", "SSS"), ("phic", "PHIC"), ("hdmf", "HDMF")):
            amt = money(f.get(col))
            if not amt:
                continue
            t = base(con_clients, f, f.get("date"))
            t.update({"g": "S", "k": "C", "t": label, "a": amt, "ch": "", "m": "", "e": "",
                      "p": None, "id": r["id"]})
            out.append(t)

    for r in list_all(CONTRIB_DOC, "Contribution_Remittances"):
        f = r["fields"]
        t = base(con_clients, f, f.get("date"), f.get("client_code_raw") or "")
        t.update({"g": "S", "k": "R", "t": f.get("contribution_type") or "Blended/Unspecified",
                  "a": money(f.get("amount")), "ch": f.get("remittance_channel") or "",
                  "m": f.get("payment_method") or "", "e": f.get("encoded_by") or "",
                  "p": None, "id": r["id"]})
        if t["a"]:
            out.append(t)
    return out


def build_office_rows(office, office_doc, source_id_field, txns, synced_at):
    office_clients = clients_by_id(office_doc)
    by_name = {}
    for cid, f in office_clients.items():
        by_name.setdefault(norm(f.get("client_name")), cid)

    groups = {}
    skipped = 0
    for t in txns:
        if t["office"] != office:
            if not t["office"]:
                skipped += 1
            continue
        sid = t["src_client"].get(source_id_field)
        oc = sid if sid in office_clients else by_name.get(norm(t["name"]))
        key = f"c{oc}" if oc else f"n:{norm(t['name'])}"
        g = groups.setdefault(key, {"client": oc or None, "name": t["name"], "code": t["code"], "txns": []})
        if oc and office_clients[oc].get("client_name"):
            g["name"] = office_clients[oc]["client_name"]
            g["code"] = office_clients[oc].get("A") or g["code"]
        g["txns"].append({k: t[k] for k in ("d", "g", "k", "t", "a", "ch", "m", "e", "p", "id")})

    rows = {}
    for key, g in groups.items():
        tx = sorted(g["txns"], key=lambda x: (x["d"] or "", x["g"], x["k"], x["id"]))
        sums = {("T", "C"): 0.0, ("T", "R"): 0.0, ("S", "C"): 0.0, ("S", "R"): 0.0}
        for x in tx:
            sums[(x["g"], x["k"])] += x["a"]
        dates = [x["d"] for x in tx if x["d"]]
        last = max(dates) if dates else None
        last_ts = None
        if last:
            y, m, d = map(int, last.split("-"))
            last_ts = int(datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc).timestamp())
        rows[key] = {
            "ledger_key": key,
            "client": g["client"] or 0,
            "client_name": g["name"],
            "tax_collected": round(sums[("T", "C")], 2),
            "tax_remitted": round(sums[("T", "R")], 2),
            "contri_collected": round(sums[("S", "C")], 2),
            "contri_remitted": round(sums[("S", "R")], 2),
            "txn_count": len(tx),
            "last_activity": last_ts,
            "txns_json": json.dumps({"code": g["code"], "office": office, "txns": tx},
                                    separators=(",", ":")),
            "synced_at": synced_at,
        }
    return rows, skipped


# ---------------------------------------------------------------- variance-row upkeep
# Tax_Variance_Monthly_ByClient / Contribution_Variance_Monthly_ByClient only show a
# (client, month) that has a row; nothing else creates those rows. This adds any missing
# (client, year, month) that has a linked, dated collection or remittance. The formula
# columns fill in collected/remitted/variance on their own. Never deletes rows.
VARIANCE_SOURCES = {
    TAX_DOC: ("Tax_Variance_Monthly_ByClient",
              [("Tax_Collections", "date"), ("Tax_Remittances", "date_paid")]),
    CONTRIB_DOC: ("Contribution_Variance_Monthly_ByClient",
                  [("Contribution_Collections", "date"), ("Contribution_Remittances", "date")]),
}


def ensure_variance_rows():
    for doc_id, (var_table, sources) in VARIANCE_SOURCES.items():
        have = set()
        for r in list_all(doc_id, var_table):
            f = r["fields"]
            have.add((f.get("client") or 0, f.get("year"), f.get("month_num")))
        need = set()
        for table_id, date_col in sources:
            for r in list_all(doc_id, table_id):
                f = r["fields"]
                cid, ts = f.get("client") or 0, f.get(date_col)
                if not cid or not ts:
                    continue
                d = datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc)
                key = (cid, d.year, d.month)
                if key not in have:
                    need.add(key)
        add_records(doc_id, var_table, [{"client": c, "year": y, "month_num": m}
                                        for c, y, m in sorted(need)])
        print(f"{var_table}: {len(need)} missing client-month row(s) added"
              + (": " + ", ".join(f"client {c} {y}-{m:02d}" for c, y, m in sorted(need)) if need else ""))


def refresh():
    try:
        ensure_variance_rows()
    except Exception as e:  # noqa: BLE001 -- never block the ledger rebuild
        print(f"::error::variance-row upkeep failed: {e}", file=sys.stderr)
    synced_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    txns = collect_transactions()
    for office, (doc_id, src_field) in OFFICE_DOCS.items():
        rows, skipped = build_office_rows(office, doc_id, src_field, txns, synced_at)
        existing = {r["fields"].get("ledger_key"): r["id"] for r in list_all(doc_id, TABLE)}
        updates = [(existing[k], f) for k, f in rows.items() if k in existing]
        inserts = [f for k, f in rows.items() if k not in existing]
        stale = [rid for k, rid in existing.items() if k not in rows]
        update_records(doc_id, TABLE, updates)
        add_records(doc_id, TABLE, inserts)
        delete_records(doc_id, TABLE, stale)
        n = sum(r["txn_count"] for r in rows.values())
        print(f"{TABLE} [{office}]: {len(rows)} clients, {n} transactions "
              f"({len(updates)} updated, {len(inserts)} added, {len(stale)} removed)"
              + (f"; {skipped} source rows had no office and were skipped" if skipped else ""))


if __name__ == "__main__":
    refresh()
