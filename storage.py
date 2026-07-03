import sqlite3
from contextlib import contextmanager

DB_PATH = "bullpen_bot.db"


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS sent_reports (
                team_id INTEGER,
                report_date TEXT,
                PRIMARY KEY (team_id, report_date)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS sent_edge_alerts (
                report_date TEXT PRIMARY KEY
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS reliever_overrides (
                pitcher_id INTEGER PRIMARY KEY,
                pitcher_name TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS team_coverage (
                team_id INTEGER PRIMARY KEY,
                last_check_date TEXT
            )
        """)


def report_already_sent(team_id: int, report_date: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM sent_reports WHERE team_id = ? AND report_date = ?",
            (team_id, report_date),
        ).fetchone()
        return row is not None


def mark_report_sent(team_id: int, report_date: str):
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO sent_reports (team_id, report_date) VALUES (?, ?)",
            (team_id, report_date),
        )


def edge_alert_already_sent(report_date: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM sent_edge_alerts WHERE report_date = ?", (report_date,)
        ).fetchone()
        return row is not None


def mark_edge_alert_sent(report_date: str):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO sent_edge_alerts (report_date) VALUES (?)", (report_date,))


def set_config(key: str, value: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_config(key: str):
    with _conn() as c:
        row = c.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def add_reliever_override(pitcher_id: int, pitcher_name: str):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO reliever_overrides (pitcher_id, pitcher_name) VALUES (?, ?)",
            (pitcher_id, pitcher_name),
        )


def remove_reliever_override(pitcher_id: int):
    with _conn() as c:
        c.execute("DELETE FROM reliever_overrides WHERE pitcher_id = ?", (pitcher_id,))


def get_reliever_overrides() -> set[int]:
    with _conn() as c:
        rows = c.execute("SELECT pitcher_id FROM reliever_overrides").fetchall()
        return {r["pitcher_id"] for r in rows}


def get_last_check_date(team_id: int):
    with _conn() as c:
        row = c.execute(
            "SELECT last_check_date FROM team_coverage WHERE team_id = ?", (team_id,)
        ).fetchone()
        return row["last_check_date"] if row else None


def set_last_check_date(team_id: int, check_date: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO team_coverage (team_id, last_check_date) VALUES (?, ?) "
            "ON CONFLICT(team_id) DO UPDATE SET last_check_date = excluded.last_check_date",
            (team_id, check_date),
        )
