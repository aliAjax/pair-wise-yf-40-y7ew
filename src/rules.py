from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def parse_timestamp(value, field="timestamp"):
    """Parse an ISO-8601 timestamp; naive values are treated as UTC."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("invalid %s: %s" % (field, value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def windows_overlap(a_start, a_end, b_start, b_end):
    """Half-open interval overlap; a None end means the stay is still open."""
    a_end = a_end or _FAR_FUTURE
    b_end = b_end or _FAR_FUTURE
    return a_start < b_end and b_start < a_end


def stay_window(stay):
    data = stay.get("data", stay)
    start = parse_timestamp(data.get("arrived_at"), "arrived_at")
    end = parse_timestamp(data.get("departed_at"), "departed_at")
    if start is None:
        raise ValidationError("stay is missing arrived_at")
    if end is not None and end < start:
        raise ValidationError("departed_at must not be earlier than arrived_at")
    return start, end


def trace_sources(consignments, start_ids):
    """Walk upstream: parent batches plus every batch on the same waybill."""
    by_id = {item["id"]: item for item in consignments}
    visited = set()
    result = []
    stack = [sid for sid in start_ids if sid]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        entity = by_id.get(current)
        if entity is None:
            continue
        result.append(current)
        data = entity.get("data", {})
        parent = data.get("parent_id")
        if parent:
            stack.append(parent)
        waybill = data.get("waybill_no")
        if waybill:
            for other in consignments:
                if other["id"] not in visited and other.get("data", {}).get("waybill_no") == waybill:
                    stack.append(other["id"])
    return result


def compute_lockdown_scope(stays, consignments, positive_ids):
    """Scope of a lockdown: facilities with positive stays, contact batches
    (same facility, overlapping time window) and upstream source batches."""
    positives = set(positive_ids)
    windows_by_facility = {}
    for stay in stays:
        data = stay.get("data", {})
        if data.get("consignment_id") in positives:
            start, end = stay_window(stay)
            windows_by_facility.setdefault(data.get("facility_id"), []).append((start, end))
    contacts = set()
    for stay in stays:
        data = stay.get("data", {})
        consignment_id = data.get("consignment_id")
        if consignment_id in positives:
            continue
        for p_start, p_end in windows_by_facility.get(data.get("facility_id"), []):
            start, end = stay_window(stay)
            if windows_overlap(start, end, p_start, p_end):
                contacts.add(consignment_id)
                break
    sources = set(trace_sources(consignments, positives)) - positives
    return {
        "facilities": set(windows_by_facility),
        "contacts": contacts,
        "sources": sources,
    }


def find_stay_conflict(existing_stays, candidate, exclude_id=None):
    """Check a candidate stay against existing stays of the same consignment.

    Returns ("duplicate", stay_id) for an identical record, ("overlap", stay_id)
    when the consignment would be in two places at once, or None.
    """
    c_start = parse_timestamp(candidate.get("arrived_at"), "arrived_at")
    c_end = parse_timestamp(candidate.get("departed_at"), "departed_at")
    if c_start is None:
        raise ValidationError("arrived_at is required")
    if c_end is not None and c_end < c_start:
        raise ValidationError("departed_at must not be earlier than arrived_at")
    for stay in existing_stays:
        if exclude_id and stay.get("id") == exclude_id:
            continue
        data = stay.get("data", {})
        if data.get("consignment_id") != candidate.get("consignment_id"):
            continue
        s_start, s_end = stay_window(stay)
        if not windows_overlap(s_start, s_end, c_start, c_end):
            continue
        same_place = data.get("facility_id") == candidate.get("facility_id")
        same_times = (
            str(data.get("arrived_at")) == str(candidate.get("arrived_at"))
            and str(data.get("departed_at")) == str(candidate.get("departed_at"))
        )
        if same_place and same_times:
            return ("duplicate", stay.get("id"))
        return ("overlap", stay.get("id"))
    return None


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")


def _validate_stay(actor, data, lookup):
    start = parse_timestamp(data.get("arrived_at"), "arrived_at")
    end = parse_timestamp(data.get("departed_at"), "departed_at")
    if start is None:
        raise ValidationError("arrived_at is required")
    if end is not None and end < start:
        raise ValidationError("departed_at must not be earlier than arrived_at")
    if lookup is None:
        return
    if _find_one(lookup, "consignment", "id", data.get("consignment_id")) is None:
        raise ValidationError("unknown consignment: " + str(data.get("consignment_id")))
    if _find_one(lookup, "facility", "id", data.get("facility_id")) is None:
        raise ValidationError("unknown facility: " + str(data.get("facility_id")))
    stays = lookup("stay", "consignment_id", data.get("consignment_id")) or []
    conflict = find_stay_conflict(stays, data)
    if conflict:
        kind, stay_id = conflict
        if kind == "duplicate":
            raise ValidationError("duplicate stay already recorded: " + str(stay_id))
        raise ValidationError("consignment already has an overlapping stay: " + str(stay_id))


def _validate_quarantine(actor, entity, data, lookup):
    if not data.get("pest_found"):
        raise ValidationError("pest_found must be true for quarantine")
    return {"quarantined_by": actor.user_id}


def _validate_release(actor, entity, data, lookup):
    if data.get("pest_found"):
        raise ValidationError("pest-positive consignment cannot be released")
    if data.get("treatment") not in ("none", "completed", "certified"):
        raise ValidationError("release requires a valid treatment state")
    return {"released_by": actor.user_id}


def _validate_freeze(actor, entity, data, lookup):
    if entity["data"].get("frozen"):
        raise ValidationError("consignment is already frozen")
    return {
        "frozen": True,
        "freeze_reason": data.get("reason") or "manual",
        "frozen_by": actor.user_id,
    }


def _validate_unfreeze(actor, entity, data, lookup):
    if not entity["data"].get("frozen"):
        raise ValidationError("consignment is not frozen")
    return {"frozen": False, "freeze_reason": None, "frozen_by": None}


def _validate_dispatch(actor, entity, data, lookup):
    if entity["data"].get("frozen"):
        raise ValidationError("consignment is frozen and cannot be dispatched")
    if lookup:
        for stay in lookup("stay", "consignment_id", entity["id"]) or []:
            stay_data = stay.get("data", {})
            if stay_data.get("departed_at"):
                continue
            facility = _find_one(lookup, "facility", "id", stay_data.get("facility_id"))
            if facility and facility.get("status") == "frozen":
                raise ValidationError("current facility is frozen; dispatch is blocked")


def _validate_facility_freeze(actor, entity, data, lookup):
    return {"freeze_reason": data.get("reason") or "manual", "frozen_by": actor.user_id}


def _validate_lift(actor, entity, data, lookup):
    if entity["data"].get("conclusion") == "positive":
        raise ValidationError("facility concluded pest-positive; lift is not allowed")
    if lookup:
        for stay in lookup("stay", "facility_id", entity["id"]) or []:
            consignment = _find_one(
                lookup, "consignment", "id", stay.get("data", {}).get("consignment_id")
            )
            if consignment and consignment.get("status") == "quarantined":
                raise ValidationError(
                    "facility still has a pest-positive consignment; lift is not allowed"
                )
    return {"freeze_reason": None, "frozen_by": None}


def _validate_conclude(actor, entity, data, lookup):
    if entity["data"].get("conclusion"):
        raise ConflictError("conclusion already recorded; first submission stands")
    if data.get("conclusion") not in ("positive", "negative", "inconclusive"):
        raise ValidationError("conclusion must be one of positive/negative/inconclusive")
    return {
        "concluded_by": actor.user_id,
        "concluded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _validate_reschedule(actor, entity, data, lookup):
    if "arrived_at" not in data and "departed_at" not in data:
        raise ValidationError("reschedule requires arrived_at or departed_at")
    merged = dict(entity["data"])
    merged.update(data)
    stays = lookup("stay", "consignment_id", merged.get("consignment_id")) if lookup else []
    conflict = find_stay_conflict(stays or [], merged, exclude_id=entity["id"])
    if conflict:
        raise ValidationError("rescheduled stay overlaps another stay: " + str(conflict[1]))
    return {}


def trace_downstream(consignments, start_id):
    pending = [start_id]
    visited = set()
    result = []
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        result.append(current)
        for item in consignments:
            if item.get("parent_id") == current:
                pending.append(item.get("id"))
    return result


CUSTOM_CREATE = {'consignment': _validate_consignment, 'stay': _validate_stay}
CUSTOM_TRANSITIONS = {('consignment', 'quarantine'): _validate_quarantine, ('consignment', 'release'): _validate_release, ('consignment', 'freeze'): _validate_freeze, ('consignment', 'unfreeze'): _validate_unfreeze, ('consignment', 'dispatch'): _validate_dispatch, ('facility', 'freeze'): _validate_facility_freeze, ('facility', 'lift'): _validate_lift, ('facility', 'conclude'): _validate_conclude, ('stay', 'reschedule'): _validate_reschedule}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility', 'stays': 'stay'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered', 'stay': 'recorded'}
    TRANSITIONS = {'consignment': {'inspect': (('declared',), 'inspected'), 'quarantine': (('inspected',), 'quarantined'), 'release': (('inspected',), 'released'), 'destroy': (('quarantined',), 'destroyed'), 'recheck': (('quarantined',), 'inspected'), 'freeze': (('declared', 'inspected', 'dispatched'), None), 'unfreeze': (('declared', 'inspected', 'dispatched'), None), 'dispatch': (('declared', 'inspected'), 'dispatched')}, 'facility': {'trace': (('registered',), 'traced'), 'freeze': (('registered', 'traced'), 'frozen'), 'lift': (('frozen',), 'registered'), 'conclude': (('registered', 'traced', 'frozen'), None)}, 'stay': {'reschedule': (('recorded',), None)}}
    CREATE_REQUIRED = {'consignment': ('code', 'origin', 'destination'), 'facility': ('name', 'address'), 'stay': ('consignment_id', 'facility_id', 'arrived_at')}
    ACTION_REQUIRED = {('consignment', 'inspect'): ('inspector', 'inspection_result'), ('consignment', 'quarantine'): ('pest_found', 'sample_id'), ('consignment', 'release'): ('pest_found', 'treatment'), ('consignment', 'destroy'): ('method', 'witnessed_by'), ('consignment', 'recheck'): ('sample_id',), ('facility', 'trace'): ('consignment_ids',), ('facility', 'conclude'): ('conclusion',)}
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine'), 'stay': ('admin', 'inspector', 'quarantine')}
    ROLE_ACTIONS = {'inspect': ('admin', 'inspector'), 'quarantine': ('admin', 'quarantine'), 'release': ('admin', 'quarantine'), 'destroy': ('admin', 'quarantine'), 'recheck': ('admin', 'inspector'), 'trace': ('admin', 'quarantine'), 'freeze': ('admin', 'quarantine'), 'unfreeze': ('admin', 'quarantine'), 'dispatch': ('admin', 'quarantine'), 'lift': ('admin', 'quarantine'), 'conclude': ('admin', 'quarantine'), 'reschedule': ('admin', 'quarantine', 'inspector')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        if next_status is None:
            next_status = entity["status"]
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
