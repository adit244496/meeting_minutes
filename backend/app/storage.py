"""Object storage with two interchangeable backends.

`local` writes to a directory on disk - no external dependency, nothing to
provision, and meeting audio never leaves the machine. This is the default.

`s3` targets AWS S3 or anything S3-compatible (MinIO, Wasabi, DigitalOcean
Spaces) for when storage needs to be shared across hosts or handed to a client's
own account.

Keys look like `meetings/<uuid>/source.m4a` in both backends, so switching means
changing one setting and copying files across - no code and no schema changes.
"""

from __future__ import annotations

import functools
import logging
import shutil
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# local filesystem
# --------------------------------------------------------------------------


def _root() -> Path:
    root = Path(settings.local_storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _local_path(key: str) -> Path:
    # Keys are built by us, never by users, but a traversal guard costs nothing
    # and this function turns a string into a filesystem write.
    clean = Path(key.replace("\\", "/"))
    if clean.is_absolute() or ".." in clean.parts:
        raise ValueError(f"unsafe storage key: {key!r}")
    return _root() / clean


# --------------------------------------------------------------------------
# S3
# --------------------------------------------------------------------------


@functools.lru_cache
def s3():
    import boto3
    from botocore.client import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4"),
    )


def _using_s3() -> bool:
    return settings.storage_backend.lower() == "s3"


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def ensure_ready() -> None:
    """Verify storage is usable at startup."""
    if not _using_s3():
        _root()
        log.info("Storage: local filesystem at %s", settings.local_storage_dir)
        return

    from botocore.exceptions import ClientError

    client = s3()
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
        log.info("Storage: S3 bucket %s", settings.s3_bucket)
        return
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 403:
            # Bucket exists but a tightly scoped policy forbids inspecting it.
            # That is the expected result under the least-privilege policy we
            # ask clients for, not an error.
            log.info("Storage: S3 bucket %s (not inspectable)", settings.s3_bucket)
            return
        if status != 404:
            raise

    client.create_bucket(Bucket=settings.s3_bucket)
    log.info("Storage: created S3 bucket %s", settings.s3_bucket)


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    if _using_s3():
        s3().put_object(Bucket=settings.s3_bucket, Key=key, Body=data, ContentType=content_type)
        return key

    dest = _local_path(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return key


def put_file(key: str, path: Path, content_type: str = "application/octet-stream") -> str:
    if _using_s3():
        s3().upload_file(str(path), settings.s3_bucket, key, ExtraArgs={"ContentType": content_type})
        return key

    dest = _local_path(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, dest)
    return key


def download_to(key: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if _using_s3():
        s3().download_file(settings.s3_bucket, key, str(dest))
        return dest

    source = _local_path(key)
    if not source.exists():
        raise FileNotFoundError(f"stored object not found: {key}")
    shutil.copyfile(source, dest)
    return dest


def open_stream(key: str):
    """Return a file-like object for streaming a stored object to a client."""
    if _using_s3():
        return s3().get_object(Bucket=settings.s3_bucket, Key=key)["Body"]

    source = _local_path(key)
    if not source.exists():
        raise FileNotFoundError(f"stored object not found: {key}")
    return source.open("rb")


def exists(key: str) -> bool:
    if _using_s3():
        from botocore.exceptions import ClientError

        try:
            s3().head_object(Bucket=settings.s3_bucket, Key=key)
            return True
        except ClientError:
            return False
    return _local_path(key).exists()


def size_bytes(key: str) -> int | None:
    try:
        if _using_s3():
            return int(s3().head_object(Bucket=settings.s3_bucket, Key=key)["ContentLength"])
        return _local_path(key).stat().st_size
    except Exception:  # noqa: BLE001
        return None


def delete(key: str) -> bool:
    """Remove a stored object. Returns False if it was already gone."""
    if _using_s3():
        from botocore.exceptions import ClientError

        try:
            s3().delete_object(Bucket=settings.s3_bucket, Key=key)
            return True
        except ClientError:
            log.warning("Could not delete %s from S3", key, exc_info=True)
            return False

    path = _local_path(key)
    if not path.exists():
        return False
    path.unlink()
    # Tidy up the now-empty per-meeting directory.
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return True
