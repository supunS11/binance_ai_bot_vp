"""Operational support: logging, the kill switch, heartbeat, and crash recovery.

THE KILL SWITCH IS A FILE, not a config flag. A flag needs a restart to change, and a
restart is the last thing anyone wants during the situation that makes them reach for
a kill switch. Touching a file stops new entries on the next cycle, from any shell,
with no deploy.

It stops NEW ENTRIES and leaves existing positions managed. Halting reconciliation
too would strand open positions with no one watching their stops, which is worse than
whatever prompted the switch.
"""
import logging
import logging.handlers
import os
import sqlite3
import threading

import config
import sessions
from execution import router as router_mod

log = logging.getLogger(__name__)


def setup_logging(path=None, level=None):
    """Rotating file log plus console, with UTC timestamps.

    UTC in the log because every other timestamp in this system is UTC - a log in
    local time would be the one place a reader has to convert, which is exactly where
    mistakes happen when comparing a log line against a session boundary.
    """
    path = path or config.LOG_PATH
    level = getattr(logging, (level or config.LOG_LEVEL).upper(), logging.INFO)

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03dZ %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    formatter.converter = __import__("time").gmtime

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root


class KillSwitch:
    """File-presence kill switch, polled each cycle."""

    def __init__(self, path=None):
        self._path = path or config.KILL_SWITCH_FILE

    @property
    def engaged(self):
        return os.path.exists(self._path)

    def reason(self):
        if not self.engaged:
            return ""
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                return handle.read().strip()[:200] or "no reason given"
        except OSError:
            return "unreadable"


class StateStore:
    """Persisted runtime state, so a restart resumes rather than starts blind.

    WHAT IS PERSISTED AND WHY. Open positions this bot opened, with the ORIGINAL stop
    and target and the setup that produced them. Without this, a restart sees its own
    positions as unmanaged - and PositionManager deliberately refuses to touch an
    unmanaged position, because closing what might be someone else's trade is worse
    than leaving it. So the persisted row is what lets recovery distinguish "mine,
    resume managing it" from "not mine, report and leave alone".
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS managed_positions (
        symbol       TEXT PRIMARY KEY,
        setup        TEXT NOT NULL,
        direction    TEXT NOT NULL,
        session_id   TEXT,
        entry_price  REAL,
        stop_price   REAL,
        target_price REAL,
        quantity     REAL,
        opened_at    INTEGER,
        client_id    TEXT,
        attrs        TEXT
    );
    """

    def __init__(self, path=None):
        self._path = path or config.DB_PATH
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(self._SCHEMA)
            self._conn.commit()

    def save_position(self, managed):
        import json
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO managed_positions
                   (symbol, setup, direction, session_id, entry_price, stop_price,
                    target_price, quantity, opened_at, client_id, attrs)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (managed.symbol, managed.setup, managed.direction,
                 managed.session_id, managed.entry_price, managed.stop_price,
                 managed.target_price, managed.quantity, managed.opened_at,
                 managed.client_order_id,
                 json.dumps(managed.attributes, default=str)),
            )
            self._conn.commit()

    def drop_position(self, symbol):
        with self._lock:
            self._conn.execute("DELETE FROM managed_positions WHERE symbol=?",
                               (symbol.upper(),))
            self._conn.commit()

    def load_positions(self):
        import json
        from execution.positions import ManagedPosition
        with self._lock:
            rows = self._conn.execute("SELECT * FROM managed_positions").fetchall()
        out = []
        for row in rows:
            try:
                attrs = json.loads(row["attrs"] or "{}")
            except ValueError:
                attrs = {}
            out.append(ManagedPosition(
                symbol=row["symbol"], setup=row["setup"],
                direction=row["direction"], session_id=row["session_id"] or "",
                entry_price=row["entry_price"], stop_price=row["stop_price"],
                target_price=row["target_price"], quantity=row["quantity"],
                opened_at=row["opened_at"] or 0,
                client_order_id=row["client_id"] or "", attributes=attrs,
            ))
        return out

    def close(self):
        with self._lock:
            self._conn.commit()
            self._conn.close()


