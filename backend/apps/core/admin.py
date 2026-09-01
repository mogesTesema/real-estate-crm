from django.contrib import admin

from .models import CustomField, Sequence

admin.site.register(CustomField)
admin.site.register(Sequence)
