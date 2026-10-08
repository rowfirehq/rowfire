"""Schema reading, draft generation, and validation against the live database.

`init` aims at a definitions file that `validate` passes with no edits, while
being honest about what it cannot know. It infers structure (tables, keys,
foreign keys, semantic types) and refuses to invent meaning: enum values are
emitted as bare numbers with TODO markers, because guessing that status 3
means in_progress is exactly the kind of plausible-but-wrong that the concept
doc's backtest exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .definitions import SemanticType

# Postgres type -> semantic type. Structural inference only.
_TYPE_MAP: dict[str, SemanticType] = {
    "text": SemanticType.string,
    "character varying": SemanticType.string,
    "character": SemanticType.string,
    "citext": SemanticType.string,
    "smallint": SemanticType.integer,
    "integer": SemanticType.integer,
    "bigint": SemanticType.integer,
    "numeric": SemanticType.money,
    "money": SemanticType.money,
    "double precision": SemanticType.integer,
    "real": SemanticType.integer,
    "boolean": SemanticType.boolean,
    "timestamp with time zone": SemanticType.timestamp,
    "timestamp without time zone": SemanticType.timestamp,
    "date": SemanticType.timestamp,
    "uuid": SemanticType.uuid,
    "json": SemanticType.json,
    "jsonb": SemanticType.json,
    # MySQL, by information_schema DATA_TYPE. Floats map to integer for the
    # same reason Postgres's do: "a number", as far as a template cares.
    "varchar": SemanticType.string,
    "char": SemanticType.string,
    "tinytext": SemanticType.string,
    "mediumtext": SemanticType.string,
    "longtext": SemanticType.string,
    "enum": SemanticType.string,
    "set": SemanticType.string,
    "tinyint": SemanticType.integer,
    "mediumint": SemanticType.integer,
    "int": SemanticType.integer,
    "decimal": SemanticType.money,
    "float": SemanticType.integer,
    "double": SemanticType.integer,
    "datetime": SemanticType.timestamp,
    "timestamp": SemanticType.timestamp,
    # tinyint(1) is how MySQL spells boolean; read_schema reports it so.
    "tinyint(1)": SemanticType.boolean,
}

# Name-based refinements, applied only to string columns. A column called
# `phone` is a phone number in every schema anyone has ever shipped.
_NAME_HINTS: list[tuple[tuple[str, ...], SemanticType]] = [
    (("phone", "mobile", "msisdn", "whatsapp"), SemanticType.phone),
    (("email", "e_mail"), SemanticType.email),
    (("url", "link", "website", "avatar"), SemanticType.url),
]

# Columns that plausibly place a row in time, best first.
_EVENT_TIME_PREFERENCES = (
    "completed_at",
    "finished_at",
    "closed_at",
    "delivered_at",
    "paid_at",
    "activated_at",
    "confirmed_at",
    "started_at",
    "updated_at",
    "created_at",
)


@dataclass
class Column:
    name: str
    pg_type: str
    nullable: bool


@dataclass
class ForeignKey:
    column: str
    target_table: str
    target_column: str


@dataclass
class Table:
    name: str
    schema: str
    columns: list[Column] = field(default_factory=list)
    primary_key: str | None = None
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}


def read_schema(conn: Any, schema: str | None = None) -> dict[str, Table]:
    """Read tables, columns, single-column primary keys and foreign keys.

    `schema` defaults to where the source keeps its tables: `public` on
    Postgres, the connected database on MySQL.
    """
    if schema is None:
        schema = conn.default_schema()
    if conn.kind == "mysql":
        return _read_mysql_schema(conn, schema)
    return _read_postgres_schema(conn, schema)


def _read_postgres_schema(conn: Any, schema: str) -> dict[str, Table]:
    """Postgres: the catalogue, read directly.

    Reads pg_catalog rather than information_schema, and that is not a style
    preference. information_schema.table_constraints only exposes constraints
    on tables the current role owns or holds a non-SELECT privilege on, so a
    SELECT-only role -- exactly the credential a customer hands over -- sees
    zero primary keys and zero foreign keys through it. `init` would then
    produce a definitions file with no entities at all.

    pg_catalog applies no such filter.
    """
    tables: dict[str, Table] = {}

    with conn.cursor() as cur:
        # relkind: r = table, p = partitioned, v = view, m = materialised view,
        # f = foreign table. Views are included because plenty of production
        # schemas expose their clean shape that way.
        cur.execute(
            """
            SELECT c.relname AS table_name,
                   a.attname AS column_name,
                   format_type(a.atttypid, a.atttypmod) AS data_type,
                   NOT a.attnotnull AS nullable
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE n.nspname = %s
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY c.relname, a.attnum
            """,
            (schema,),
        )
        for row in cur.fetchall():
            table = tables.setdefault(
                row["table_name"], Table(name=row["table_name"], schema=schema)
            )
            table.columns.append(
                Column(
                    name=row["column_name"],
                    pg_type=_base_type(row["data_type"]),
                    nullable=row["nullable"],
                )
            )

        # Single-column primary keys only. A composite-key table is skipped
        # rather than half-modelled -- dedup and v1 enrichment both assume one.
        cur.execute(
            """
            SELECT c.relname AS table_name,
                   a.attname AS column_name,
                   i.indnatts AS key_columns
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
            WHERE i.indisprimary AND n.nspname = %s
            """,
            (schema,),
        )
        for row in cur.fetchall():
            if row["key_columns"] == 1 and row["table_name"] in tables:
                tables[row["table_name"]].primary_key = row["column_name"]

        cur.execute(
            """
            SELECT c.relname  AS table_name,
                   a.attname  AS column_name,
                   tc.relname AS target_table,
                   ta.attname AS target_column
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_class tc ON tc.oid = con.confrelid
            JOIN pg_attribute a
              ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
            JOIN pg_attribute ta
              ON ta.attrelid = con.confrelid AND ta.attnum = con.confkey[1]
            WHERE con.contype = 'f'
              AND n.nspname = %s
              AND array_length(con.conkey, 1) = 1
            """,
            (schema,),
        )
        for row in cur.fetchall():
            if row["table_name"] in tables:
                tables[row["table_name"]].foreign_keys.append(
                    ForeignKey(
                        column=row["column_name"],
                        target_table=row["target_table"],
                        target_column=row["target_column"],
                    )
                )

    conn.rollback()
    return tables


def _read_mysql_schema(conn: Any, schema: str) -> dict[str, Table]:
    """MySQL: information_schema, which on MySQL does show a SELECT-only role
    the keys of the tables it can read -- unlike Postgres's.
    """
    tables: dict[str, Table] = {}

    rows, _ = conn.fetch(
        """
        SELECT c.TABLE_NAME AS table_name,
               c.COLUMN_NAME AS column_name,
               c.DATA_TYPE AS data_type,
               c.COLUMN_TYPE AS column_type,
               c.IS_NULLABLE AS nullable
        FROM information_schema.COLUMNS c
        JOIN information_schema.TABLES t
          ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
        WHERE c.TABLE_SCHEMA = %(schema)s
          AND t.TABLE_TYPE IN ('BASE TABLE', 'VIEW')
        ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
        """,
        {"schema": schema},
    )
    for row in rows:
        table = tables.setdefault(row["table_name"], Table(name=row["table_name"], schema=schema))
        data_type = str(row["data_type"]).lower()
        if str(row["column_type"]).lower().startswith("tinyint(1)"):
            data_type = "tinyint(1)"
        table.columns.append(
            Column(name=row["column_name"], pg_type=data_type, nullable=row["nullable"] == "YES")
        )

    rows, _ = conn.fetch(
        """
        SELECT k.TABLE_NAME AS table_name,
               k.COLUMN_NAME AS column_name,
               k.REFERENCED_TABLE_NAME AS target_table,
               k.REFERENCED_COLUMN_NAME AS target_column,
               k.CONSTRAINT_NAME AS constraint_name,
               (SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE k2
                 WHERE k2.TABLE_SCHEMA = k.TABLE_SCHEMA
                   AND k2.TABLE_NAME = k.TABLE_NAME
                   AND k2.CONSTRAINT_NAME = k.CONSTRAINT_NAME) AS key_columns
        FROM information_schema.KEY_COLUMN_USAGE k
        WHERE k.TABLE_SCHEMA = %(schema)s
        """,
        {"schema": schema},
    )
    for row in rows:
        table = tables.get(row["table_name"])
        # Single-column keys only, for the same reason as on Postgres.
        if table is None or int(row["key_columns"]) != 1:
            continue
        if row["constraint_name"] == "PRIMARY":
            table.primary_key = row["column_name"]
        elif row["target_table"]:
            table.foreign_keys.append(
                ForeignKey(
                    column=row["column_name"],
                    target_table=row["target_table"],
                    target_column=row["target_column"],
                )
            )
    return tables


def _base_type(formatted: str) -> str:
    """Strip modifiers and array markers: `numeric(10,2)` -> `numeric`.

    format_type() is more precise than information_schema.data_type, which is
    what the semantic type map was written against.
    """
    return formatted.split("(")[0].replace("[]", "").strip()


def semantic_type(column: Column) -> SemanticType:
    base = _TYPE_MAP.get(column.pg_type, SemanticType.unknown)
    if base is SemanticType.string:
        lowered = column.name.lower()
        for needles, hinted in _NAME_HINTS:
            if any(needle in lowered for needle in needles):
                return hinted
    return base


# Postgres type OID -> semantic type. The authority on what a trigger returns
# is the query itself, so output types are read from the cursor rather than
# from a model someone has to keep in step with the schema.
_OID_MAP: dict[int, SemanticType] = {
    16: SemanticType.boolean,
    20: SemanticType.integer,  # int8
    21: SemanticType.integer,  # int2
    23: SemanticType.integer,  # int4
    700: SemanticType.integer,  # float4
    701: SemanticType.integer,  # float8
    790: SemanticType.money,
    1700: SemanticType.money,  # numeric
    1082: SemanticType.timestamp,  # date
    1114: SemanticType.timestamp,  # timestamp
    1184: SemanticType.timestamp,  # timestamptz
    25: SemanticType.string,  # text
    1042: SemanticType.string,  # bpchar
    1043: SemanticType.string,  # varchar
    2950: SemanticType.uuid,
    114: SemanticType.json,
    3802: SemanticType.json,  # jsonb
}


# MySQL protocol field type -> semantic type (pymysql.constants.FIELD_TYPE).
# TEXT columns arrive as BLOB types; a binary BLOB is rare in an event query
# and reads as a string to a template either way.
_MYSQL_FIELD_MAP: dict[int, SemanticType] = {
    0: SemanticType.money,  # DECIMAL
    246: SemanticType.money,  # NEWDECIMAL
    1: SemanticType.integer,  # TINY (boolean when its display width is 1)
    2: SemanticType.integer,  # SHORT
    3: SemanticType.integer,  # LONG
    8: SemanticType.integer,  # LONGLONG
    9: SemanticType.integer,  # INT24
    13: SemanticType.integer,  # YEAR
    4: SemanticType.integer,  # FLOAT
    5: SemanticType.integer,  # DOUBLE
    7: SemanticType.timestamp,  # TIMESTAMP
    10: SemanticType.timestamp,  # DATE
    12: SemanticType.timestamp,  # DATETIME
    15: SemanticType.string,  # VARCHAR
    253: SemanticType.string,  # VAR_STRING
    254: SemanticType.string,  # STRING
    247: SemanticType.string,  # ENUM
    249: SemanticType.string,  # TINY_BLOB
    250: SemanticType.string,  # MEDIUM_BLOB
    251: SemanticType.string,  # LONG_BLOB
    252: SemanticType.string,  # BLOB
    245: SemanticType.json,  # JSON
}


def semantic_from_cursor(description: Any, engine: str = "postgres") -> dict[str, str]:
    """Map a cursor description to semantic types, keyed by column name.

    `engine` says whose description it is: Postgres reports type OIDs, MySQL
    protocol field types. Name hints refine the result the same way they do
    for schema reading: a column called `phone` is a phone number in every
    schema anyone has shipped, and an `id` is a label that happens to be
    numeric rather than a quantity anyone would sum.
    """
    out: dict[str, str] = {}
    for column in description or ():
        if engine == "mysql":
            # A DB-API 7-tuple. PyMySQL leaves display_size (index 2) empty
            # and reports the column's width as internal_size (index 3).
            name, type_code, width = column[0], column[1], column[3]
            kind = _MYSQL_FIELD_MAP.get(type_code, SemanticType.unknown)
            if type_code == 1 and width == 1:
                kind = SemanticType.boolean  # tinyint(1)
        else:
            name = column.name
            kind = _OID_MAP.get(column.type_code, SemanticType.unknown)
        lowered = name.lower()

        if kind is SemanticType.string:
            for needles, hinted in _NAME_HINTS:
                if any(needle in lowered for needle in needles):
                    kind = hinted
                    break
        elif kind is SemanticType.integer and (lowered == "id" or lowered.endswith("_id")):
            kind = SemanticType.identifier

        out[name] = kind.value
    return out


def timestamp_columns(table: Table) -> list[str]:
    """Every column a trigger could plausibly use as its event_time.

    Offered as candidates, never applied as a default: the choice belongs to
    the trigger, because one table has many events and each has its own clock.
    """
    stamps = [c.name for c in table.columns if _TYPE_MAP.get(c.pg_type) is SemanticType.timestamp]
    preferred = [c for c in _EVENT_TIME_PREFERENCES if c in stamps]
    return preferred + [c for c in stamps if c not in preferred]


def guess_event_time(table: Table) -> str | None:
    names = {c.name for c in table.columns if _TYPE_MAP.get(c.pg_type) is SemanticType.timestamp}
    for candidate in _EVENT_TIME_PREFERENCES:
        if candidate in names:
            return candidate
    return None


def looks_like_enum(column: Column) -> bool:
    """Integer status-ish columns are almost always encoded enums."""
    if _TYPE_MAP.get(column.pg_type) is not SemanticType.integer:
        return False
    lowered = column.name.lower()
    return any(k in lowered for k in ("status", "state", "type", "kind", "reason"))


def _singular(table_name: str) -> str:
    """orders -> order. Naive depluralisation, good enough for a draft."""
    if table_name.endswith("ies"):
        return table_name[:-3] + "y"
    if table_name.endswith("ses") or table_name.endswith("xes"):
        return table_name[:-2]
    if table_name.endswith("s") and not table_name.endswith("ss"):
        return table_name[:-1]
    return table_name


def draft_yaml(
    tables: dict[str, Table], dsn_env: str = "DATABASE_URL", kind: str = "postgres"
) -> str:
    """Render a starter definitions file.

    The schema goes in as comments rather than as an entity model: triggers
    are SQL now, so what an author needs is the column list in front of them,
    not a parallel description to keep in step.
    """
    lines: list[str] = [
        "# Generated by `rowfire init`. Review before use.",
        "#",
        "# A trigger is a SELECT describing when something happened. It may join",
        "# whatever it needs -- a joined column is just a column, which is why",
        "# there is no separate enrichment step.",
        "#",
        "# `key` is the grain: which output columns identify one row. Only the",
        "# person writing the query reliably knows this, which is why it lives",
        "# on the trigger rather than on the rules that use it.",
        "version: 2",
        "",
        "source:",
        f"  type: {kind}",
        f"  dsn_env: {dsn_env}          # read the DSN from this env var, never inline it",
        "  statement_timeout_ms: 30000",
        "  max_rows: 50000",
        "",
        "# ---------------------------------------------------------------- schema",
        "# Reference only. Nothing below this needs maintaining.",
    ]

    for table in sorted(tables.values(), key=lambda t: t.name):
        key = f"pk {table.primary_key}" if table.primary_key else "no single-column pk"
        lines.append("#")
        lines.append(f"#   {table.name}  ({key})")
        for column in table.columns:
            kind = semantic_type(column).value
            lines.append(f"#     {column.name:<22} {column.pg_type:<26} {kind}")
        for fk in table.foreign_keys:
            lines.append(f"#     -> {fk.column} references {fk.target_table}.{fk.target_column}")

    drafted = _draft_triggers(tables)
    lines += ["", "triggers:"]
    if drafted:
        lines += [line for line in _render_triggers(drafted).splitlines()]
    else:
        lines[-1] = "triggers: {}"
        lines += [
            "# No table here has both a single-column primary key and a timestamp,",
            "# so there is nothing safe to draft. Write the query you want:",
            "#",
            "#   something_happened:",
            "#     sql: SELECT id, created_at FROM your_table WHERE ...",
            "#     event_time: created_at",
            "#     key: [id]",
        ]

    lines += ["", "rules:"]
    if drafted:
        first = drafted[0][0]
        lines += [
            f"  {first}_once:",
            f"    trigger: {first}",
            "    description: One message the first time each row appears",
            "    policy: once_ever        # once_ever | once_per_period | once_per_n",
        ]
    else:
        lines[-1] = "rules: {}"

    return "\n".join(lines) + "\n"


def _draft_triggers(tables: dict[str, Table]) -> list[tuple[str, Table, str]]:
    """One runnable trigger per table that can support one.

    A table qualifies when it has a single-column primary key (the grain) and a
    timestamp (the clock). Both are facts about the schema, not guesses about
    meaning -- which is the line this draft stays on. No WHERE clause is
    invented: `status = 3` would look right and fire wrong, and the enum labels
    are left as TODO for the same reason.
    """
    drafted: list[tuple[str, Table, str]] = []
    for table in sorted(tables.values(), key=lambda t: t.name):
        clock = guess_event_time(table)
        if not table.primary_key or not clock:
            continue
        drafted.append((f"{_singular(table.name)}_row", table, clock))
    return drafted


def _render_triggers(drafted: list[tuple[str, Table, str]]) -> str:
    """Render the drafted triggers as YAML.

    Each one runs as written, which is the point: `init` then `run` should
    produce a number without an edit in between. Each is also deliberately
    incomplete in a visible way -- no WHERE means every row, and the comment
    says so, rather than a guessed condition that reads as finished.
    """
    blocks: list[str] = []
    for name, table, clock in drafted:
        columns = _draft_columns(table, clock)
        blocks.append(
            f"  {name}:\n"
            f"    description: Every row in {table.name}. Narrow this with a WHERE clause.\n"
            f"    sql: |\n"
            f"      SELECT {', '.join(columns)}\n"
            f"      FROM {table.name}\n"
            f"      -- WHERE <your condition>\n"
            f"    event_time: {clock}\n"
            f"    key: [{table.primary_key}]\n"
        )
    return "\n".join(blocks).rstrip("\n")


def _draft_columns(table: Table, clock: str) -> list[str]:
    """The primary key, the clock, and a few columns worth seeing.

    Not `*`: the output columns are what templates and keys reference, so an
    explicit list is what makes the draft readable. Foreign keys come first
    because they are how the query gets joined later.
    """
    chosen: list[str] = [c for c in (table.primary_key, clock) if c]
    for fk in table.foreign_keys:
        if fk.column not in chosen:
            chosen.append(fk.column)
    for column in table.columns:
        if len(chosen) >= 6:
            break
        if column.name not in chosen:
            chosen.append(column.name)
    return chosen


def describe_trigger(conn: Any, trigger: Any) -> dict[str, str]:
    """Run a trigger's query with LIMIT 0 to learn its output columns.

    Cheap, exact, and it validates the SQL against the real schema on the way:
    a typo in a column name fails here rather than at 3am. Parsed in the
    connection's own dialect.
    """
    from .compile import describe_sql

    compiled = describe_sql(trigger, conn.dialect)
    _, columns = conn.fetch(compiled.sql, dict(compiled.params))
    return columns


def validate_definitions(definitions: Any, conn: Any, only: Any = None) -> list[str]:
    """Check every trigger's query actually runs, reporting all failures.

    Also checks that the columns a trigger names -- its clock and its key --
    are columns the query really returns. `only` limits the check to the
    triggers that read this connection's source.
    """
    errors: list[str] = []
    for name, trigger in definitions.triggers.items():
        if only is not None and name not in only:
            continue
        try:
            columns = describe_trigger(conn, trigger)
        except Exception as exc:  # noqa: BLE001 -- surfaced, not raised
            errors.append(f"trigger `{name}`: {str(exc).strip().splitlines()[0][:200]}")
            continue

        if trigger.event_time and trigger.event_time not in columns:
            errors.append(
                f"trigger `{name}`: event_time `{trigger.event_time}` is not a "
                f"column this query returns ({', '.join(columns) or 'none'})"
            )
        for column in trigger.key:
            if column not in columns:
                errors.append(
                    f"trigger `{name}`: key column `{column}` is not a column this "
                    f"query returns ({', '.join(columns) or 'none'})"
                )
    return errors
