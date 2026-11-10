"""Some words are reserved by the database and break a column or table with that name.

SQLite accepts nearly anything, so a clash only appears on a real PostgreSQL or MySQL (it happened before in another
project with a column called row_number). This test fails the build if a table or column uses such a word.
"""

from app import models  # noqa: F401  (registers the tables)
from app.db import Base

POSTGRESQL = {
    "ALL", "ANALYSE", "ANALYZE", "AND", "ANY", "ARRAY", "AS", "ASC", "ASYMMETRIC", "BOTH", "CASE", "CAST", "CHECK",
    "COLLATE", "COLUMN", "CONSTRAINT", "CREATE", "CURRENT_CATALOG", "CURRENT_DATE", "CURRENT_ROLE", "CURRENT_TIME",
    "CURRENT_TIMESTAMP", "CURRENT_USER", "DEFAULT", "DEFERRABLE", "DESC", "DISTINCT", "DO", "ELSE", "END", "EXCEPT",
    "FALSE", "FETCH", "FOR", "FOREIGN", "FROM", "GRANT", "GROUP", "HAVING", "IN", "INITIALLY", "INTERSECT", "INTO",
    "LATERAL", "LEADING", "LIMIT", "LOCALTIME", "LOCALTIMESTAMP", "NOT", "NULL", "OFFSET", "ON", "ONLY", "OR", "ORDER",
    "PLACING", "PRIMARY", "REFERENCES", "RETURNING", "SELECT", "SESSION_USER", "SOME", "SYMMETRIC", "SYSTEM_USER",
    "TABLE", "THEN", "TO", "TRAILING", "TRUE", "UNION", "UNIQUE", "USER", "USING", "VARIADIC", "WHEN", "WHERE",
    "WINDOW", "WITH",
}  # fmt: skip

MYSQL = {
    "ADD", "ALL", "ALTER", "AND", "AS", "ASC", "BEFORE", "BY", "CALL", "CASE", "CHECK", "COLUMN", "CONDITION",
    "CONSTRAINT", "CREATE", "CURRENT_USER", "DATABASE", "DEFAULT", "DELETE", "DESC", "DISTINCT", "DROP", "EACH", "ELSE",
    "EXISTS", "EXPLAIN", "FALSE", "FETCH", "FOR", "FOREIGN", "FROM", "GRANT", "GROUP", "HAVING", "IF", "IGNORE", "IN",
    "INDEX", "INSERT", "INTERVAL", "INTO", "IS", "JOIN", "KEY", "KEYS", "KILL", "LIKE", "LIMIT", "LOCK", "LONG", "MATCH",
    "NOT", "NULL", "ON", "OPTION", "OR", "ORDER", "OUT", "OVER", "PRIMARY", "RANK", "READ", "REFERENCES", "RELEASE",
    "RENAME", "REPLACE", "REQUIRE", "RETURN", "REVOKE", "ROW", "ROWS", "ROW_NUMBER", "SCHEMA", "SELECT", "SET", "SHOW",
    "SYSTEM", "TABLE", "THEN", "TO", "TRIGGER", "TRUE", "UNION", "UNIQUE", "UPDATE", "USAGE", "USE", "USING", "VALUES",
    "WHEN", "WHERE", "WINDOW", "WITH", "WRITE",
}  # fmt: skip


def test_no_table_or_column_name_is_a_reserved_word():
    clashes = []
    for table in Base.metadata.tables.values():
        names = [(table.name, f"table {table.name}")] + [(c.name, f"{table.name}.{c.name}") for c in table.columns]
        for name, where in names:
            word = name.upper()
            for database, reserved in (("PostgreSQL", POSTGRESQL), ("MySQL", MYSQL)):
                if word in reserved:
                    clashes.append(f"{where} is reserved in {database}")
    assert not clashes, clashes
