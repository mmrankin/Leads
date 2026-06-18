#!/usr/bin/env python3
"""One-time migration: create the platform's data tables in the SQL Server
`dlrPro` database (10.1.1.10) and copy existing rows from the local SQLite
stores. Idempotent: a table is only created if missing, and only populated if
empty. Caches (squish_map/vin8_map/inv_prefix_count) are NOT migrated — they
stay local SQLite.

Env: DLRPRO_DB_SERVER (10.1.1.10), DLRPRO_DB_USER (sa), DLRPRO_DB_PASSWORD,
     DLRPRO_DB_NAME (dlrPro).
"""
import os
import sqlite3
import pymssql

CLAUDE = "/Users/markrankin/claude"
SETS = [
    (f"{CLAUDE}/platform/platform.db",
     ["dealers", "products", "dealer_products",
      "dealer_valuation_settings", "dealer_credit_settings"]),
    (f"{CLAUDE}/dealer-leads/leads.db", ["leads"]),
    (f"{CLAUDE}/trade-in/trade_in.db", ["trade_leads"]),
    (f"{CLAUDE}/credit-estimator/credit.db", ["credit_leads"]),
]
BIG = {"adf_xml", "comments", "email_detail", "email1_detail", "email2_detail",
       "source", "banner_url", "description"}
UNIQUES = {"dealers": [["dealer_id"]],
           "dealer_products": [["dealer_id", "product_code"]]}
IDENTITY_TABLES = {"dealers", "dealer_products", "leads", "trade_leads", "credit_leads"}


def dlr():
    return pymssql.connect(
        server=os.environ.get("DLRPRO_DB_SERVER", "10.1.1.10"),
        user=os.environ.get("DLRPRO_DB_USER", "sa"),
        password=os.environ["DLRPRO_DB_PASSWORD"],
        database=os.environ.get("DLRPRO_DB_NAME", "dlrPro"),
        timeout=120, login_timeout=15,
    )


def col_type(name, typ, pk):
    t = (typ or "").upper()
    if pk and t == "INTEGER" and name == "id":
        return "INT IDENTITY(1,1) PRIMARY KEY"
    if pk:
        return "NVARCHAR(64) NOT NULL PRIMARY KEY"
    if t == "INTEGER":
        return "INT"
    if t == "REAL":
        return "FLOAT"
    if name in ("created_at", "updated_at", "tc_agreed_at"):
        return "NVARCHAR(32)"
    return "NVARCHAR(MAX)" if name in BIG else "NVARCHAR(255)"


def build_create(table, cols):
    parts = []
    for cid, name, typ, nn, dflt, pk in cols:
        decl = f"[{name}] " + col_type(name, typ, pk)
        if "PRIMARY KEY" in decl:
            parts.append(decl); continue
        if name in ("created_at", "updated_at") and dflt and "datetime" in str(dflt):
            decl += " DEFAULT (CONVERT(varchar(19), SYSUTCDATETIME(), 120))"
        elif dflt is not None and "datetime" not in str(dflt):
            decl += f" DEFAULT ({dflt})"
        if nn:
            decl += " NOT NULL"
        parts.append(decl)
    for u in UNIQUES.get(table, []):
        cols_csv = ",".join(f"[{c}]" for c in u)
        parts.append(f"CONSTRAINT [UQ_{table}_{'_'.join(u)}] UNIQUE ({cols_csv})")
    body = ",\n  ".join(parts)
    return (f"IF OBJECT_ID(N'dbo.{table}','U') IS NULL\n"
            f"CREATE TABLE dbo.{table} (\n  {body}\n);")


def main():
    mcon = dlr()
    mcur = mcon.cursor()
    for path, tables in SETS:
        scon = sqlite3.connect(path)
        scon.row_factory = sqlite3.Row
        for table in tables:
            cols = scon.execute(f"PRAGMA table_info({table})").fetchall()
            colnames = [c[1] for c in cols]
            # 1) create table if missing
            mcur.execute(build_create(table, cols))
            mcon.commit()
            # 2) populate only if target empty
            mcur.execute(f"SELECT COUNT(*) FROM dbo.{table}")
            n = mcur.fetchone()[0]
            if n:
                print(f"  {table}: target already has {n} rows — skip copy")
                continue
            rows = scon.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"  {table}: source empty — table created, no rows")
                continue
            collist = ",".join(f"[{c}]" for c in colnames)
            ph = ",".join(["%s"] * len(colnames))
            ident = table in IDENTITY_TABLES
            if ident:
                mcur.execute(f"SET IDENTITY_INSERT dbo.{table} ON")
            mcur.executemany(
                f"INSERT INTO dbo.{table} ({collist}) VALUES ({ph})",
                [tuple(r[c] for c in colnames) for r in rows],
            )
            if ident:
                mcur.execute(f"SET IDENTITY_INSERT dbo.{table} OFF")
            mcon.commit()
            print(f"  {table}: copied {len(rows)} rows")
        scon.close()
    mcon.close()
    print("done.")


if __name__ == "__main__":
    main()
