from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request


def get_idempotency_key(
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> str | None:
    supplied = any(
        name.lower() == b"idempotency-key"
        for name, _ in request.scope.get("headers", ())
    )
    if not supplied:
        return None
    candidate = idempotency_key or ""
    if (
        len(candidate) > 255
        or not candidate.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        raise HTTPException(
            status_code=422,
            detail=[{
                "type": "value_error",
                "loc": ["header", "Idempotency-Key"],
                "msg": "Idempotency-Key must contain 1-255 non-control characters.",
                "input": candidate,
            }],
        )
    return candidate


IdempotencyKeyHeader = Annotated[str | None, Depends(get_idempotency_key)]
