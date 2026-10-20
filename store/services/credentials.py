"""Credential administration — provider keys for the model gateway.

Inputs go to the
configured secret backend (orchestrator/secrets.py: local Fernet, or OpenBao where the writer
AppRole can write but not read) and are never returned. model_provider credentials are read
only by the model gateway."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator import secrets
from orchestrator.credentials import check_inputs, public_summary

from ..models.automation import Credential

logger = logging.getLogger(__name__)


async def store(db: AsyncSession, name: str, credential_type: str, inputs: dict[str, Any], actor: str,
                existing: Optional[Credential] = None) -> Credential:
    check_inputs(credential_type, inputs)
    stored = await secrets.aput(credential_type, name, inputs)
    summary = public_summary(credential_type, inputs)
    if existing is None:
        existing = Credential(name=name, credential_type=credential_type, summary=summary)
        db.add(existing)
        operation = "create"
    else:
        existing.credential_type = credential_type
        existing.summary = summary
        operation = "update"
    existing.backend, existing.secret_ref, existing.inputs_encrypted = stored.backend, stored.ref, stored.ciphertext
    # Names and field names only, never values.
    logger.info("credential %s %s by %s (backend %s, fields %s)", operation, name, actor, stored.backend, sorted(inputs))
    return existing
