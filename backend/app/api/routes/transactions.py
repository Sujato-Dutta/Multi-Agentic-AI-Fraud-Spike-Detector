"""Service-token-protected transaction ingestion."""

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request

from backend.app.core.runtime import AppError
from backend.app.core.security import require_service_token
from backend.app.schemas import TransactionBatchInput, TransactionInput

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.post("")
async def ingest_transaction(
    transaction: TransactionInput,
    request: Request,
    _: Annotated[None, Depends(require_service_token)],
) -> dict[str, Any]:
    trace_id = request.headers.get("x-trace-id", str(uuid4()))
    result = await request.app.state.transaction_service.ingest(
        transaction.model_dump(), trace_id
    )
    payload = result.to_dict()
    await request.app.state.websocket_hub.broadcast("txn", payload)
    return payload


@router.post("/batch")
async def ingest_batch(
    batch: TransactionBatchInput,
    request: Request,
    _: Annotated[None, Depends(require_service_token)],
) -> dict[str, Any]:
    if len(batch.transactions) > request.app.state.settings.max_ingest_batch_size:
        raise AppError("batch_too_large", 413, "Transaction batch exceeds configured limit")
    trace_id = request.headers.get("x-trace-id", str(uuid4()))
    batch_results = await request.app.state.transaction_service.ingest_batch(
        [transaction.model_dump() for transaction in batch.transactions], trace_id
    )
    results = [result.to_dict() for result in batch_results]
    await request.app.state.websocket_hub.broadcast(
        "metric_tick", request.app.state.transaction_service.stats()
    )
    return {
        "accepted": sum(item["created"] for item in results),
        "duplicates": sum(not item["created"] for item in results),
        "results": results,
    }
