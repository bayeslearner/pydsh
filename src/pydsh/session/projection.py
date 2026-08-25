"""Reading the log as state — the fold primitive every domain view is built on.

A **projection** is a domain's maths over the session log: three pure functions
(``init``, ``apply``, ``view``) contributed by a plugin that never subscribes to
anything, driven by a registry that never knows what the values mean.

The split is the point. Without it, every consumer that wants "how many turns
has this had" walks the log itself, with its own rules, and the rules drift.

**Identity is the change gate.** ``apply`` returning the *same object* means
"this event does not concern me", and the registry then does nothing at all —
no view, no validation, no notification. That is what lets a registry carry
twenty units without costing twenty times as much on an event nineteen of them
ignore. Returning an equal-but-new object instead would publish a change that
did not happen.

**Cells are a cache, never authority.** Delete every one and the next read
rebuilds the same values by folding the log. Nothing here is a store.
"""

from __future__ import annotations

import copy
import logging
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from plugkit import Service

logger = logging.getLogger("pydsh.projection")

#: Session event sequence numbers start at 1, so 0 means "nothing observed".
#: The reference uses -1 because its own `seq` is the *next* number to be
#: assigned; pydsh's is the last committed one. Transcribing its arithmetic
#: would report every snapshot one event stale.
EMPTY_WATERMARK = 0

#: The first seq a log can hold. A cold read starting here has the whole log.
FIRST_SEQ = 1

#: A unit's view validator: given the view, return it (or a validated form).
Validator = Callable[[Any], Any]

#: ``(session, key, value, seq)`` — one unit's value changed for one session.
ChangeListener = Callable[[Any, str, Any, int], None]


@dataclass(frozen=True)
class ProjectionDefinition:
    """One domain's state-driven unit: three pure functions and a contract.

    :param key: the name this unit owns in a snapshot.
    :param init: the state of an empty log.
    :param apply: ``(state, event) -> state``. **Must be synchronous** — an
        async transition would tear the consistent slice a snapshot promises —
        and must return the *same reference* for events it does not care about.
    :param view: ``state -> value``, the read-side projection.
    :param validate: optional check applied to a view before it leaves.
    :param state_version: bump when the serialized state's shape or the fold's
        meaning changes. Cached rows from another version are discarded rather
        than fed forward into an ``apply`` that no longer understands them.
    """

    key: str
    init: Callable[[], Any]
    apply: Callable[[Any, Any], Any]
    view: Callable[[Any], Any]
    validate: Optional[Validator] = None
    state_version: int = 1


class ProjectionFaultError(RuntimeError):
    """A unit's ``apply`` raised, so its value for that session is unknowable.

    Raised on *read*, not on append. The append that triggered the failure has
    already committed and cannot be undone — containing it is spec 01's rule
    that an observer never rewrites history. But the cell has now missed a
    transition, so every later value it could offer is wrong. Faulting on read
    is the difference between "this projection is broken" and a plausible
    number nobody questions.
    """


@dataclass
class _Cell:
    """One unit's folded state for one session, and how far it has seen."""

    state: Any
    watermark: int = EMPTY_WATERMARK
    #: Set when `apply` raised. Terminal for this session: refolding would
    #: replay the same event onto the same maths and fail identically.
    fault: Optional[BaseException] = None


@dataclass
class _Registration:
    """One key's unit, its per-session cells, and how many registrants share it."""

    definition: ProjectionDefinition
    cells: "weakref.WeakKeyDictionary" = field(default_factory=weakref.WeakKeyDictionary)
    refs: int = 1


def _validated(definition: ProjectionDefinition, state: Any) -> Any:
    """A unit's view of a state, checked by the unit's own validator."""
    view = definition.view(state)
    return definition.validate(view) if definition.validate is not None else view


