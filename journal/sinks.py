"""The four journal sinks, on SQLite.

TWO STRUCTURAL RULES, both of which exist to make the record complete by
construction rather than by anyone's diligence.

RULE 1 - THERE IS NO REASON ALLOWLIST. `record_reject` writes whatever
RejectReason it is handed. The alternative - a hand-maintained list of "reasons we
record" - always falls behind the gates themselves, and the reasons it omits become
INVISIBLE rather than under-reported, so nobody notices they are missing. Since
rejection is a typed value in this system (setups/base.py) and journaling is the
only thing done with one, a gate added later is observable the day it is written.

RULE 2 - EVERY ROW CARRIES THE PROFILE SNAPSHOT IT WAS DECIDED FROM, as raw
measurements rather than pass/fail booleans. A stored threshold comparison cannot be
re-swept; a stored value can. This is what makes Phase 3's ablation a query over
existing rows instead of a re-run, and it is the difference between a journal that
is an audit log and one that is a research substrate.

Every row also carries `config_hash`, `code_version` and `session_id`. Without them,
a performance figure spanning a parameter change is an average over two different
systems, and the only honest aggregation is GROUP BY config_hash.

SCHEMA EVOLUTION. Attribute sets differ per setup and will grow, so the variable
part of each row is stored as JSON in an `attrs` column rather than as columns that
would need a migration per new measurement. The fixed, queried-on fields are real
columns and indexed.
"""
import json
import logging
import os
import sqlite3
import subprocess
import threading

import config
import sessions

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS setup_journal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    session_id    TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    setup         TEXT    NOT NULL,
    direction     TEXT,
    state         TEXT    NOT NULL,
    level_kind    TEXT,
    level_price   REAL,
    entry_price   REAL,
    stop_price    REAL,
    target_price  REAL,
    risk_distance REAL,
    r_multiple    REAL,
    cost_r        REAL,
    net_r         REAL,
    quantity      REAL,
    notional      REAL,
    atr           REAL,
    as_of         INTEGER,
    config_hash   TEXT,
    code_version  TEXT,
    attrs         TEXT,
    profile_snap  TEXT
);
CREATE INDEX IF NOT EXISTS ix_setup_session ON setup_journal(session_id, symbol);
CREATE INDEX IF NOT EXISTS ix_setup_setup   ON setup_journal(setup, state);

CREATE TABLE IF NOT EXISTS reject_journal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    session_id    TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    setup         TEXT    NOT NULL,
    direction     TEXT,
    reason        TEXT    NOT NULL,
    detail        TEXT,
    level_price   REAL,
    config_hash   TEXT,
    code_version  TEXT,
    context       TEXT,
    profile_snap  TEXT
);
CREATE INDEX IF NOT EXISTS ix_reject_reason  ON reject_journal(reason, setup);
CREATE INDEX IF NOT EXISTS ix_reject_session ON reject_journal(session_id);

CREATE TABLE IF NOT EXISTS trade_journal (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at      INTEGER NOT NULL,
    closed_at      INTEGER,
    session_id     TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    setup          TEXT    NOT NULL,
    direction      TEXT    NOT NULL,
    entry_price    REAL,
    stop_price     REAL,
    target_price   REAL,
    quantity       REAL,
    exit_price     REAL,
    exit_reason    TEXT,
    gross_r        REAL,
    net_r          REAL,
    commission     REAL,
    pnl            REAL,
    hours_held     REAL,
    intended_entry REAL,
    slippage_bps   REAL,
    client_id      TEXT,
    config_hash    TEXT,
    code_version   TEXT,
    attrs          TEXT
);
CREATE INDEX IF NOT EXISTS ix_trade_session ON trade_journal(session_id);
CREATE INDEX IF NOT EXISTS ix_trade_open    ON trade_journal(symbol, closed_at);

