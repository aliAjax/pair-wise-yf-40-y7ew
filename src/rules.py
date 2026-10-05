from datetime import datetime, timedelta
from uuid import uuid4

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")


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


def trace_upstream(waybills, consignment_id):
    """顺运单找上头来源：沿该批次的运单找到起运种植点。"""
    sources = []
    for item in waybills:
        data = item.get("data", {})
        if data.get("consignment_id") == consignment_id:
            source = data.get("from_facility_id")
            if source and source not in sources:
                sources.append(source)
    return sources


def _parse_dt(value):
    if value is None:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.fromisoformat(text[:10])


def _overlaps(start_a, end_a, start_b, end_b):
    """两个 [start, end) 时段是否重叠；end 为 None 表示延续至今。"""
    if start_a is None or start_b is None:
        return False
    if end_a is not None and start_b >= end_a:
        return False
    if end_b is not None and start_a >= end_b:
        return False
    return True


def compute_lockdown(positive_ids, waybills, stays, traced_links=None, positive_facilities=None):
    """根据阳性批次、运单和棚时记录计算封控范围。

    返回各种植点的阳性批次、接触批次、上游来源，以及当前冻结的设施/批次集合。
    种植点只有在仍有阳性批次撑着时才保持冻结。
    """
    positive_ids = set(positive_ids)
    facility_positive = {}
    facility_contacts = {}
    facility_upstream = {}
    positive_windows = {}

    if positive_facilities:
        for facility in positive_facilities:
            facility_positive.setdefault(facility, set())
            if traced_links:
                for batch in traced_links.get(facility, []):
                    if batch in positive_ids:
                        facility_positive[facility].add(batch)

    stays_by_facility = {}
    for item in stays:
        data = item.get("data", {})
        facility = data.get("facility_id")
        batch = data.get("consignment_id")
        if facility and batch:
            stays_by_facility.setdefault(facility, []).append(item)

    for facility, items in stays_by_facility.items():
        for item in items:
            data = item.get("data", {})
            batch = data.get("consignment_id")
            if batch in positive_ids:
                facility_positive.setdefault(facility, set()).add(batch)
                positive_windows.setdefault((facility, batch), []).append(
                    (_parse_dt(data.get("entered_at")), _parse_dt(data.get("left_at")))
                )

    if traced_links:
        for facility, batch_ids in traced_links.items():
            for batch in batch_ids:
                if batch in positive_ids:
                    facility_positive.setdefault(facility, set()).add(batch)

    for item in waybills:
        data = item.get("data", {})
        batch = data.get("consignment_id")
        source = data.get("from_facility_id")
        destination = data.get("to_facility_id")
        if batch in positive_ids and source:
            facility_positive.setdefault(source, set()).add(batch)
            if destination:
                facility_upstream.setdefault(destination, set()).add(source)

    for facility, positives in list(facility_positive.items()):
        for item in stays_by_facility.get(facility, []):
            data = item.get("data", {})
            batch = data.get("consignment_id")
            if batch in positive_ids:
                continue
            window = (_parse_dt(data.get("entered_at")), _parse_dt(data.get("left_at")))
            for positive in positives:
                for positive_window in positive_windows.get((facility, positive), []):
                    if _overlaps(positive_window[0], positive_window[1], window[0], window[1]):
                        facility_contacts.setdefault(facility, set()).add(batch)
                        break
                if batch in facility_contacts.get(facility, set()):
                    break

    frozen_facilities = set(facility_positive.keys())
    frozen_batches = set(positive_ids)
    for contacts in facility_contacts.values():
        frozen_batches.update(contacts)

    return {
        "frozen": bool(frozen_facilities),
        "frozen_facility_ids": sorted(frozen_facilities),
        "frozen_batch_ids": sorted(frozen_batches),
        "positive_by_facility": {f: sorted(v) for f, v in sorted(facility_positive.items())},
        "contact_by_facility": {f: sorted(v) for f, v in sorted(facility_contacts.items())},
        "upstream_by_facility": {f: sorted(v) for f, v in sorted(facility_upstream.items())},
    }


def _validate_waybill_create(actor, data, lookup):
    for field, kind in (
        ("consignment_id", "consignment"),
        ("from_facility_id", "facility"),
        ("to_facility_id", "facility"),
    ):
        if not _find_one(lookup, kind, "id", data.get(field)):
            raise ValidationError("%s does not exist: %s" % (field, data.get(field)))
    if data.get("from_facility_id") == data.get("to_facility_id"):
        raise ValidationError("waybill source and destination must differ")
    if _parse_dt(data.get("arrived_at")) < _parse_dt(data.get("shipped_at")):
        raise ValidationError("arrived_at must be after shipped_at")


