"""Base serializers, including custom-field validation (plan §2.3)."""
from rest_framework import serializers

from .models import FieldDefinition


class CustomFieldsValidationMixin:
    """
    Validates the ``custom_fields`` JSON blob against the tenant's FieldDefinition
    rows for this model. Unknown keys and missing required keys are rejected;
    select/multiselect values are checked against allowed options.

    Serializers set ``custom_fields_model_label`` (e.g. "contacts.Contact").
    """

    custom_fields_model_label: str | None = None

    def validate_custom_fields(self, value):
        label = self.custom_fields_model_label
        if not label:
            return value
        value = value or {}
        defs = {
            d.key: d
            for d in FieldDefinition.objects.filter(target_model=label)
        }
        # No tenant field defs yet — allow free-form JSON (image_url, media_order, etc.).
        if not defs:
            return value
        system_keys = {"image_url", "media_order", "cover_media_id"}
        errors = {}
        for key in value:
            if key not in defs and key not in system_keys:
                errors[key] = "Unknown custom field."
        for key, d in defs.items():
            if d.required and key not in value:
                errors[key] = "This custom field is required."
                continue
            if key not in value:
                continue
            if d.field_type == FieldDefinition.FieldType.SELECT:
                if value[key] not in d.options:
                    errors[key] = f"Must be one of {d.options}."
            elif d.field_type == FieldDefinition.FieldType.MULTISELECT:
                vals = value[key] if isinstance(value[key], list) else [value[key]]
                bad = [v for v in vals if v not in d.options]
                if bad:
                    errors[key] = f"Invalid options: {bad}."
        if errors:
            raise serializers.ValidationError(errors)
        return value
