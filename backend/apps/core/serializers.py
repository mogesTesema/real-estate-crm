"""Serializers shared across apps' HTTP layers.

Deliberately **not** under `apps/core/api/`. §1.2 makes every app's `api` package private and
`lint-imports` enforces it, so a serializer that several apps need cannot live in one — but
`core` is the kernel every app may import, and this module sits outside its `api` package for
exactly that reason. `pagination.py` and `exceptions.py` are here on the same logic.

The alternative — a copy of `UserSummarySerializer` per app — is what this replaces. Beyond
the duplication, drf-spectacular registers components by class name, so N identical copies
collide into one component with a warning that the schema is probably wrong.
"""
from django.contrib.auth import get_user_model
from rest_framework import serializers


class UserSummarySerializer(serializers.ModelSerializer):
    """A person, as much of them as a foreign key on someone else's record needs.

    `get_user_model()` rather than importing `apps.identity.models`: `core` is the bottom of
    the import DAG and depends on no other app (§1.2). The lookup is a runtime setting read,
    not an import edge.
    """

    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = get_user_model()
        fields = ("id", "full_name", "email")
