"""Invoke a model only after durable admission; reuse saved responses after restart."""

import asyncio
from contextlib import suppress

from visualization_runtime.providers import ProviderError, complete
from visualization_runtime.snapshots import digest
from visualization_runtime.store import JobConflict


class LedgerProvider:
    def __init__(self, ledger, config, *, transport=None):
        self.ledger, self.config, self.transport = ledger, config, transport

    async def complete(
        self,
        task_id,
        worker,
        generation,
        *,
        call_key,
        messages,
        reserved_tokens,
        max_tokens=1200,
        json_object=False,
        json_schema=None,
    ):
        if (
            type(max_tokens) is not int
            or not 1 <= max_tokens <= 8192
            or type(reserved_tokens) is not int
            or reserved_tokens < max_tokens
        ):
            raise ValueError("invalid_provider_output_reservation")
        fingerprint = digest(
            {
                "model": self.config.model,
                "base_url": self.config.base_url,
                "messages": messages,
                "max_tokens": max_tokens,
                "json_object": json_object,
                **({"json_schema": json_schema} if json_schema is not None else {}),
            }
        )
        row = self.ledger.reserve_call(
            task_id,
            worker,
            generation,
            call_key=call_key,
            role=self.config.role,
            fingerprint=fingerprint,
            tokens=reserved_tokens,
        )
        if row["state"] == "succeeded":
            return self.ledger.saved_completion(row)
        remaining = self.ledger.start_call(task_id, worker, generation, row["call_id"])
        try:
            response = await complete(
                self.config.model_copy(
                    update={
                        "timeout_seconds": max(0.001, min(self.config.timeout_seconds, remaining))
                    }
                ),
                messages,
                max_tokens=max_tokens,
                json_object=json_object,
                json_schema=json_schema,
                transport=self.transport,
            )
        except ProviderError as error:
            self.ledger.finish_call(
                task_id,
                worker,
                generation,
                row["call_id"],
                error_code=error.code,
                unknown=error.unknown,
                usage=error.usage,
            )
            raise
        except asyncio.CancelledError:
            with suppress(JobConflict):
                self.ledger.finish_call(
                    task_id,
                    worker,
                    generation,
                    row["call_id"],
                    error_code="provider_cancelled",
                    unknown=True,
                )
            raise
        self.ledger.finish_call(task_id, worker, generation, row["call_id"], completion=response)
        return response
