from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

import boto3


@lru_cache(maxsize=4)
def get_runtime_secret(secret_arn: str | None = None) -> dict[str, Any]:
    arn = secret_arn or os.environ.get("RUNTIME_SECRET_ARN", "")
    if not arn:
        raise RuntimeError("RUNTIME_SECRET_ARN is not configured")
    region = os.environ.get("AWS_REGION", "eu-central-1")
    response = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=arn)
    payload = json.loads(response.get("SecretString") or "{}")
    if not isinstance(payload, dict):
        raise RuntimeError("Runtime secret must contain a JSON object")
    return payload

