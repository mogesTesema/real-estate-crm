"""File storage (architecture.md §12, SRS 3.7.1) — the first code that can put a byte in
object storage.

The schema shipped with `collaboration_file` as the target of every deferred `*_file_id`
FK (avatars, logos, inventory media, call recordings) and docker-compose ships MinIO with
signed URLs enabled — but until now nothing wrote to either. `store_file` is the single
ingestion point; everything that carries a file (documents, media, recordings) references a
`File` created here.
"""
import hashlib
import logging
import mimetypes
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage, default_storage
from django.db import transaction
from django.utils import timezone

from ..models import File

logger = logging.getLogger(__name__)


def _max_bytes():
    return getattr(settings, "FILE_UPLOAD_MAX_BYTES", 25 * 1024 * 1024)


def _allowed_prefixes():
    return getattr(
        settings,
        "FILE_UPLOAD_ALLOWED_MIME_PREFIXES",
        [
            "image/",
            "application/pdf",
            "video/",
            "audio/",
            "text/",
            "application/msword",
            "application/vnd.openxmlformats-officedocument",
        ],
    )


@transaction.atomic
def store_file(*, actor, uploaded_file, is_encrypted=False) -> File:
    """Persist an upload: validate size and type, stream a sha256, save, record.

    The checksum is computed by streaming `chunks()` — the file is never held in memory
    whole — and stored so later passes get de-duplication and integrity verification for
    free (SRS 3.7.5's audit story leans on it: the hash in a signature event is only
    meaningful if the stored file carries one to compare against).
    """
    size = getattr(uploaded_file, "size", None) or 0
    if size <= 0:
        raise ValidationError({"file": "The uploaded file is empty."})
    if size > _max_bytes():
        raise ValidationError(
            {"file": f"File is {size} bytes; the limit is {_max_bytes()} bytes."}
        )

    mime = getattr(uploaded_file, "content_type", "") or ""
    if not mime:
        mime = mimetypes.guess_type(uploaded_file.name or "")[0] or ""
    if not mime or not any(mime.startswith(prefix) for prefix in _allowed_prefixes()):
        raise ValidationError({"file": f"Unsupported file type {mime or 'unknown'!r}."})

    digest = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        digest.update(chunk)
    uploaded_file.seek(0)

    original_name = Path(uploaded_file.name or "upload").name[:200] or "upload"
    now = timezone.now()
    key = f"files/{now:%Y/%m}/{uuid.uuid4().hex}/{original_name}"
    # save()'s return value is authoritative: a storage backend may rename to avoid
    # collisions, and recording the requested key instead would orphan the blob.
    storage_key = default_storage.save(key, uploaded_file)

    file = File.objects.create(
        original_name=original_name,
        storage_key=storage_key,
        mime_type=mime[:127],
        size_bytes=size,
        checksum=digest.hexdigest(),
        uploaded_by=actor,
        is_encrypted=is_encrypted,
    )
    _audit_file_created(file, actor)
    return file


def _audit_file_created(file, actor):
    # Lazy import: collaboration -> platform.services is a legal DAG edge, but keeping it
    # out of module scope keeps import order boring.
    from apps.platform.services import record_event

    record_event(
        action="CREATE",
        entity_type="FILE",
        entity_id=file.pk,
        actor=actor,
        new_values={
            "original_name": file.original_name,
            "size_bytes": file.size_bytes,
            "checksum": file.checksum,
        },
    )


def download_target(file) -> dict:
    """How to hand the bytes to a client: a signed URL (S3/MinIO) or a local stream.

    Detected from the storage *instance*, not settings — test and dev configurations vary,
    and the storage object is the single source of truth for what it can do.
    """
    if isinstance(default_storage, FileSystemStorage):
        return {"kind": "stream"}
    return {"kind": "url", "url": default_storage.url(file.storage_key)}


def open_file(file):
    """The raw stream, for the filesystem-storage path and for hashing at signature time."""
    return default_storage.open(file.storage_key)


def file_sha256(file) -> str:
    """Re-hash the stored bytes — the e-sign completion fingerprint."""
    digest = hashlib.sha256()
    with open_file(file) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
