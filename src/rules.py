from datetime import date, datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

ZONE_QUARANTINE_DAYS = 21


def _today():
    return date.today()


def _as_date(value):
    return datetime.fromisoformat(str(value)[:10]).date()


def _facility_exists(lookup, facility_id):
    return bool(lookup and facility_id and _find_one(lookup, "facility", "id", facility_id))


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")
    if (
        data.get("origin_facility_id")
        and data.get("origin_facility_id") == data.get("destination_facility_id")
    ):
        raise ValidationError("origin and destination must differ")
    if lookup:
        for field in ("origin_facility_id", "destination_facility_id"):
            facility_id = data.get(field)
            if facility_id and not _find_one(lookup, "facility", "id", facility_id):
                raise ValidationError("unknown facility: " + str(facility_id))


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


def _validate_zone_create(actor, data, lookup):
    for field in ("zone_no", "center_facility_id", "facility_ids", "reason"):
        if not data.get(field):
            raise ValidationError("missing required field: " + field)
    center = data["center_facility_id"]
    if not _facility_exists(lookup, center):
        raise ValidationError("unknown center facility: " + str(center))
    if lookup and _find_one(lookup, "zone", "zone_no", data["zone_no"]):
        raise ValidationError("zone number already exists: " + str(data["zone_no"]))
    facility_ids = list(dict.fromkeys([center] + list(data["facility_ids"])))
    for facility_id in facility_ids:
        if not _facility_exists(lookup, facility_id):
            raise ValidationError("unknown facility: " + str(facility_id))
    reasons = data.get("facility_reasons") or {}
    if not isinstance(reasons, dict):
        raise ValidationError("facility_reasons must be an object")
    positive_since = str(data.get("positive_since") or _today())
    _as_date(positive_since)
    data["facility_ids"] = facility_ids
    data["facility_reasons"] = {
        facility_id: reasons.get(facility_id, data["reason"])
        for facility_id in facility_ids
    }
    data["positive_events"] = [
        {"facility_id": center, "date": positive_since, "recorded_by": actor.user_id}
    ]
    data["last_positive_date"] = positive_since
    data["new_positive_facility_ids"] = []
    data["registered_by"] = actor.user_id


def _validate_zone_mark_positive(actor, entity, data, lookup):
    facility_id = data.get("facility_id")
    if not facility_id:
        raise ValidationError("missing required field: facility_id")
    if not _facility_exists(lookup, facility_id):
        raise ValidationError("unknown facility: " + str(facility_id))
    found_date = str(data.get("date") or _today())
    _as_date(found_date)
    payload = dict(entity["data"])
    facility_ids = list(payload.get("facility_ids", []))
    reasons = dict(payload.get("facility_reasons", {}))
    events = list(payload.get("positive_events", []))
    new_facility_ids = list(payload.get("new_positive_facility_ids", []))
    is_new_facility = facility_id not in facility_ids
    first_positive = facility_id not in {event.get("facility_id") for event in events}
    if is_new_facility:
        facility_ids.append(facility_id)
        reasons[facility_id] = data.get("reason") or "后续检测阳性，追加成纳入设施"
    if first_positive:
        new_facility_ids.append(facility_id)
    events.append({"facility_id": facility_id, "date": found_date, "recorded_by": actor.user_id})
    last_positive_date = max([payload.get("last_positive_date"), found_date])
    payload.update(
        {
            "facility_ids": facility_ids,
            "facility_reasons": reasons,
            "positive_events": events,
            "new_positive_facility_ids": new_facility_ids,
            "last_positive_date": last_positive_date,
            "last_positive_by": actor.user_id,
        }
    )
    managed = (
        "facility_ids",
        "facility_reasons",
        "positive_events",
        "new_positive_facility_ids",
        "last_positive_date",
        "last_positive_by",
    )
    return {key: payload[key] for key in managed}


def zone_release_readiness(entity, today=None):
    """Compute whether a zone satisfies its release conditions."""
    payload = entity["data"]
    today = today or _today()
    last_positive = payload.get("last_positive_date")
    last_date = _as_date(last_positive) if last_positive else None
    days_since = (today - last_date).days if last_date else None
    wait_ok = days_since is not None and days_since >= ZONE_QUARANTINE_DAYS
    new_window = [
        event
        for event in payload.get("positive_events", [])
        if event.get("facility_id") in payload.get("new_positive_facility_ids", [])
        and (today - _as_date(event["date"])).days < ZONE_QUARANTINE_DAYS
    ]
    new_ok = not new_window
    earliest = (
        (last_date + timedelta(days=ZONE_QUARANTINE_DAYS)).isoformat()
        if last_date
        else None
    )
    return {
        "releaseable": wait_ok and new_ok,
        "days_since_last_positive": days_since,
        "required_days": ZONE_QUARANTINE_DAYS,
        "wait_days_ok": wait_ok,
        "new_positive_within_window": [event["facility_id"] for event in new_window],
        "no_new_positive_ok": new_ok,
        "earliest_release_date": earliest,
    }


