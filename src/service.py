from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import (
    RuleEngine,
    chain_links,
    chain_roots,
    downstream_counts,
    trace_downstream,
)


class DomainService:
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

    def _consignment_state(self):
        entities = self.repository.list_entities(kind="consignment")
        links = chain_links(entities)
        counts = downstream_counts(links)
        by_id = {entity["id"]: entity for entity in entities}
        return links, counts, by_id

    @staticmethod
    def _chain_node(entity, counts):
        data = entity.get("data") or {}
        return {
            "id": entity["id"],
            "code": data.get("code"),
            "status": entity["status"],
            "origin": data.get("origin"),
            "destination": data.get("destination"),
            "parent_id": data.get("parent_id"),
            "downstream_count": counts.get(entity["id"], 0),
        }

    def _chain_payload(self, root_id, links, counts, by_id):
        member_ids = [cid for cid in trace_downstream(links, root_id) if cid in by_id]
        members = [self._chain_node(by_id[cid], counts) for cid in member_ids]
        return {
            "root_id": root_id,
            "root": members[0] if members else None,
            "members": members,
            "size": len(members),
            "downstream_count": counts.get(root_id, 0),
        }

    def _require_consignment(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if self.rules.normalize_kind(entity["kind"]) != "consignment":
            raise ValidationError("not a consignment: " + entity_id)
        return entity

    def chains(self):
        links, counts, by_id = self._consignment_state()
        return [
            self._chain_payload(root_id, links, counts, by_id)
            for root_id in chain_roots(links)
        ]

    def chain(self, root_id):
        self._require_consignment(root_id)
        links, counts, by_id = self._consignment_state()
        return self._chain_payload(root_id, links, counts, by_id)

    def quarantine_chain(self, actor, root_id, reason=None):
        self._require_consignment(root_id)
        links, _, _ = self._consignment_state()
        reason = reason or ("chain quarantine from %s" % root_id)
        isolated = []
        skipped = []
        for cid in trace_downstream(links, root_id):
            current = self.repository.get_entity(cid)
            if not current:
                continue
            status = current["status"]
            code = (current.get("data") or {}).get("code")
            if status in ("destroyed", "released"):
                skipped.append({
                    "id": cid,
                    "code": code,
                    "status": status,
                    "reason": "already " + status,
                })
                continue
            if status == "quarantined":
                skipped.append({
                    "id": cid,
                    "code": code,
                    "status": status,
                    "reason": "already quarantined",
                })
                continue
            updated = self.transition(
                actor,
                cid,
                "isolate",
                {"reason": reason, "chain_root": root_id},
            )
            isolated.append({
                "id": cid,
                "code": code,
                "from_status": status,
                "to_status": updated["status"],
            })
        return {
            "root_id": root_id,
            "isolated": isolated,
            "skipped": skipped,
            "isolated_count": len(isolated),
            "skipped_count": len(skipped),
        }
