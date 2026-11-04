"""AWS Lambda runtime helpers. Nothing here runs locally or in tests: every function is a no-op off Lambda.

The API image runs on Lambda through the Lambda Web Adapter (uvicorn unchanged). Two things differ from a
long-running server:
- secrets come from SSM Parameter Store (SecureString), read once per cold start;
- background work can't outlive the response (Lambda freezes the process), so it runs as its own async
  invocation of the same function, which the adapter delivers as a POST to /events.
"""
from __future__ import annotations

import json
import os


def on_lambda() -> bool:
    return bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))


def load_ssm_params() -> int:
    """Copy every parameter under SSM_PARAMETER_PATH into the environment (name = last path segment)."""
    path = os.getenv("SSM_PARAMETER_PATH")
    if not path:
        return 0
    import boto3

    loaded = 0
    pages = boto3.client("ssm").get_paginator("get_parameters_by_path").paginate(Path=path, WithDecryption=True)
    for page in pages:
        for p in page["Parameters"]:
            os.environ.setdefault(p["Name"].rsplit("/", 1)[-1], p["Value"])
            loaded += 1
    return loaded


def invoke_self_async(payload: dict) -> None:
    """Queue an async invocation of this function; it arrives at POST /events. It goes to the alias traffic uses
    (LAMBDA_ALIAS), so it runs the live version with that alias's async settings (no retries)."""
    import boto3

    target = os.environ["AWS_LAMBDA_FUNCTION_NAME"]
    if os.getenv("LAMBDA_ALIAS"):
        target += ":" + os.environ["LAMBDA_ALIAS"]
    boto3.client("lambda").invoke(FunctionName=target, InvocationType="Event", Payload=json.dumps(payload).encode())
