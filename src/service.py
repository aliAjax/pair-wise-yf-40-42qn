from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, zone_release_readiness


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _blocked_consignments(self, zone):
        facility_ids = set(zone["data"].get("facility_ids", []))
        blocked = []
        for consignment in self.repository.list_entities(kind="consignment"):
            if consignment["status"] in ("shipped", "destroyed"):
                continue
            origin = consignment["data"].get("origin_facility_id")
            destination = consignment["data"].get("destination_facility_id")
            if origin in facility_ids and destination not in facility_ids:
                blocked.append(consignment["id"])
        return blocked

    def _view(self, entity):
        if entity and entity["kind"] == "zone":
            entity["data"]["release"] = zone_release_readiness(entity)
            if entity["status"] == "active":
                entity["data"]["blocked_consignment_ids"] = self._blocked_consignments(entity)
        return entity

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
                    return self._view(entity)
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return self._view(entity)

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
        return self._view(updated)

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return self._view(entity)

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return [self._view(entity) for entity in self.repository.list_entities(kind=kind, status=status)]

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
