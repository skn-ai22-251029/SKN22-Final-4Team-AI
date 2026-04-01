import logging
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

import boto3
from botocore.config import Config as BotoConfig

from config import settings

logger = logging.getLogger(__name__)

_BOTO_RETRY_CONFIG = BotoConfig(retries={"max_attempts": 5, "mode": "standard"})

_cached_credentials: Optional[dict] = None
_cached_credentials_expires_at: Optional[datetime] = None
_s3_client = None
_s3_client_access_key_id: Optional[str] = None


def _guess_content_type(filename: str, default: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or default


def _normalize_prefix(prefix: str) -> str:
    cleaned = (prefix or "").strip().strip("/")
    if not cleaned:
        raise ValueError("S3 prefix must not be empty")
    return cleaned


def _validate_settings() -> None:
    if not settings.media_s3_bucket:
        raise RuntimeError("MEDIA_S3_BUCKET is required")


def _static_credentials() -> dict:
    access_key = (settings.aws_access_key_id or "").strip()
    secret_key = (settings.aws_secret_access_key or "").strip()
    session_token = (settings.aws_session_token or "").strip()
    if not access_key or not secret_key:
        return {}

    creds = {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
    }
    if session_token:
        creds["aws_session_token"] = session_token
    return creds


def _should_assume_role() -> bool:
    role_arn = (settings.media_s3_role_arn or "").strip()
    if not role_arn:
        return False
    if ":role/" not in role_arn:
        logger.warning("[storage] MEDIA_S3_ROLE_ARN is not an IAM role ARN, falling back to direct credentials")
        return False
    return True


def _assume_role_credentials() -> dict:
    global _cached_credentials, _cached_credentials_expires_at

    _validate_settings()
    if not _should_assume_role():
        raise RuntimeError("MEDIA_S3_ROLE_ARN must be a valid IAM role ARN for AssumeRole")
    now = datetime.utcnow()
    if (
        _cached_credentials is not None
        and _cached_credentials_expires_at is not None
        and now < (_cached_credentials_expires_at - timedelta(minutes=5))
    ):
        return _cached_credentials

    sts = boto3.client(
        "sts",
        region_name=settings.media_s3_region,
        config=_BOTO_RETRY_CONFIG,
        **_static_credentials(),
    )
    assume_kwargs = {
        "RoleArn": settings.media_s3_role_arn,
        "RoleSessionName": settings.media_s3_role_session_name,
    }
    if settings.media_s3_external_id:
        assume_kwargs["ExternalId"] = settings.media_s3_external_id

    resp = sts.assume_role(**assume_kwargs)
    creds = resp["Credentials"]
    _cached_credentials = {
        "aws_access_key_id": creds["AccessKeyId"],
        "aws_secret_access_key": creds["SecretAccessKey"],
        "aws_session_token": creds["SessionToken"],
    }
    _cached_credentials_expires_at = creds["Expiration"].replace(tzinfo=None)
    logger.info("[storage] assumed role for cross-account S3, expires_at=%s", _cached_credentials_expires_at.isoformat())
    return _cached_credentials


def _get_s3_client():
    global _s3_client, _s3_client_access_key_id
    _validate_settings()
    creds = _assume_role_credentials() if _should_assume_role() else _static_credentials()
    access_key_id = creds.get("aws_access_key_id")
    if _s3_client is None or _s3_client_access_key_id != access_key_id:
        _s3_client = boto3.client(
            "s3",
            region_name=settings.media_s3_region,
            config=_BOTO_RETRY_CONFIG,
            **creds,
        )
        _s3_client_access_key_id = access_key_id
    return _s3_client


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    s3_uri: str
    presigned_url: str
    public_url: str
    size_bytes: int
    content_type: str


def build_object_key(prefix: str, filename: str) -> str:
    normalized_prefix = _normalize_prefix(prefix)
    normalized_name = (filename or "").strip().lstrip("/")
    if not normalized_name:
        raise ValueError("filename is required")
    return f"{normalized_prefix}/{normalized_name}"


def _build_public_url(bucket: str, key: str) -> str:
    return f"https://{bucket}.s3.{settings.media_s3_region}.amazonaws.com/{key}"


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    if not s3_uri or not s3_uri.startswith("s3://"):
        raise ValueError(f"invalid s3 uri: {s3_uri}")
    parsed = urlparse(s3_uri)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise ValueError(f"invalid s3 uri: {s3_uri}")
    return bucket, key


def put_bytes_and_presign(
    *,
    prefix: str,
    filename: str,
    content: bytes,
    content_type: Optional[str] = None,
) -> StoredObject:
    if content is None:
        raise ValueError("content is required")
    s3 = _get_s3_client()
    bucket = settings.media_s3_bucket
    key = build_object_key(prefix, filename)
    resolved_content_type = content_type or _guess_content_type(filename)

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=content,
        ContentType=resolved_content_type,
        ServerSideEncryption="AES256",
    )
    presigned = s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=settings.media_presign_expires_seconds,
    )
    return StoredObject(
        bucket=bucket,
        key=key,
        s3_uri=f"s3://{bucket}/{key}",
        presigned_url=presigned,
        public_url=_build_public_url(bucket, key),
        size_bytes=len(content),
        content_type=resolved_content_type,
    )


def presign_s3_uri(s3_uri: str, expires_seconds: Optional[int] = None) -> str:
    bucket, key = _parse_s3_uri(s3_uri)
    s3 = _get_s3_client()
    expire = expires_seconds if expires_seconds is not None else settings.media_presign_expires_seconds
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expire,
    )
