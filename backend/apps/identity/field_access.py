"""Field-level permissions (SRS 3.17.2): who may see or edit which columns.

`field_rules(user, entity_type)` folds the user's roles' `FieldPermission` rows into one
{field_name: access_level} dict, **most-permissive wins** — the same OR-not-rank doctrine
as data scopes: two roles are two grants, and the wider one is the answer. Absence of rows
means READ_WRITE, i.e. exactly today's behavior — the feature costs nothing until an admin
writes rows.

`FieldPermissionSerializerMixin` applies the rules at the serializer boundary: HIDDEN
fields disappear from representations; HIDDEN and READ_ONLY fields are silently dropped
from writes (DRF's own read_only convention — clients are not punished for sending a field
the server chooses to ignore).
"""
from .models import FieldPermission

_RANK = {"HIDDEN": 0, "READ_ONLY": 1, "READ_WRITE": 2}


def field_rules(user, entity_type):
    """{field_name: access_level} for this user on this entity type. Superusers and users
    with no rows get the empty dict — everything READ_WRITE."""
    if user is None or not user.is_authenticated or user.is_superuser:
        return {}
    cache = getattr(user, "_field_rules_cache", None)
    if cache is None:
        cache = user._field_rules_cache = {}
    if entity_type in cache:
        return cache[entity_type]

    rules = {}
    rows = FieldPermission.objects.filter(
        role__user_roles__user=user, entity_type=entity_type
    ).values_list("field_name", "access_level")
    for field_name, level in rows:
        current = rules.get(field_name)
        if current is None or _RANK[level] > _RANK[current]:
            rules[field_name] = level
    # A field every one of the user's roles restricts stays restricted; a field any role
    # leaves unmentioned is unmentioned — unmentioned means READ_WRITE, so drop it.
    rules = {name: level for name, level in rules.items() if level != "READ_WRITE"}
    cache[entity_type] = rules
    return rules


class FieldPermissionSerializerMixin:
    """Set `field_permission_entity = ScopedEntityType.X` on the serializer."""

    field_permission_entity = None

    def _rules(self):
        request = self.context.get("request")
        if request is None or self.field_permission_entity is None:
            return {}
        return field_rules(request.user, self.field_permission_entity)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        for name, level in self._rules().items():
            if level == "HIDDEN":
                data.pop(name, None)
        return data

    def to_internal_value(self, data):
        rules = self._rules()
        if rules and hasattr(data, "items"):
            data = {
                key: value
                for key, value in data.items()
                if rules.get(key) not in ("HIDDEN", "READ_ONLY")
            }
        return super().to_internal_value(data)
