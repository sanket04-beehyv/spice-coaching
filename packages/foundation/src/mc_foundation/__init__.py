"""mc_foundation — shared config, logging, HTTP primitives, and infra protocols.

Allowed: settings base classes, logging setup, tracing utilities, generic HTTP
helpers, vendor-agnostic VectorStore / ObjectStore protocols and test doubles.
Not allowed: domain rules, DB models, repositories, vendor SDKs (e.g. pgvector, boto3).
"""