def heartbeat(journal, state_machine, cache, portfolio, rate_limits,
              scanned=0, contexts=0, candidates=0):
    """One structured status line per interval.

    Reject counts are ABSOLUTE for the session, not deltas since the last beat. A
    delta forces a reader to sum across every log line to answer "how many since
    restart", which makes the simplest operational question unnecessarily hard.

    The regime tally is genuinely informative rather than decorative: if almost every
    symbol reads OUTSIDE_RANGE then the market is trending and the absence of
    mean-reversion trades is correct behaviour, not a fault to chase.
    """
    tally = journal.reject_tally() if journal else []
    top = " ".join(f"{reason}/{setup}={n}" for reason, setup, n in tally[:8])
    regimes = state_machine.regime_tally() if state_machine else {}
    regime_text = " ".join(f"{key}={value}" for key, value in sorted(regimes.items()))

    log.info(
        "HEARTBEAT %s | scanned=%d ctx=%d candidates=%d | %s | regimes: %s | "
        "rejects: %s | cache: %s",
        sessions.describe(), scanned, contexts, candidates,
        portfolio.as_row() if portfolio else {}, regime_text or "none",
        top or "none", cache.stats() if cache else {},
    )
    if rate_limits:
        log.info("  rate limits: %s", rate_limits)


def recover(rest, position_manager, state_store):
    """Reconcile persisted state against the exchange at startup.

    THE REFUSAL TO TRADE UNTIL THEY AGREE IS DELIBERATE. If the exchange holds a
    position this bot has no record of, something happened that the code does not
    understand - a crash at the wrong moment, a manual intervention, a partial fill
    handled badly. Trading on top of an unexplained position compounds whatever went
    wrong. Reporting and stopping is the only safe response, and it is loud on purpose.

    Returns True when it is safe to begin trading.
    """
    persisted = {m.symbol: m for m in state_store.load_positions()}

    live = {}
    try:
        for row in rest.position_risk() or []:
            amount = float(row.get("positionAmt") or 0.0)
            if amount != 0:
                live[(row.get("symbol") or "").upper()] = amount
    except Exception as exc:                      # noqa: BLE001
        log.error("recovery: positionRisk failed: %s", exc)
        return False

    for symbol, managed in persisted.items():
        if symbol in live:
            position_manager.adopt(managed)
            log.info("recovered %s %s %s qty=%.10g stop=%.8g",
                     symbol, managed.setup, managed.direction,
                     managed.quantity, managed.stop_price)
        else:
            # Closed while we were away. Drop the record; the trade journal is
            # settled by the normal reconcile pass on the next cycle.
            log.info("recovery: %s no longer open, clearing persisted state", symbol)
            state_store.drop_position(symbol)

    unexplained = set(live) - set(persisted)
    if unexplained:
        log.error("recovery: %d UNEXPLAINED position(s) on the exchange: %s",
                  len(unexplained), ", ".join(sorted(unexplained)))
        log.error("recovery: refusing to trade. Close or document these positions, "
                  "then restart.")
        return False

    cancel_stale_entries(rest, position_manager)

    log.info("recovery complete: %d position(s) adopted, no unexplained positions",
             len(persisted))
    return True


def cancel_stale_entries(rest, position_manager):
    """Cancel entry orders this bot left resting before it stopped.

    THE FAILURE THIS PREVENTS IS THE WORST ONE IN THE SYSTEM. Entries rest at the
    venue and are tracked in memory, so a restart loses the tracking but not the
    order. Nothing else cleans it up: the orphan-order pass only cancels reduceOnly
    and closePosition orders, and an entry is neither. So the order stays live, fills
    at some later point, and produces a position with no stop that the bot has no
    record of - at which point the orphan-position pass correctly refuses to touch it,
    because closing a position it cannot explain is worse than leaving it. The end
    state is an unprotected position, open indefinitely, by design.

    Two further reasons to cancel rather than adopt: the order's price and stop came
    from a profile that may now be stale, and an entry placed before a restart has
    already outlived the evidence that justified it.

    OWNERSHIP COMES FROM THE CLIENT ORDER ID, never from local state - local state is
    exactly what was lost. Anything without our tag is someone else's and is left
    strictly alone.
    """
    try:
        orders = rest.open_orders() or []
    except Exception as exc:                          # noqa: BLE001
        log.error("recovery: openOrders failed, cannot check for stale entries: %s",
                  exc)
        return 0

    cancelled = 0
    for order in orders:
        client_id = order.get("clientOrderId") or ""
        if router_mod.order_role(client_id) != router_mod.ROLE_ENTRY:
            continue
        symbol = (order.get("symbol") or "").upper()
        log.warning("recovery: cancelling stale entry order %s on %s "
                    "(left resting by a previous run)", client_id, symbol)
        try:
            rest.cancel_order(symbol, order_id=order.get("orderId"))
            cancelled += 1
        except Exception as exc:                      # noqa: BLE001
            log.error("recovery: could not cancel %s: %s", client_id, exc)

    if cancelled:
        log.warning("recovery: cancelled %d stale entry order(s)", cancelled)
    return cancelled