def _validate_stay_create(actor, data, lookup):
    if not _find_one(lookup, "consignment", "id", data.get("consignment_id")):
        raise ValidationError("consignment does not exist: " + str(data.get("consignment_id")))
    if not _find_one(lookup, "facility", "id", data.get("facility_id")):
        raise ValidationError("facility does not exist: " + str(data.get("facility_id")))
    if data.get("left_at") and _parse_dt(data["left_at"]) < _parse_dt(data["entered_at"]):
        raise ValidationError("left_at must be after entered_at")


def _validate_conclude(actor, entity, data, lookup):
    if not isinstance(data.get("pest_found"), bool):
        raise ValidationError("pest_found must be a boolean")
    if data.get("pest_found") and not data.get("sample_id"):
        raise ValidationError("positive conclusion requires a sample_id")
    return {"concluded_by": actor.user_id}


def _validate_freeze(actor, entity, data, lookup):
    return {"frozen": True, "pre_freeze_status": entity["status"]}


def _validate_unfreeze(actor, entity, data, lookup):
    return {"frozen": False}


def _validate_release_lockdown(actor, entity, data, lookup):
    if entity["data"].get("positive_batch_ids"):
        raise ValidationError("lockdown with positive batches cannot be released")
    return {"released_by": actor.user_id}


def _validate_resolve_pending(actor, entity, data, lookup):
    return {"resolved_by": actor.user_id}


def _merge_stay(payload, existing_stays, consignment_ids, facility_ids, point_id, site_time):
    consignment = payload.get("consignment_id")
    facility = payload.get("facility_id")
    entered_at = payload.get("entered_at")
    if consignment not in consignment_ids:
        return "pending", {
            "record_type": "stay", "payload": payload, "reason": "consignment_not_found",
            "source_point": point_id, "site_recorded_at": site_time,
        }
    if facility not in facility_ids:
        return "pending", {
            "record_type": "stay", "payload": payload, "reason": "facility_not_found",
            "source_point": point_id, "site_recorded_at": site_time,
        }
    if not entered_at:
        return "pending", {
            "record_type": "stay", "payload": payload, "reason": "missing_entered_at",
            "source_point": point_id, "site_recorded_at": site_time,
        }
    for item in existing_stays:
        data = item.get("data", {})
        same_key = (
            data.get("consignment_id") == consignment
            and data.get("facility_id") == facility
            and str(data.get("entered_at"))[:19] == str(entered_at)[:19]
        )
        if not same_key:
            continue
        if str(data.get("left_at") or "")[:19] == str(payload.get("left_at") or "")[:19]:
            return "merged", item
        return "pending", {
            "record_type": "stay", "payload": payload, "reason": "conflicting_record",
            "source_point": point_id, "site_recorded_at": site_time, "conflicts_with": item.get("id"),
        }
    merged_payload = dict(payload)
    merged_payload.update({"source": "offline", "site_recorded_at": site_time, "source_point": point_id})
    return "merged", merged_payload


def _merge_waybill(payload, existing_waybills, consignment_ids, facility_ids, point_id, site_time):
    consignment = payload.get("consignment_id")
    source = payload.get("from_facility_id")
    destination = payload.get("to_facility_id")
    shipped_at = payload.get("shipped_at")
    if not payload.get("code"):
        payload["code"] = "WB-" + uuid4().hex[:8]
    if consignment not in consignment_ids:
        return "pending", {
            "record_type": "waybill", "payload": payload, "reason": "consignment_not_found",
            "source_point": point_id, "site_recorded_at": site_time,
        }
    if source not in facility_ids or destination not in facility_ids:
        return "pending", {
            "record_type": "waybill", "payload": payload, "reason": "facility_not_found",
            "source_point": point_id, "site_recorded_at": site_time,
        }
    if not shipped_at:
        return "pending", {
            "record_type": "waybill", "payload": payload, "reason": "missing_shipped_at",
            "source_point": point_id, "site_recorded_at": site_time,
        }
    for item in existing_waybills:
        data = item.get("data", {})
        same_key = (
            data.get("code") == payload.get("code")
            or (
                data.get("consignment_id") == consignment
                and data.get("from_facility_id") == source
                and data.get("to_facility_id") == destination
                and str(data.get("shipped_at"))[:19] == str(shipped_at)[:19]
            )
        )
        if not same_key:
            continue
        if str(data.get("arrived_at") or "")[:19] == str(payload.get("arrived_at") or "")[:19]:
            return "merged", item
        return "pending", {
            "record_type": "waybill", "payload": payload, "reason": "conflicting_record",
            "source_point": point_id, "site_recorded_at": site_time, "conflicts_with": item.get("id"),
        }
    merged_payload = dict(payload)
    merged_payload.update({"source": "offline", "site_recorded_at": site_time, "source_point": point_id})
    return "merged", merged_payload


