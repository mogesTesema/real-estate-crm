from rest_framework import serializers

from apps.core.serializers import CustomFieldsValidationMixin

from .models import Listing, ListingMedia, ListingStatusHistory, Property


class PropertySerializer(CustomFieldsValidationMixin, serializers.ModelSerializer):
    custom_fields_model_label = "properties.Property"

    class Meta:
        model = Property
        fields = [
            "id",
            "node_type",
            "parent",
            "category",
            "title",
            "description",
            "address_line",
            "city",
            "region",
            "country",
            "postal_code",
            "latitude",
            "longitude",
            "bedrooms",
            "bathrooms",
            "area_built",
            "area_carpet",
            "year_built",
            "floor",
            "unit_number",
            "parking",
            "amenities",
            "owner",
            "custom_fields",
            "created_at",
        ]
        read_only_fields = ["created_at"]


class ListingMediaSerializer(serializers.ModelSerializer):
    class Meta:
        model = ListingMedia
        fields = ["id", "listing", "kind", "file", "url", "caption"]


class ListingStatusHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ListingStatusHistory
        fields = ["id", "from_status", "to_status", "changed_by", "reason", "at"]


class ListingSerializer(CustomFieldsValidationMixin, serializers.ModelSerializer):
    custom_fields_model_label = "properties.Listing"
    media = ListingMediaSerializer(many=True, read_only=True)
    status_history = ListingStatusHistorySerializer(many=True, read_only=True)

    class Meta:
        model = Listing
        fields = [
            "id",
            "property",
            "listing_type",
            "status",
            "mandate",
            "price",
            "currency",
            "listing_agent",
            "listed_at",
            "expires_at",
            "media",
            "status_history",
            "custom_fields",
            "created_at",
        ]
        # Status is changed only via the /transition action (state machine).
        read_only_fields = ["status", "created_at"]


class TransitionSerializer(serializers.Serializer):
    to_status = serializers.ChoiceField(choices=[])
    reason = serializers.CharField(required=False, allow_blank=True)

    def __init__(self, *args, **kwargs):
        from .models import ListingStatus

        super().__init__(*args, **kwargs)
        self.fields["to_status"].choices = ListingStatus.choices
