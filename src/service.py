from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .repository import utcnow
from .rules import (
    RuleEngine,
    compute_lockdown_scope,
    find_stay_conflict,
    parse_timestamp,
)

LOCKDOWN_ROLES = ("admin", "quarantine")
SYNC_ROLES = ("admin", "quarantine", "inspector")
# After these transitions the lockdown scope is recomputed automatically.
RECOMPUTE_TRIGGERS = {
    ("consignment", "quarantine"),
    ("consignment", "recheck"),
    ("stay", "reschedule"),
}


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    @staticmethod
    def _require_role(actor, roles):
        if actor.role not in roles:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        if kind == "stay":
            self._recompute(actor)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        kind = self.rules.normalize_kind(entity["kind"])
        if (kind, action) in RECOMPUTE_TRIGGERS or (
            kind == "facility"
            and action == "conclude"
            and updated["data"].get("conclusion") == "positive"
        ):
            self._recompute(actor)
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ---- lockdown orchestration ----

    @staticmethod
    def _facility_has_positive(stays, positive_ids, facility_id):
        for stay in stays:
            data = stay.get("data", {})
            if data.get("facility_id") == facility_id and data.get("consignment_id") in positive_ids:
                return True
        return False

    def _set_facility_freeze(self, actor, facility, frozen, reason):
        data = dict(facility["data"])
        if frozen:
            data.update({"freeze_reason": reason, "frozen_by": actor.user_id})
            status = "frozen"
        else:
            data.update({"freeze_reason": None, "frozen_by": None})
            status = "registered"
        updated = self.repository.update_entity(facility["id"], None, status, data)
        self.audit.record(
            facility["id"],
            actor,
            "freeze" if frozen else "lift",
            facility["status"],
            status,
            {"reason": reason, "source": "lockdown"},
        )
        return updated

    def _set_consignment_freeze(self, actor, consignment, frozen, reason):
        data = dict(consignment["data"])
        if frozen:
            data.update({"frozen": True, "freeze_reason": reason, "frozen_by": actor.user_id})
        else:
            data.update({"frozen": False, "freeze_reason": None, "frozen_by": None})
        updated = self.repository.update_entity(
            consignment["id"], None, consignment["status"], data
        )
        self.audit.record(
            consignment["id"],
            actor,
            "freeze" if frozen else "unfreeze",
            consignment["status"],
            consignment["status"],
            {"reason": reason, "source": "lockdown"},
        )
        return updated

    def _recompute(self, actor):
        consignments = self.repository.list_entities(kind="consignment")
        stays = self.repository.list_entities(kind="stay")
        facilities = self.repository.list_entities(kind="facility")
        positive_ids = {c["id"] for c in consignments if c["status"] == "quarantined"}
        scope = compute_lockdown_scope(stays, consignments, positive_ids)
        changes = {
            "frozen_facilities": [],
            "lifted_facilities": [],
            "frozen_consignments": [],
            "unfrozen_consignments": [],
        }
        for facility in facilities:
            has_positive = self._facility_has_positive(stays, positive_ids, facility["id"])
            keep_frozen = (
                facility["id"] in scope["facilities"]
                or has_positive
                or facility["data"].get("conclusion") == "positive"
            )
            if keep_frozen and facility["status"] != "frozen":
                self._set_facility_freeze(actor, facility, True, "lockdown")
                changes["frozen_facilities"].append(facility["id"])
            elif (
                not keep_frozen
                and facility["status"] == "frozen"
                and facility["data"].get("freeze_reason") == "lockdown"
            ):
                # A site is only released once no positive batch supports it.
                self._set_facility_freeze(actor, facility, False, None)
                changes["lifted_facilities"].append(facility["id"])
        targets = scope["contacts"] | scope["sources"]
        for consignment in consignments:
            if consignment["status"] not in ("declared", "inspected", "dispatched"):
                continue
            should_freeze = consignment["id"] in targets
            is_frozen = bool(consignment["data"].get("frozen"))
            if should_freeze and not is_frozen:
                self._set_consignment_freeze(actor, consignment, True, "lockdown")
                changes["frozen_consignments"].append(consignment["id"])
            elif (
                not should_freeze
                and is_frozen
                and consignment["data"].get("freeze_reason") == "lockdown"
            ):
                self._set_consignment_freeze(actor, consignment, False, None)
                changes["unfrozen_consignments"].append(consignment["id"])
        return changes

    def lockdown(self, actor, facility_id, reason=None):
        self._require_role(actor, LOCKDOWN_ROLES)
        facility = self.repository.get_entity(facility_id)
        if not facility or facility["kind"] != "facility":
            raise NotFoundError("facility not found: " + facility_id)
        if facility["status"] != "frozen":
            self._set_facility_freeze(actor, facility, True, reason or "manual")
        changes = self._recompute(actor)
        return {"facility": self.repository.get_entity(facility_id), "changes": changes}

    def recompute_lockdown(self, actor):
        self._require_role(actor, LOCKDOWN_ROLES)
        return self._recompute(actor)

    def scope_preview(self, facility_id):
        facility = self.repository.get_entity(facility_id)
        if not facility or facility["kind"] != "facility":
            raise NotFoundError("facility not found: " + facility_id)
        consignments = self.repository.list_entities(kind="consignment")
        stays = self.repository.list_entities(kind="stay")
        positive_ids = {c["id"] for c in consignments if c["status"] == "quarantined"}
        scope = compute_lockdown_scope(stays, consignments, positive_ids)
        return {
            "facility_id": facility_id,
            "status": facility["status"],
            "in_scope": facility_id in scope["facilities"],
            "has_positive": self._facility_has_positive(stays, positive_ids, facility_id),
            "positive_consignment_ids": sorted(positive_ids),
            "contact_consignment_ids": sorted(scope["contacts"]),
            "source_consignment_ids": sorted(scope["sources"]),
            "affected_facility_ids": sorted(scope["facilities"]),
        }

    # ---- offline registration sync ----

    def _create_synced_stay(self, actor, item):
        stay_id = str(item.get("id") or uuid4())
        if self.repository.get_entity(stay_id):
            raise ConflictError("entity already exists: " + stay_id)
        data = {
            "consignment_id": item["consignment_id"],
            "facility_id": item["facility_id"],
            "arrived_at": item["arrived_at"],
            "departed_at": item.get("departed_at"),
            "occurred_at": item.get("occurred_at") or item["arrived_at"],
            "received_at": utcnow(),
            "source": "field_sync",
        }
        if item.get("client_id"):
            data["client_id"] = item["client_id"]
        stay = self.repository.create_entity(stay_id, "stay", "recorded", data, actor.user_id)
        self.audit.record(stay_id, actor, "create", None, "recorded", {"kind": "stay", "source": "field_sync"})
        return stay

    def _park_pending(self, actor, item, reason):
        review_id = str(uuid4())
        self.repository.add_pending_review(review_id, "stay", item, reason, actor.user_id)
        return {"id": review_id, "reason": reason, "item": item}

    def sync_stays(self, actor, items):
        """Merge offline stay registrations ordered by on-site time (occurred_at).

        Items that do not match the current record are parked in the
        pending-review queue instead of being applied.
        """
        self._require_role(actor, SYNC_ROLES)
        if not isinstance(items, list) or not items:
            raise ValidationError("items must be a non-empty list")
        normalized = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValidationError("each sync item must be an object")
            for field in ("consignment_id", "facility_id", "arrived_at"):
                if not item.get(field):
                    raise ValidationError("sync item missing required field: " + field)
            occurred_at = item.get("occurred_at") or item["arrived_at"]
            parse_timestamp(item["arrived_at"], "arrived_at")
            if item.get("departed_at"):
                parse_timestamp(item["departed_at"], "departed_at")
            normalized.append((parse_timestamp(occurred_at, "occurred_at").isoformat(), index, item))
        normalized.sort(key=lambda entry: (entry[0], entry[1]))
        applied, duplicates, pending = [], [], []
        for _, _, item in normalized:
            consignment = self.repository.get_entity(item["consignment_id"])
            if not consignment or consignment["kind"] != "consignment":
                pending.append(self._park_pending(actor, item, "unknown consignment: " + str(item["consignment_id"])))
                continue
            facility = self.repository.get_entity(item["facility_id"])
            if not facility or facility["kind"] != "facility":
                pending.append(self._park_pending(actor, item, "unknown facility: " + str(item["facility_id"])))
                continue
            existing = self.repository.find_entities("stay", "consignment_id", item["consignment_id"])
            conflict = find_stay_conflict(existing, item)
            if conflict:
                kind, stay_id = conflict
                if kind == "duplicate":
                    duplicates.append({"stay_id": stay_id, "item": item})
                else:
                    pending.append(self._park_pending(actor, item, "overlaps existing stay " + str(stay_id)))
                continue
            applied.append(self._create_synced_stay(actor, item))
        changes = self._recompute(actor) if applied else {
            "frozen_facilities": [],
            "lifted_facilities": [],
            "frozen_consignments": [],
            "unfrozen_consignments": [],
        }
        return {"applied": applied, "duplicates": duplicates, "pending": pending, "changes": changes}

    def list_pending(self, status=None):
        return self.repository.list_pending_reviews(status=status)

    def resolve_pending(self, actor, review_id, decision):
        self._require_role(actor, LOCKDOWN_ROLES)
        record = self.repository.get_pending_review(review_id)
        if not record:
            raise NotFoundError("pending review not found: " + review_id)
        if record["status"] != "pending":
            raise ConflictError("pending review already resolved")
        if decision not in ("accept", "discard"):
            raise ValidationError("decision must be accept or discard")
        stay = None
        if decision == "accept":
            item = record["payload"]
            for field in ("consignment_id", "facility_id", "arrived_at"):
                if not item.get(field):
                    raise ValidationError("pending payload missing required field: " + field)
            stay = self._create_synced_stay(actor, item)
            self._recompute(actor)
        self.repository.resolve_pending_review(
            review_id, "accepted" if decision == "accept" else "discarded", actor.user_id
        )
        return {"review": self.repository.get_pending_review(review_id), "stay": stay}