def merge_offline_records(incoming, existing_stays, existing_waybills, consignment_ids, facility_ids, point_id, default_site_time=None):
    """按现场时间合并断网点回连的登记；对不上的单列待核对。"""
    merged = []
    pending = []
    for record in incoming:
        rtype = record.get("type")
        payload = record.get("payload", {})
        site_time = record.get("site_recorded_at") or default_site_time
        if rtype == "stay":
            disposition, result = _merge_stay(payload, existing_stays, consignment_ids, facility_ids, point_id, site_time)
        elif rtype == "waybill":
            disposition, result = _merge_waybill(payload, existing_waybills, consignment_ids, facility_ids, point_id, site_time)
        else:
            disposition, result = "pending", {
                "record_type": rtype or "unknown", "payload": payload, "reason": "unknown_record_type",
                "source_point": point_id, "site_recorded_at": site_time,
            }
        if disposition == "merged":
            merged.append(result)
        else:
            pending.append(result)
    return {"merged": merged, "pending": pending}


CUSTOM_CREATE = {
    'consignment': _validate_consignment,
    'waybill': _validate_waybill_create,
    'stay': _validate_stay_create,
}
CUSTOM_TRANSITIONS = {
    ('consignment', 'quarantine'): _validate_quarantine,
    ('consignment', 'release'): _validate_release,
    ('consignment', 'freeze'): _validate_freeze,
    ('consignment', 'unfreeze'): _validate_unfreeze,
    ('facility', 'conclude'): _validate_conclude,
    ('lockdown', 'release'): _validate_release_lockdown,
    ('pending', 'resolve'): _validate_resolve_pending,
}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility', 'waybills': 'waybill', 'stays': 'stay', 'lockdowns': 'lockdown'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered', 'waybill': 'issued', 'stay': 'recorded', 'lockdown': 'frozen', 'pending': 'open'}
    TRANSITIONS = {
        'consignment': {
            'inspect': (('declared',), 'inspected'),
            'quarantine': (('inspected', 'frozen'), 'quarantined'),
            'release': (('inspected', 'frozen'), 'released'),
            'destroy': (('quarantined',), 'destroyed'),
            'recheck': (('quarantined',), 'inspected'),
            'freeze': (('declared', 'inspected'), 'frozen'),
            'unfreeze': (('frozen',), '__pre_freeze__'),
        },
        'facility': {
            'trace': (('registered',), 'traced'),
            'conclude': (('registered', 'traced', 'quarantined', 'released'), '__conclude__'),
        },
        'waybill': {
            'correct': (('issued',), 'issued'),
        },
        'stay': {
            'correct': (('recorded',), 'recorded'),
        },
        'lockdown': {
            'recompute': (('frozen', 'released'), 'frozen'),
            'release': (('frozen',), 'released'),
        },
        'pending': {
            'resolve': (('open',), 'resolved'),
        },
    }
    CREATE_REQUIRED = {
        'consignment': ('code', 'origin', 'destination'),
        'facility': ('name', 'address'),
        'waybill': ('code', 'consignment_id', 'from_facility_id', 'to_facility_id', 'shipped_at', 'arrived_at'),
        'stay': ('consignment_id', 'facility_id', 'entered_at'),
        'lockdown': ('facility_id',),
        'pending': ('record_type', 'payload', 'reason'),
    }
    ACTION_REQUIRED = {
        ('consignment', 'inspect'): ('inspector', 'inspection_result'),
        ('consignment', 'quarantine'): ('pest_found', 'sample_id'),
        ('consignment', 'release'): ('pest_found', 'treatment'),
        ('consignment', 'destroy'): ('method', 'witnessed_by'),
        ('consignment', 'recheck'): ('sample_id',),
        ('facility', 'trace'): ('consignment_ids',),
        ('facility', 'conclude'): ('pest_found',),
        ('pending', 'resolve'): ('resolution',),
    }
    CREATE_ROLES = {
        'consignment': ('admin', 'inspector'),
        'facility': ('admin', 'quarantine'),
        'waybill': ('admin', 'quarantine'),
        'stay': ('admin', 'quarantine', 'inspector'),
        'lockdown': ('admin', 'quarantine'),
        'pending': ('admin',),
    }
    ROLE_ACTIONS = {
        'inspect': ('admin', 'inspector'),
        'quarantine': ('admin', 'quarantine'),
        'release': ('admin', 'quarantine'),
        'destroy': ('admin', 'quarantine'),
        'recheck': ('admin', 'inspector'),
        'trace': ('admin', 'quarantine'),
        'conclude': ('admin', 'quarantine'),
        'freeze': ('admin', 'quarantine'),
        'unfreeze': ('admin', 'quarantine'),
        'correct': ('admin', 'quarantine'),
        'recompute': ('admin', 'quarantine'),
        'resolve': ('admin',),
    }

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
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        if next_status == "__conclude__":
            next_status = "quarantined" if data.get("pest_found") else "released"
        elif next_status == "__pre_freeze__":
            next_status = entity["data"].get("pre_freeze_status") or "inspected"
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
