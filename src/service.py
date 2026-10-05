from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .repository import utcnow
from .rules import RuleEngine, compute_lockdown, merge_offline_records


class DomainService:
    # 状态变更后触发封控范围重算
    RECOMPUTE_ACTIONS = {
        ("consignment", "quarantine"),
        ("consignment", "release"),
        ("consignment", "destroy"),
        ("facility", "conclude"),
        ("waybill", "correct"),
        ("stay", "correct"),
    }

    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

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
        if (self.rules.normalize_kind(entity["kind"]), action) in self.RECOMPUTE_ACTIONS:
            self.recompute_lockdowns(actor, facility_id=entity_id if entity["kind"] == "facility" else None)
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

    def recompute_lockdowns(self, actor, facility_id=None):
        """重算封控范围：阳性批次顺运单找来源，同期同棚算接触；没有阳性撑着的种植点放开。"""
        positives = [
            entity for entity in self.repository.list_entities(kind="consignment")
            if entity["status"] == "quarantined" and entity["data"].get("pest_found")
        ]
        positive_ids = {entity["id"] for entity in positives}
        positive_facilities = {
            entity["id"] for entity in self.repository.list_entities(kind="facility")
            if entity["status"] == "quarantined" and entity["data"].get("pest_found")
        }
        waybills = self.repository.list_entities(kind="waybill")
        stays = self.repository.list_entities(kind="stay")
        facilities = self.repository.list_entities(kind="facility")
        traced = {
            entity["id"]: entity["data"].get("consignment_ids", [])
            for entity in facilities
        }
        scope = compute_lockdown(
            positive_ids, waybills, stays,
            traced_links=traced, positive_facilities=positive_facilities,
        )
        existing = self.repository.list_entities(kind="lockdown")
        locked_facilities = {item["data"].get("facility_id") for item in existing}
        if facility_id:
            targets = {facility_id}
        else:
            targets = set(scope["frozen_facility_ids"]) | locked_facilities
        results = []
        for target in sorted(targets):
            results.append(self._apply_facility_lockdown(actor, target, scope))
        self._sync_batch_freeze(actor, scope["frozen_batch_ids"])
        return results

    def _apply_facility_lockdown(self, actor, facility_id, scope):
        pos = scope["positive_by_facility"].get(facility_id, [])
        contacts = scope["contact_by_facility"].get(facility_id, [])
        upstream = scope["upstream_by_facility"].get(facility_id, [])
        facilities = self.repository.find_entities("facility", "id", facility_id)
        facility = facilities[0] if facilities else None
        concluded_positive = bool(
            facility
            and facility["status"] == "quarantined"
            and facility["data"].get("pest_found")
        )
        frozen = bool(pos) or concluded_positive
        data = {
            "facility_id": facility_id,
            "positive_batch_ids": pos,
            "contact_batch_ids": contacts,
            "upstream_facility_ids": upstream,
            "computed_at": utcnow(),
            "computed_by": actor.user_id,
        }
        existing = self.repository.find_entities("lockdown", "facility_id", facility_id)
        if existing:
            lockdown = existing[0]
            result = self.repository.update_entity(
                lockdown["id"], lockdown["version"],
                "frozen" if frozen else "released", data,
            )
        else:
            result = self.repository.create_entity(
                str(uuid4()), "lockdown",
                "frozen" if frozen else "released", data, actor.user_id,
            )
        if facility:
            fdata = dict(facility["data"])
            fdata["lockdown_status"] = "frozen" if frozen else "released"
            fdata["positive_batch_ids"] = pos
            fdata["contact_batch_ids"] = contacts
            self.repository.update_entity(
                facility["id"], facility["version"],
                "quarantined" if frozen else "released", fdata,
            )
        return result

    def _sync_batch_freeze(self, actor, frozen_batch_ids):
        for batch in self.repository.list_entities(kind="consignment"):
            data = dict(batch["data"])
            in_scope = batch["id"] in frozen_batch_ids
            terminal = batch["status"] in ("quarantined", "destroyed", "released")
            if in_scope and not terminal and batch["status"] != "frozen":
                data["frozen"] = True
                data["pre_freeze_status"] = batch["status"]
                self.repository.update_entity(batch["id"], batch["version"], "frozen", data)
            elif not in_scope and batch["status"] == "frozen":
                data["frozen"] = False
                restore = data.pop("pre_freeze_status", None) or "inspected"
                self.repository.update_entity(batch["id"], batch["version"], restore, data)

    def sync_offline(self, actor, records, point_id, site_recorded_at=None):
        """断网点登记回连：按现场时间合并，对不上的单列待核对。"""
        stays = self.repository.list_entities(kind="stay")
        waybills = self.repository.list_entities(kind="waybill")
        consignment_ids = {item["id"] for item in self.repository.list_entities(kind="consignment")}
        facility_ids = {item["id"] for item in self.repository.list_entities(kind="facility")}
        result = merge_offline_records(
            records or [], stays, waybills,
            consignment_ids, facility_ids, point_id, site_recorded_at,
        )
        merged = []
        for item in result["merged"]:
            if item.get("id"):
                merged.append(item)
                continue
            if "entered_at" in item:
                merged.append(self.create(actor, "stay", item))
            else:
                if not item.get("code"):
                    item["code"] = "WB-" + uuid4().hex[:8]
                merged.append(self.create(actor, "waybill", item))
        pending = []
        for item in result["pending"]:
            pending.append(self.create(actor, "pending", item))
        return {"merged": merged, "pending_verification": pending}