class SessionProjections(Service):
    """Provides ``ctx.session_projections`` — the unit registry and its driver."""

    provide = "session_projections"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._registrations: dict[str, _Registration] = {}
        self._listeners: list[ChangeListener] = []
        # One subscription for every unit: the registry owns the plumbing so a
        # domain plugin never has to know when sessions appear or vanish.
        ctx.on("session/event", self._drive)

    # -- registration ------------------------------------------------------ #
    def register(self, definition: ProjectionDefinition) -> Callable[[], None]:
        """Register a unit; returns its disposer.

        Registering a key twice is normal — the same plugin mounted under N
        agent presets — so registrations are counted and share one cell. The
        one conflict that cannot be shared is a differing ``state_version``:
        that is a versioned contract saying the cached shape is not the same.
        """
        version = definition.state_version
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError(
                f"projection {definition.key!r} needs a non-negative integer "
                f"state_version, got {version!r}"
            )

        key = definition.key
        existing = self._registrations.get(key)
        if existing is None:
            self._registrations[key] = _Registration(definition)
        elif existing.definition.state_version != version:
            raise ValueError(
                f"projection key {key!r} is registered at state_version "
                f"{existing.definition.state_version}; refusing to share a cell "
                f"with state_version {version}"
            )
        else:
            existing.refs += 1

        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            live = self._registrations.get(key)
            if live is None:
                return
            live.refs -= 1
            if live.refs <= 0:
                self._registrations.pop(key, None)

        return dispose

    def on_changed(self, listener: ChangeListener) -> Callable[[], None]:
        """Subscribe to the change stream; returns an exact unsubscribe."""
        self._listeners.append(listener)

        def dispose() -> None:
            # By identity: the same callable may be subscribed twice, and
            # `list.remove` would drop whichever compared equal first.
            for index, candidate in enumerate(self._listeners):
                if candidate is listener:
                    self._listeners.pop(index)
                    return

        return dispose

    def keys(self) -> list[str]:
        """The registered unit keys, sorted."""
        return sorted(self._registrations)

    # -- driving ----------------------------------------------------------- #
    def _fold(self, definition: ProjectionDefinition, events) -> _Cell:
        """Fold a unit from ``init`` over a run of events."""
        state = definition.init()
        watermark = EMPTY_WATERMARK
        for event in events:
            try:
                state = definition.apply(state, event)
            except Exception as exc:  # noqa: BLE001 - recorded, surfaced on read
                logger.error(
                    "projection %r failed while folding %s (seq %s)",
                    definition.key,
                    event.type,
                    event.seq,
                    exc_info=exc,
                )
                return _Cell(state, watermark, fault=exc)
            watermark = event.seq
        return _Cell(state, watermark)

    def _cell_for(self, registration: _Registration, session: Any) -> _Cell:
        """A unit's cell for a session, folding the whole log on first touch.

        :raises ProjectionFaultError: this unit's maths already failed for this
            session, so it has no value to give.
        """
        cell = registration.cells.get(session)
        if cell is None:
            cell = self._fold(registration.definition, session.events)
            registration.cells[session] = cell
        if cell.fault is not None:
            raise ProjectionFaultError(
                f"projection {registration.definition.key!r} failed while folding "
                f"this session and has no value: {cell.fault!r}"
            ) from cell.fault
        return cell

    def _drive(self, session: Any, event: Any) -> None:
        """Take one committed event through every registered unit."""
        for registration in list(self._registrations.values()):
            definition = registration.definition
            cell = registration.cells.get(session)
            if cell is None:
                # A unit registered mid-stream: fold everything strictly before
                # this event, then take the normal path, so this event is
                # applied exactly once rather than skipped or doubled.
                cell = self._fold(
                    definition, [e for e in session.events if e.seq < event.seq]
                )
                registration.cells[session] = cell

            if cell.fault is not None:
                continue  # already broken for this session; reads will say so

            try:
                next_state = definition.apply(cell.state, event)
            except Exception as exc:  # noqa: BLE001 - see ProjectionFaultError
                # Contained here because the append already committed and an
                # observer must not rewrite history. Recorded because the cell
                # has now missed a transition, and every later value would be
                # confidently wrong. Reads raise instead.
                cell.fault = exc
                logger.error(
                    "projection %r failed on %s (seq %s); its values are now "
                    "unavailable for this session",
                    definition.key,
                    event.type,
                    event.seq,
                    exc_info=exc,
                )
                continue

            changed = next_state is not cell.state
            cell.state = next_state
            cell.watermark = event.seq
            if changed and self._listeners:
                self._publish(session, definition, next_state, event.seq)

    def _publish(
        self, session: Any, definition: ProjectionDefinition, state: Any, seq: int
    ) -> None:
        """Notify the change stream; a failing listener does not stop the rest.

        Contained because this is a notification *after* the fact: the state
        already changed, and one bad observer must not undo it or silence the
        others.
        """
        value = _validated(definition, state)
        for listener in list(self._listeners):
            try:
                listener(session, definition.key, value, seq)
            except Exception as exc:  # noqa: BLE001 - containment is the point
                logger.warning(
                    "projection listener for %r failed: %s",
                    definition.key,
                    exc,
                    exc_info=exc,
                )

    # -- reading ----------------------------------------------------------- #
    def snapshot(self, session: Any) -> dict:
        """A consistent read across every unit for one session.

        Every value reflects the same point in the log, so a consumer never
        sees two units disagreeing about *when*.
        """
        values = {
            registration.definition.key: _validated(
                registration.definition, self._cell_for(registration, session).state
            )
            for registration in self._registrations.values()
        }
        return {"as_of_seq": session.seq, "values": values}

    def checkpoint(self, session: Any) -> dict:
        """State-level rows for every unit — the write side of a durable cache.

        ``val`` is a **detached deep copy**. The cells are this registry's
        authoritative mutable state; handing out a live reference would let a
        caller corrupt every later snapshot.
        """
        rows: dict[str, dict] = {}
        for registration in self._registrations.values():
            definition = registration.definition
            cell = self._cell_for(registration, session)
            rows[definition.key] = {
                "ver": definition.state_version,
                "seq": cell.watermark,
                "val": copy.deepcopy(cell.state),
            }
        return rows

    def view_checkpoint(self, rows: dict) -> dict:
        """Zero-I/O read: the view of every row whose version still matches.

        A row from another ``state_version`` makes its key *absent* rather than
        wrong — a cold consumer reads that as "not available yet" and a fuller
        read path refolds it.
        """
        values: dict[str, Any] = {}
        for registration in self._registrations.values():
            definition = registration.definition
            row = rows.get(definition.key)
            if row is None or row.get("ver") != definition.state_version:
                continue
            values[definition.key] = _validated(definition, row["val"])
        return values

    def restore_floor(self, rows: dict) -> Optional[int]:
        """The event seq a cold read must start from, or ``None`` if no units.

        One below the lowest usable watermark, clamped: the overlap is harmless
        because :meth:`restore` re-checks each event against the row's own
        watermark before folding it.
        """
        floor: Optional[int] = None
        for registration in self._registrations.values():
            definition = registration.definition
            row = rows.get(definition.key)
            usable = row is not None and row.get("ver") == definition.state_version
            need = (row.get("seq", EMPTY_WATERMARK) + 1) if usable else FIRST_SEQ
            floor = need if floor is None else min(floor, need)
        if floor is None:
            return None
        return max(floor - 1, EMPTY_WATERMARK)

    def restore(self, rows: dict, events: list, base_seq: int) -> dict:
        """Cold read: seed each unit from its row and fold a stored tail on top.

        :param base_seq: the seq the supplied tail starts at. ``FIRST_SEQ``
            means the whole log is present.
        :raises RuntimeError: a row is unusable and the caller supplied only a
            tail. Folding from ``init`` over part of a history would return a
            confident number computed from half the evidence, and nothing
            downstream could tell.
        """
        end_seq = events[-1].seq if events else base_seq - 1
        values: dict[str, Any] = {}
        refreshed: dict[str, dict] = {}

        for registration in self._registrations.values():
            definition = registration.definition
            row = rows.get(definition.key)
            row_seq = row.get("seq", EMPTY_WATERMARK) if row is not None else None
            usable = (
                row is not None
                and row.get("ver") == definition.state_version
                # covers everything before the tail starts …
                and row_seq >= base_seq - 1
                # … and is not ahead of the evidence supplied to check it
                and row_seq <= end_seq
            )
            if not usable and base_seq > FIRST_SEQ:
                raise RuntimeError(
                    f"projection {definition.key!r} cannot be restored from seq "
                    f"{base_seq}: its row is missing, versioned differently, or "
                    "ahead of the supplied events — re-read the log from the start"
                )

            state = copy.deepcopy(row["val"]) if usable else definition.init()
            from_seq = row_seq if usable else base_seq - 1
            for event in events:
                if event.seq > from_seq:
                    state = definition.apply(state, event)

            values[definition.key] = _validated(definition, state)
            refreshed[definition.key] = {
                "ver": definition.state_version,
                "seq": end_seq,
                "val": copy.deepcopy(state),
            }

        return {
            "snapshot": {"as_of_seq": end_seq, "values": values},
            "checkpoint": refreshed,
        }


__all__ = [
    "ProjectionDefinition",
    "SessionProjections",
    "ChangeListener",
    "Validator",
    "EMPTY_WATERMARK",
    "FIRST_SEQ",
    "ProjectionFaultError",
]
