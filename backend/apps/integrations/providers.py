"""
Integration adapters (plan §2.5, SRS §2.5).

Core code depends only on these interfaces. The concrete class is chosen via
settings (SMS_PROVIDER / EMAIL_PROVIDER), so Twilio/SendGrid can replace the
console stubs without touching business logic.
"""
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger("integrations")


class SMSProvider(ABC):
    @abstractmethod
    def send(self, to: str, body: str) -> None: ...


class EmailProvider(ABC):
    @abstractmethod
    def send(self, to: str, subject: str, body: str) -> None: ...


class ESignProvider(ABC):
    @abstractmethod
    def create_envelope(self, document, signers: list) -> str: ...


class PaymentProvider(ABC):
    @abstractmethod
    def charge(self, amount, currency: str, source: str) -> str: ...


class ListingFeedProvider(ABC):
    @abstractmethod
    def push(self, listing) -> None: ...


# --- Phase 1 console implementations (no third-party accounts needed) --------
class ConsoleSMSProvider(SMSProvider):
    def send(self, to: str, body: str) -> None:
        logger.info("[SMS] to=%s body=%s", to, body)


class ConsoleEmailProvider(EmailProvider):
    def send(self, to: str, subject: str, body: str) -> None:
        logger.info("[EMAIL] to=%s subject=%s body=%s", to, subject, body)