CREATE TABLE IF NOT EXISTS profile_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL,
    session_id    TEXT    NOT NULL,
    window_start  INTEGER NOT NULL,
    window_end    INTEGER NOT NULL,
    as_of         INTEGER NOT NULL,
    bin_size      REAL,
    poc_price     REAL,
    vah           REAL,
    val           REAL,
    vwap          REAL,
    high          REAL,
    low           REAL,
    total_volume  REAL,
    quote_volume  REAL,
    candle_count  INTEGER,
    bin_count     INTEGER,
    shape         TEXT,
    levels        TEXT,
    histogram     TEXT,
    config_hash   TEXT,
    UNIQUE(symbol, session_id, window_start, as_of)
);
CREATE INDEX IF NOT EXISTS ix_snap_symbol ON profile_snapshots(symbol, session_id);
"""


def _code_version():
    """Short git SHA, so a row can be tied to the code that produced it."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:                             # noqa: BLE001
        pass
    return "unknown"


class Journal:
    """Writer for all four sinks. Thread-safe, single connection.

    SQLite in WAL mode with a single writer is more than adequate here - the write
    rate is a few rows per symbol per cycle - and it removes an entire class of
    operational problem compared with a server database.
    """

    def __init__(self, path=None):
        self._path = path or config.DB_PATH
        self._lock = threading.RLock()
        self._config_hash = config.config_hash()
        self._code_version = _code_version()

        directory = os.path.dirname(os.path.abspath(self._path))
        if directory:
            os.makedirs(directory, exist_ok=True)

        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.commit()
            self._conn.close()

    def _stamp(self):
        return self._config_hash, self._code_version

    # ------------------------------------------------------------- setups

    def record_setup(self, candidate):
        """One row per candidate STATE TRANSITION, not one per candidate.

        Appending rather than updating keeps the whole lifecycle - armed, working,
        filled, expired - which is what makes the funnel measurable. An UPDATE would
        leave only the final state and discard how it got there.
        """
        config_hash, code_version = self._stamp()
        row = candidate.as_row()
        with self._lock:
            self._conn.execute(
                """INSERT INTO setup_journal (
                    ts, session_id, symbol, setup, direction, state, level_kind,
                    level_price, entry_price, stop_price, target_price,
                    risk_distance, r_multiple, cost_r, net_r, quantity, notional,
                    atr, as_of, config_hash, code_version, attrs, profile_snap
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sessions.now_ms(), candidate.session_id, candidate.symbol,
                    candidate.setup, candidate.direction, candidate.state.value,
                    candidate.level_kind, candidate.level_price,
                    candidate.entry_price, candidate.stop_price,
                    candidate.target_price, candidate.risk_distance,
                    candidate.r_multiple, candidate.round_trip_cost_r(),
                    candidate.net_r_at_target(), candidate.quantity,
                    candidate.notional, candidate.atr, candidate.as_of,
                    config_hash, code_version,
                    json.dumps(candidate.attributes, default=str),
                    json.dumps(candidate.profile_snapshot, default=str),
                ),
            )
            self._conn.commit()

    # ------------------------------------------------------------ rejects

    def record_reject(self, rejection, session_id="", profile_snapshot=None):
        """Write a rejection. NO ALLOWLIST - whatever reason arrives is recorded.

        This is the method that makes a new gate observable the day it is written.
        There is deliberately no filtering, sampling, or reason whitelist here; the
        volume is manageable because rejects are per candidate, not per tick.
        """
        config_hash, code_version = self._stamp()
        with self._lock:
            self._conn.execute(
                """INSERT INTO reject_journal (
                    ts, session_id, symbol, setup, direction, reason, detail,
                    level_price, config_hash, code_version, context, profile_snap
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sessions.now_ms(), session_id, rejection.symbol,
                    rejection.setup, rejection.direction,
                    str(rejection.reason.value), rejection.detail,
                    rejection.level_price, config_hash, code_version,
                    json.dumps(rejection.context, default=str),
                    json.dumps(profile_snapshot or {}, default=str),
                ),
            )
            self._conn.commit()

    def record_rejects(self, rejections, session_id="", profile_snapshot=None):
        for rejection in rejections:
            self.record_reject(rejection, session_id=session_id,
                               profile_snapshot=profile_snapshot)

    # ------------------------------------------------------------- trades

    def record_trade_open(self, managed, candidate=None):
        """Open a trade row. Slippage against the intended entry is recorded here.

        Intended-vs-actual is kept because it is the only way to calibrate the
        slippage model used in replay. Without it, Phase 5's live-vs-replay
        reconciliation has nothing to reconcile against.
        """
        config_hash, code_version = self._stamp()
        intended = (candidate.entry_price if candidate is not None
                    else managed.entry_price)
        slippage_bps = 0.0
        if intended > 0:
            direction_sign = 1.0 if managed.direction == "BUY" else -1.0
            slippage_bps = ((managed.entry_price - intended) / intended
                            * 10_000.0 * direction_sign)

        with self._lock:
            self._conn.execute(
                """INSERT INTO trade_journal (
                    opened_at, session_id, symbol, setup, direction, entry_price,
                    stop_price, target_price, quantity, intended_entry,
                    slippage_bps, client_id, config_hash, code_version, attrs
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    managed.opened_at, managed.session_id, managed.symbol,
                    managed.setup, managed.direction, managed.entry_price,
                    managed.stop_price, managed.target_price, managed.quantity,
                    intended, slippage_bps, managed.client_order_id,
                    config_hash, code_version,
                    json.dumps(managed.attributes, default=str),
                ),
            )
            self._conn.commit()

    def record_trade_close(self, managed, outcome):
        """Complete the open row for this position, most recent first."""
        with self._lock:
            self._conn.execute(
                """UPDATE trade_journal SET
                    closed_at=?, exit_price=?, exit_reason=?, gross_r=?, net_r=?,
                    commission=?, pnl=?, hours_held=?
                   WHERE id = (
                     SELECT id FROM trade_journal
                     WHERE symbol=? AND closed_at IS NULL
                     ORDER BY opened_at DESC, id DESC LIMIT 1
                   )""",
                (
                    outcome.get("closed_at"), outcome.get("exit_price"),
                    outcome.get("exit_reason"), outcome.get("gross_r"),
                    outcome.get("net_r"), outcome.get("commission"),
                    outcome.get("pnl"), outcome.get("bars_held"),
                    managed.symbol,
                ),
            )
            self._conn.commit()

    # --------------------------------------------------------- profile snaps

    def recent_profile_pocs(self, symbol, limit=30):
        """POC and session extremes for recent sessions, newest first.

        The read side of the naked-POC registry. A closed session's POC never changes,
        so these rows are a permanent record rather than a cache, and reading them costs
        nothing compared with rebuilding ten profiles from 1m candles.

        DISTINCT per session because `profile_snapshots` keys on
        (symbol, session_id, window_start, as_of) and so holds one row per as_of a
        profile was recorded at - several per session during a normal day. The newest
        as_of for a session is the complete one; an earlier row is a partial session and
        its POC would be a different, thinner level. GROUP BY with MAX(as_of) picks the
        complete row rather than whichever the index happened to return first.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT symbol, session_id, poc_price, high, low, MAX(as_of) AS as_of
                     FROM profile_snapshots
                    WHERE symbol = ? AND poc_price IS NOT NULL
                 GROUP BY session_id
                 ORDER BY session_id DESC
                    LIMIT ?""",
                (symbol.upper(), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_profile(self, profile, levels, shape=None, session_id=""):
        """Persist a frozen profile, histogram included.

        The full histogram is stored so any past decision can be recomputed exactly -
        including under a different bin width or value-area percentage, which is what
        Phase 4's robustness sweep needs. It is a few kilobytes per symbol-session.
        """
        if levels is None:
            return
        config_hash, _ = self._stamp()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO profile_snapshots (
                    symbol, session_id, window_start, window_end, as_of, bin_size,
                    poc_price, vah, val, vwap, high, low, total_volume,
                    quote_volume, candle_count, bin_count, shape, levels,
                    histogram, config_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    profile.symbol, session_id, profile.window_start,
                    profile.window_end, profile.as_of, profile.bin_size,
                    levels.poc_price, levels.vah, levels.val, levels.vwap,
                    profile.high, profile.low, profile.total_volume,
                    profile.total_quote_volume, profile.candle_count,
                    profile.bin_count,
                    json.dumps(shape.as_row(), default=str) if shape else None,
                    json.dumps({
                        "hvns": [(n.low_price, n.high_price, n.volume_pct_of_poc)
                                 for n in levels.hvns],
                        "lvns": [(n.low_price, n.high_price, n.volume_pct_of_poc)
                                 for n in levels.lvns],
                        "value_fraction": levels.value_fraction,
                        "poc_prominence": levels.poc_prominence,
                        "delta_poc": levels.delta_poc_price,
                    }, default=str),
                    json.dumps(profile.histogram(), default=str),
                    config_hash,
                ),
            )
            self._conn.commit()

    # ------------------------------------------------------------ read-back

    def stats_today(self):
        """Realised R and the loss streak for today - risk.py's daily limits.

        Read from the journal rather than accumulated in memory, so a restart cannot
        reset a loss limit. That failure would be silent and would remove the one
        control meant to stop a bad day compounding.
        """
        session_id = sessions.session_id(sessions.now_ms())
        with self._lock:
            realised = self._conn.execute(
                """SELECT COALESCE(SUM(net_r), 0) AS total FROM trade_journal
                   WHERE session_id=? AND closed_at IS NOT NULL""",
                (session_id,),
            ).fetchone()["total"]

            # ORDER BY closed_at ALONE IS NOT DETERMINISTIC, and the consequence lands
            # on a risk control. closed_at is a millisecond stamp, and reconcile()
            # settles every finished position in ONE pass - so a volatile minute that
            # stops out three positions writes three identical closed_at values, and
            # SQLite may return them in any order. The consecutive-loss streak then
            # depends on that order: loss/loss/win read as win/loss/loss gives a streak
            # of 2 instead of 0, or the reverse, and CONSECUTIVE_LOSS_LIMIT either fires
            # early or never. `id` is AUTOINCREMENT, so it breaks the tie by true
            # insertion order.
            recent = self._conn.execute(
                """SELECT net_r FROM trade_journal
                   WHERE closed_at IS NOT NULL
                   ORDER BY closed_at DESC, id DESC LIMIT 20"""
            ).fetchall()

        # A trade whose exit could not be read has net_r NULL. It must NOT break the
        # streak: `(net_r or 0.0) < 0` turns NULL into 0.0, which reads as a
        # non-loss and RESETS the consecutive-loss counter - the optimistic reading,
        # inside the one control meant to stop a bad day compounding. Three losses,
        # an unreadable exit, then more losses would never reach the limit.
        #
        # Skipping is the honest middle: an unknown outcome neither confirms the
        # streak nor denies it. Counting it as a loss would be the other error,
        # halting trading on missing data rather than on evidence.
        streak = 0
        for row in recent:
            value = row["net_r"]
            if value is None:
                continue
            if value < 0:
                streak += 1
            else:
                break

        return {"realised_r_today": float(realised or 0.0),
                "consecutive_losses": streak}

    def reject_tally(self, session_id=None, limit=40):
        """Reject counts by reason, for the heartbeat.

        Absolute counters, not deltas since the last heartbeat. A delta forces a
        summation across every log line to answer "how many since restart", which
        makes the simplest operational question unnecessarily hard.
        """
        session_id = session_id or sessions.session_id(sessions.now_ms())
        with self._lock:
            rows = self._conn.execute(
                """SELECT reason, setup, COUNT(*) AS n FROM reject_journal
                   WHERE session_id=? GROUP BY reason, setup
                   ORDER BY n DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        return [(row["reason"], row["setup"], row["n"]) for row in rows]

    def open_trades(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trade_journal WHERE closed_at IS NULL"
            ).fetchall()
        return [dict(row) for row in rows]
