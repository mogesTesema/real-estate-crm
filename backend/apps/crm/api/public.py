"""Public, unauthenticated lead capture (SRS 3.1.1 "Website", 3.8.2 landing pages).

Anti-abuse posture:
* honeypot `website` field — bots fill it; a non-blank value returns the same 202 and
  nothing happens (silence teaches the bot nothing);
* an invalid listing id also returns the generic 202 — no enumeration oracle;
* everything sits behind tight ScopedRateThrottle scopes.
A valid submission runs the full `capture_lead` pipeline: dedupe, scoring, routing, SLA
clock, acknowledgment — SRS 3.1's guarantees hold for the public door too.
"""
import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import F
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .. import services
from ..models import LandingPage, Lead, LeadSource

logger = logging.getLogger(__name__)

ACCEPTED = {"detail": "Thank you — we will be in touch shortly."}


class PublicBase(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]


class InquirySerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    first_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=50, required=False, allow_blank=True)
    message = serializers.CharField(required=False, allow_blank=True)
    listing = serializers.UUIDField(required=False, allow_null=True)
    # The honeypot. Humans never see it; bots helpfully fill it.
    website = serializers.CharField(required=False, allow_blank=True)


class PublicInquiryView(PublicBase):
    throttle_scope = "public_inquiry"

    @extend_schema(auth=[], request=InquirySerializer, responses={202: dict})
    def post(self, request):
        payload = InquirySerializer(data=request.data)
        if not payload.is_valid():
            # A malformed inquiry is still a 202 unless it is missing all contact routes —
            # detailed validation errors are an enumeration and probing aid.
            return Response(ACCEPTED, status=status.HTTP_202_ACCEPTED)
        data = payload.validated_data
        if data.get("website"):
            return Response(ACCEPTED, status=status.HTTP_202_ACCEPTED)  # honeypot
        if not (data.get("email") or data.get("phone")):
            return Response(
                {"detail": "Leave an email or a phone number so we can reach you."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        listing = None
        if data.get("listing"):
            from apps.inventory.models import Listing

            listing = Listing.objects.filter(
                pk=data["listing"], status=Listing.Status.ACTIVE,
                deleted_at__isnull=True,
            ).select_related("property").first()
            # Unknown/inactive id: proceed without it — same 202, no oracle.

        name = (data.get("name") or "").strip()
        first = data.get("first_name") or (name.split(" ")[0] if name else "Web")
        last = data.get("last_name") or (
            " ".join(name.split(" ")[1:]) if name and " " in name else "Visitor"
        )
        lead_type = Lead.LeadType.BUY
        if listing is not None and listing.listing_type == "RENT":
            lead_type = Lead.LeadType.RENT_IN

        source, _ = LeadSource.objects.get_or_create(
            name="Website", defaults={"source_type": "WEBSITE"}
        )
        try:
            services.capture_lead(
                actor=None,
                contact_data={
                    "contact_type": "PERSON",
                    "first_name": first,
                    "last_name": last,
                    "email": data.get("email") or None,
                    "phone": data.get("phone") or None,
                },
                lead_type=lead_type,
                source=source,
                target_property=listing.property if listing else None,
                description=data.get("message") or None,
            )
        except DjangoValidationError:
            logger.warning("public inquiry failed validation", exc_info=True)
            # Still a 202: the visitor cannot fix a back-office rule, and telling an
            # attacker which inputs break the pipeline is a probing aid.
        return Response(ACCEPTED, status=status.HTTP_202_ACCEPTED)


class PublicLandingPageView(PublicBase):
    throttle_scope = "public_page"

    @extend_schema(auth=[], responses={200: dict})
    def get(self, request, slug):
        page = LandingPage.objects.filter(slug=slug, is_published=True).first()
        if page is None:
            from rest_framework.exceptions import NotFound

            raise NotFound
        LandingPage.objects.filter(pk=page.pk).update(views=F("views") + 1)
        return Response(
            {
                "slug": page.slug,
                "title": page.title,
                "content": page.content,
                "form": {
                    "fields": (page.form_config or {}).get("fields", []),
                },
            }
        )


class PublicLandingPageSubmitView(PublicBase):
    throttle_scope = "public_inquiry"

    @extend_schema(auth=[], request=dict, responses={202: dict})
    def post(self, request, slug):
        page = LandingPage.objects.filter(slug=slug, is_published=True).first()
        if page is None:
            from rest_framework.exceptions import NotFound

            raise NotFound
        if (request.data or {}).get("website"):
            return Response(ACCEPTED, status=status.HTTP_202_ACCEPTED)  # honeypot
        try:
            services.submit_landing_page(
                page, form_data={k: v for k, v in (request.data or {}).items()}
            )
        except DjangoValidationError as exc:
            payload = exc.message_dict if hasattr(exc, "message_dict") else {
                "detail": exc.messages
            }
            # Form validation IS shown: the page's own form defined these fields, so
            # echoing them helps the human without teaching a bot anything new.
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)
        return Response(ACCEPTED, status=status.HTTP_202_ACCEPTED)


class PublicChatQualifyView(PublicBase):
    """`POST /public/chat/qualify/` — stateless scripted qualification (SRS 3.20.4).

    The client round-trips `state`; when the script completes, the collected slots run
    the normal capture pipeline. Honeypot + throttle, same doctrine as the inquiry door.
    """

    throttle_scope = "public_chat"

    @extend_schema(auth=[], request=dict, responses={200: dict})
    def post(self, request):
        from ..ai import get_provider

        data = request.data or {}
        if data.get("website"):  # honeypot
            return Response({"done": True, "reply": "Thank you!", "state": {}})
        result = get_provider().qualify_chat(
            state=data.get("state"), message=data.get("message", "")
        )
        if result.get("done") and not result["state"].get("_captured"):
            state = result["state"]
            name = (state.get("name") or "").strip()
            first = name.split(" ")[0] if name else "Chat"
            last = " ".join(name.split(" ")[1:]) if " " in name else "Visitor"
            source, _ = LeadSource.objects.get_or_create(
                name="Website chat", defaults={"source_type": "WEBSITE"}
            )
            lead_type = (
                Lead.LeadType.RENT_IN
                if "rent" in (state.get("intent") or "").lower()
                else Lead.LeadType.BUY
            )
            try:
                services.capture_lead(
                    actor=None,
                    contact_data={
                        "contact_type": "PERSON", "first_name": first,
                        "last_name": last, "email": state.get("email") or None,
                    },
                    lead_type=lead_type,
                    source=source,
                    description=(
                        f"Chat-qualified. Budget: {state.get('budget')}; "
                        f"area: {state.get('location')}."
                    ),
                )
                result["state"] = {**state, "_captured": True}
            except DjangoValidationError:
                logger.warning("chat qualify capture failed", exc_info=True)
        return Response(result)
