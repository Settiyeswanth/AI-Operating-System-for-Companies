"""
Adversarial verification for create_canonical_person race-condition fix.
Checks the ACTUAL claim (same canonical_id returned) vs what the test
asserts (lookup returns not-None). Also audits raw row counts and schema.

Run with:  uv run python scripts/_verify_er_fix.py
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "services/ingestion")
sys.path.insert(0, "packages/aios-core")

from ingestion.entity_resolution.resolver import EntityResolutionIndex

failures = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if detail:
        print(f"         {detail}")
    if not condition:
        failures.append(label)


tmpdir = tempfile.mkdtemp()

# ---------------------------------------------------------------------------
# Test 1: Sequential duplicate calls — raw row count
# ---------------------------------------------------------------------------
print("\n--- Test 1: Sequential duplicates -- raw row count")
db1 = Path(tmpdir) / "t1.db"
idx = EntityResolutionIndex(db_path=db1)
cid1 = idx.create_canonical_person("alice@co.com", "Alice", "github", "alice-gh")
cid2 = idx.create_canonical_person("alice@co.com", "Alice", "linear", "alice-lin")

with sqlite3.connect(db1) as conn:
    cp_rows = conn.execute(
        "SELECT canonical_id, canonical_email FROM canonical_persons"
    ).fetchall()
    em_rows = conn.execute(
        "SELECT source_system, source_id, canonical_id FROM entity_map ORDER BY source_system"
    ).fetchall()

print(f"  canonical_persons rows: {len(cp_rows)}")
for r in cp_rows:
    print(f"    canonical_id={r[0]}  email={r[1]}")
print(f"  entity_map rows: {len(em_rows)}")
for r in em_rows:
    print(f"    {r[0]}:{r[1]} -> {r[2]}")
print(f"  cid1={cid1}")
print(f"  cid2={cid2}")

check("canonical_persons has exactly 1 row", len(cp_rows) == 1,
      f"got {len(cp_rows)}")
check("entity_map has exactly 2 rows (github + linear)", len(em_rows) == 2,
      f"got {len(em_rows)}")
check("cid1 == cid2  (the actual fix claim)", cid1 == cid2,
      f"cid1={cid1}  cid2={cid2}")
check("both entity_map rows point to same canonical_id",
      em_rows[0][2] == em_rows[1][2],
      f"{em_rows[0][2]} vs {em_rows[1][2]}")

# ---------------------------------------------------------------------------
# Test 2: Interleaved race simulation at the INSERT/SELECT gap
# ---------------------------------------------------------------------------
print("\n--- Test 2: Interleaved race at INSERT/SELECT gap")
db2 = Path(tmpdir) / "t2.db"
EntityResolutionIndex(db_path=db2)  # bootstrap schema only

new_id_a = "canonical-aaa"
new_id_b = "canonical-bbb"
email = "bob@co.com"

conn_a = sqlite3.connect(db2)
conn_a.row_factory = sqlite3.Row
conn_b = sqlite3.connect(db2)
conn_b.row_factory = sqlite3.Row

# A inserts successfully
conn_a.execute(
    "INSERT OR IGNORE INTO canonical_persons (canonical_id, canonical_email, display_names)"
    " VALUES (?,?,?)",
    (new_id_a, email, json.dumps(["Bob"]))
)
conn_a.commit()

# B's INSERT is silently ignored (email not unique — see Test 4)
conn_b.execute(
    "INSERT OR IGNORE INTO canonical_persons (canonical_id, canonical_email, display_names)"
    " VALUES (?,?,?)",
    (new_id_b, email, json.dumps(["Bob"]))
)
conn_b.commit()

# B reads back — must get A's canonical_id
row_b = conn_b.execute(
    "SELECT canonical_id FROM canonical_persons WHERE canonical_email=?", (email,)
).fetchone()
winner = row_b["canonical_id"] if row_b else None

raw_count = conn_a.execute(
    "SELECT COUNT(*) FROM canonical_persons WHERE canonical_email=?", (email,)
).fetchone()[0]

print(f"  A inserted:         {new_id_a}")
print(f"  B tried to insert:  {new_id_b}")
print(f"  B reads back:       {winner}")
print(f"  raw row count:      {raw_count}")

check("only 1 row for email after race", raw_count == 1)
check("B reads back A's canonical_id (not its own)", winner == new_id_a,
      f"winner={winner}  expected={new_id_a}")

conn_a.close()
conn_b.close()

# ---------------------------------------------------------------------------
# Test 3: Assertion strength — what test asserts vs what fix claims
# ---------------------------------------------------------------------------
print("\n--- Test 3: Assertion strength audit")
db3 = Path(tmpdir) / "t3.db"
idx3 = EntityResolutionIndex(db_path=db3)
c1 = idx3.create_canonical_person("carol@co.com", "Carol", "github", "carol-gh")
c2 = idx3.create_canonical_person("carol@co.com", "Carol", "linear", "carol-lin")
email_lookup = idx3.lookup_by_email("carol@co.com")

print(f"  test asserts: email_cid is not None  -> {email_lookup is not None}")
print(f"  fix claims:   cid1 == cid2           -> {c1 == c2}")
print(f"  c1={c1}")
print(f"  c2={c2}")

check("weak assertion (test passes): email_cid is not None",
      email_lookup is not None)
check("strong assertion (fix claim): cid1 == cid2", c1 == c2,
      "TEST IS GREEN BUT FIX IS WRONG if this fails")

# ---------------------------------------------------------------------------
# Test 4: Schema constraint audit — what does INSERT OR IGNORE actually enforce?
# ---------------------------------------------------------------------------
print("\n--- Test 4: Schema constraint audit")
with sqlite3.connect(db1) as conn:
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='canonical_persons'"
    ).fetchone()
    schema = schema_row[0] if schema_row else ""
    print(f"  canonical_persons DDL:")
    for line in schema.splitlines():
        print(f"    {line}")

    indices = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index'"
        " AND tbl_name='canonical_persons'"
    ).fetchall()
    print(f"  Indices: {[r[0] for r in indices]}")

# What INSERT OR IGNORE actually enforces:
# It fires on ANY constraint violation: PRIMARY KEY, UNIQUE, NOT NULL.
# canonical_id is PRIMARY KEY.
# canonical_email is declared UNIQUE (both inline in DDL and via
# CREATE UNIQUE INDEX idx_email_unique).  INSERT OR IGNORE will fire on
# email collisions, preventing duplicate rows at DB level.

has_pk = "canonical_id TEXT PRIMARY KEY" in schema
# The UNIQUE constraint may appear as: "canonical_email TEXT UNIQUE" in the
# column DDL, OR as an autoindex in sqlite_master.  Check both.
has_unique_email = (
    ("UNIQUE" in schema.upper() and "canonical_email" in schema.upper())
    or any("idx_email_unique" in (r[0] or "") for r in indices)
    or any("autoindex_canonical_persons" in (r[0] or "") and r[0].endswith("_2")
           for r in indices)
)

check("canonical_id PRIMARY KEY exists (INSERT OR IGNORE fires on PK collision)",
      has_pk, schema)
check(
    "canonical_email has UNIQUE constraint (inline DDL or unique index)",
    has_unique_email,
    f"Indices found: {[r[0] for r in indices]}. "
    "Expected 'canonical_email TEXT UNIQUE' in DDL or idx_email_unique index."
)

# ---------------------------------------------------------------------------
# Test 5: Directly prove the schema gap -- insert two rows with same email
# ---------------------------------------------------------------------------
print("\n--- Test 5: Schema rejects duplicate emails at DB level (UNIQUE enforced)")
db5 = Path(tmpdir) / "t5.db"
EntityResolutionIndex(db_path=db5)
with sqlite3.connect(db5) as conn:
    # INSERT first row — must succeed
    conn.execute(
        "INSERT INTO canonical_persons (canonical_id, canonical_email, display_names)"
        " VALUES ('id-x', 'dup@co.com', '[]')"
    )
    conn.commit()

    # INSERT second row with same email — must raise IntegrityError
    constraint_raised = False
    try:
        conn.execute(
            "INSERT INTO canonical_persons (canonical_id, canonical_email, display_names)"
            " VALUES ('id-y', 'dup@co.com', '[]')"
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        constraint_raised = True
        print(f"  IntegrityError (expected): {exc}")

    count = conn.execute(
        "SELECT COUNT(*) FROM canonical_persons WHERE canonical_email='dup@co.com'"
    ).fetchone()[0]
    print(f"  Rows with same email after attempted duplicate insert: {count}")
    check(
        "Schema rejects duplicate emails at DB level (IntegrityError raised)",
        constraint_raised,
        "Expected sqlite3.IntegrityError on duplicate email insert — "
        "UNIQUE constraint on canonical_email must be enforced by the DB."
    )
    check(
        "Only 1 row exists for that email after attempted duplicate",
        count == 1,
        f"got {count} rows — UNIQUE constraint not enforced at DB level."
    )

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
print()
if failures:
    print(f"RESULT: {len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("RESULT: All checks passed.")