def _validate_zone_lift(actor, entity, data, lookup):
    if entity["status"] != "active":
        raise InvalidTransition("zone is not active")
    readiness = zone_release_readiness(entity)
    problems = []
    if not readiness["wait_days_ok"]:
        problems.append(
            "latest positive was %s days ago, %s required"
            % (readiness["days_since_last_positive"], ZONE_QUARANTINE_DAYS)
        )
    if not readiness["no_new_positive_ok"]:
        problems.append(
            "new positive facilities within %s days: %s"
            % (ZONE_QUARANTINE_DAYS, ", ".join(readiness["new_positive_within_window"]))
        )
    if problems:
        raise ValidationError("zone release conditions not met: " + "; ".join(problems))
    return {"lifted_by": actor.user_id}


def _active_zone_ids(lookup, facility_id):
    if not lookup:
        return set()
    return {
        zone["id"]
        for zone in lookup("zone", "status", "active")
        if facility_id in zone["data"].get("facility_ids", [])
    }


def _validate_ship(actor, entity, data, lookup):
    patch = {}
    origin_id = entity["data"].get("origin_facility_id") or data.get("origin_facility_id")
    destination_id = data.get("destination_facility_id") or entity["data"].get(
        "destination_facility_id"
    )
    if not origin_id or not destination_id:
        raise ValidationError("origin_facility_id and destination_facility_id are required")
    if origin_id == destination_id:
        raise ValidationError("origin and destination must differ")
    if lookup and not _find_one(lookup, "facility", "id", destination_id):
        raise ValidationError("unknown facility: " + str(destination_id))
    blocked_zones = _active_zone_ids(lookup, origin_id)
    blocked_zones -= _active_zone_ids(lookup, destination_id)
    if blocked_zones:
        raise ValidationError(
            "consignment is blocked by active epidemic zone(s): " + ", ".join(sorted(blocked_zones))
        )
    patch["origin_facility_id"] = origin_id
    patch["destination_facility_id"] = destination_id
    patch["shipped_by"] = actor.user_id
    return patch


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


CUSTOM_CREATE = {'consignment': _validate_consignment, 'zone': _validate_zone_create}
CUSTOM_TRANSITIONS = {
    ('consignment', 'quarantine'): _validate_quarantine,
    ('consignment', 'release'): _validate_release,
    ('consignment', 'ship'): _validate_ship,
    ('zone', 'mark_positive'): _validate_zone_mark_positive,
    ('zone', 'lift'): _validate_zone_lift,
}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility', 'zones': 'zone'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered', 'zone': 'active'}
    TRANSITIONS = {
        'consignment': {
            'inspect': (('declared',), 'inspected'),
            'quarantine': (('inspected',), 'quarantined'),
            'release': (('inspected',), 'released'),
            'destroy': (('quarantined',), 'destroyed'),
            'recheck': (('quarantined',), 'inspected'),
            'ship': (('declared', 'released'), 'shipped'),
        },
        'facility': {'trace': (('registered',), 'traced')},
        'zone': {
            'mark_positive': (('active',), 'active'),
            'lift': (('active',), 'lifted'),
        },
    }
    CREATE_REQUIRED = {
        'consignment': ('code', 'origin', 'destination'),
        'facility': ('name', 'address'),
        'zone': ('zone_no', 'center_facility_id', 'facility_ids', 'reason'),
    }
    ACTION_REQUIRED = {
        ('consignment', 'inspect'): ('inspector', 'inspection_result'),
        ('consignment', 'quarantine'): ('pest_found', 'sample_id'),
        ('consignment', 'release'): ('pest_found', 'treatment'),
        ('consignment', 'destroy'): ('method', 'witnessed_by'),
        ('consignment', 'recheck'): ('sample_id',),
        ('facility', 'trace'): ('consignment_ids',),
        ('zone', 'mark_positive'): ('facility_id',),
    }
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine'), 'zone': ('admin', 'inspector')}
    ROLE_ACTIONS = {
        'inspect': ('admin', 'inspector'),
        'quarantine': ('admin', 'quarantine'),
        'release': ('admin', 'quarantine'),
        'destroy': ('admin', 'quarantine'),
        'recheck': ('admin', 'inspector'),
        'trace': ('admin', 'quarantine'),
        'ship': ('admin', 'inspector', 'quarantine'),
        'mark_positive': ('admin', 'inspector'),
        'lift': ('admin', 'inspector'),
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
