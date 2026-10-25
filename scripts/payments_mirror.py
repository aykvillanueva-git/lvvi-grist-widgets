"""Mirror client-tagged office Payments into the remittance tables.

When a client's tax, permit, SSS/PHIC/HDMF or client's-expense payment goes out
through an office's Payments ledger (Cash_disbursements, `client` set) instead of the
Tax & Contributions Fund, it never reached Tax_Remittances / Contribution_Remittances,
so the client looked "unremitted" (per Ayk 2026-10-25, e.g. Lusanta's Jun-5 ₱406,873).

Each run (called at the start of ledger_cache.refresh()):
  - looks at live Cash_disbursements rows (both offices) dated 2026-07-01 onward that
    have a client and a client-related amount:
        * sss/phic/hdmf_employer_amount on a client row -> Contribution_Remittances (that type)
        * others_amount2 with a BIR/LGU/Taxes description -> Tax_Remittances if the
          description mentions BIR/Tax, else Contribution_Remittances (Blended/permits)
        * contri_others_amount ("Client's Expenses")     -> Contribution_Remittances (Blended)
  - skips a row already linked (payments_ref = "dag:<id>" / "poz:<id>"), or one that
    already has a matching remittance (same client, amount within ₱1, date within 45
    days) -- that existing remittance gets the payments_ref stamped instead.
  - inserts the rest with channel "Office Payments (paid from office cash)",
    posted_to_fund = True (so fund_refresh never draws the Fund for them).
Idempotent. Deposit/Remit/GCash, rent, supplies etc. are never touched.
"""
import datetime

from ledger_cache import (TAX_DOC, CONTRIB_DOC, OFFICE_DOCS, list_all, add_records,
                          update_records)

CHANNEL = "Office Payments (paid from office cash)"
START = int(datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc).timestamp())
TAXY = ("bir", "lgu", "tax", "license", "permit")
PREFIX = {"Dagupan": "dag", "Pozorrubio": "poz"}


def _matches(existing, client, amount, date):
    for r in existing:
        f = r["fields"]
        d = f.get("date") or f.get("date_paid") or 0
        if f.get("client") == client and abs((f.get("amount") or 0) - amount) <= 1 \
                and d and abs(d - date) <= 45 * 86400:
            return r
    return None


def mirror(dry_run=False):
    tax_clients = {r["id"]: r["fields"] for r in list_all(TAX_DOC, "Clients")}
    con_clients = {r["id"]: r["fields"] for r in list_all(CONTRIB_DOC, "Clients")}
    tax_rem = list_all(TAX_DOC, "Tax_Remittances")
    con_rem = list_all(CONTRIB_DOC, "Contribution_Remittances")
    linked = {f.get("payments_ref") for f in
              (r["fields"] for r in tax_rem + con_rem) if f.get("payments_ref")}
    used = set()  # remittance ids already matched this run

    new_tax, new_con, stamp_tax, stamp_con = [], [], [], []
    for office, (doc_id, src_field) in OFFICE_DOCS.items():
        tax_by_src = {f.get(src_field): cid for cid, f in tax_clients.items() if f.get(src_field)}
        con_by_src = {f.get(src_field): cid for cid, f in con_clients.items() if f.get(src_field)}
        for r in list_all(doc_id, "Cash_disbursements"):
            f = r["fields"]
            oc, date = f.get("client") or 0, f.get("date") or 0
            if not oc or date < START:
                continue
            ref = f"{PREFIX[office]}:{r['id']}"
            if ref in linked:
                continue
            desc = (f.get("others_description") or "").lower()
            parts = []  # (table, type/form, amount)
            for col, typ in (("sss_employer_amount", "SSS"), ("phic_employer_amount", "PHIC"),
                             ("hdmf_employer_amount", "HDMF")):
                if f.get(col):
                    parts.append(("S", typ, f[col]))
            if f.get("others_amount2") and any(w in desc for w in TAXY):
                if "bir" in desc or "tax" in desc:
                    parts.append(("T", "BIR/LGU (via Payments)", f["others_amount2"]))
                else:
                    parts.append(("S", "Blended/Unspecified", f["others_amount2"]))
            if f.get("contri_others_amount"):
                parts.append(("S", "Blended/Unspecified", f["contri_others_amount"]))
            for kind, typ, amt in parts:
                amt = round(float(amt), 2)
                if kind == "T":
                    cid = tax_by_src.get(oc)
                    pool, stamps = tax_rem, stamp_tax
                else:
                    cid = con_by_src.get(oc)
                    pool, stamps = con_rem, stamp_con
                if not cid:
                    continue
                hit = _matches([x for x in pool if x["id"] not in used], cid, amt, date)
                if hit:
                    used.add(hit["id"])
                    if not hit["fields"].get("payments_ref"):
                        stamps.append((hit["id"], {"payments_ref": ref}))
                    continue
                code = (tax_clients if kind == "T" else con_clients)[cid].get("A") or ""
                base = {"office": office, "client": cid, "client_code_raw": code, "amount": amt,
                        "remittance_channel": CHANNEL, "posted_to_fund": True,
                        "encoded_by": "Ayk", "payments_ref": ref}
                if kind == "T":
                    new_tax.append(dict(base, date_paid=date, form=typ,
                                        source=f"Auto: office Payments {ref} (paid from office cash, not via Fund)"))
                else:
                    new_con.append(dict(base, date=date, contribution_type=typ,
                                        payment_method=f.get("payment_method") or "Cash",
                                        source="Live entry"))

    if dry_run:
        for x in new_tax:
            print("WOULD ADD TAX ", x["payments_ref"], x["client_code_raw"], x["date_paid"], x["amount"], x["form"])
        for x in new_con:
            print("WOULD ADD CON ", x["payments_ref"], x["client_code_raw"], x["date"], x["amount"], x["contribution_type"])
        print(f"DRY RUN: {len(new_tax)} tax, {len(new_con)} contri would be added "
              f"(₱{sum(x['amount'] for x in new_tax):,.0f} / ₱{sum(x['amount'] for x in new_con):,.0f}); "
              f"{len(stamp_tax) + len(stamp_con)} existing would be linked")
        return
    add_records(TAX_DOC, "Tax_Remittances", new_tax)
    add_records(CONTRIB_DOC, "Contribution_Remittances", new_con)
    update_records(TAX_DOC, "Tax_Remittances", stamp_tax)
    update_records(CONTRIB_DOC, "Contribution_Remittances", stamp_con)
    if new_tax or new_con:  # make the ledger rebuild see the new rows
        list_all(TAX_DOC, "Tax_Remittances", fresh=True)
        list_all(CONTRIB_DOC, "Contribution_Remittances", fresh=True)
    print(f"payments_mirror: {len(new_tax)} tax + {len(new_con)} contribution remittances added "
          f"from office Payments; {len(stamp_tax) + len(stamp_con)} existing remittances linked")


if __name__ == "__main__":
    import sys
    mirror(dry_run="--dry-run" in sys.argv)
