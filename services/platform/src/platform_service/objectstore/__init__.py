"""Platform object-store adapters (boto3 S3 / MinIO today; more backends later)."""

from platform_service.objectstore.factory import create_object_store

__all__ = ["create_object_store"]
