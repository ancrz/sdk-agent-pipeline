"""The model gateway's tables.

- Credential: a provider key; inputs in OpenBao (secret_ref) or Fernet-encrypted with
  ENGINE_CREDENTIALS_KEY (inputs_encrypted). Never plaintext, never returned.
- AiPreset / AppSetting: the named sets of model profiles and the active one.
- ModelProfile: which model and credential a role uses in a preset.
- ModelCall: the audit row of every call through the gateway.
- ModelPrice: the price table that stamps each call's cost.
- AgentTeam: a named lineup of the worker agents: per role an optional model the
  gateway puts first, and an addendum the caller sends to the worker.
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, false, func
from sqlalchemy.dialects.postgresql import UUID

from .activity import Base

CREDENTIAL_TYPES = ("machine", "vault")


class Credential(Base):
    __tablename__ = "credentials"
    __table_args__ = (
        CheckConstraint("backend IN ('local', 'openbao')", name="ck_credentials_backend"),
        CheckConstraint("(backend = 'local' AND inputs_encrypted IS NOT NULL) OR "
                        "(backend = 'openbao' AND secret_ref IS NOT NULL)", name="ck_credentials_stored"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(128), nullable=False, unique=True)
    credential_type = Column(String(16), nullable=False)
    # Where the inputs live (orchestrator/secrets.py): local = Fernet ciphertext in
    # inputs_encrypted; openbao = KV v2 at secret_ref. Never plaintext.
    backend = Column(String(16), nullable=False, default="local", server_default="local")
    secret_ref = Column(String(255), nullable=True)
    inputs_encrypted = Column(Text, nullable=True)
    # Non-secret facts shown in the Vault tab (provider, key last 4, fingerprint); never inputs.
    summary = Column(JSON, nullable=False, default=dict)
    # Models the provider lists for this key (fetched by the gateway, which holds the key).
    model_catalog = Column(JSON, nullable=True)
    validated_at = Column(DateTime(timezone=True), nullable=True)
    # What the key is for (shown in the UI; the failover chain itself is ai.failover).
    description = Column(String(255), nullable=True)
    purpose = Column(String(16), nullable=True)   # primary | backup | embeddings | local | testing | other
    # The environments (inventory ids) where a
    # machine/windows credential may run; empty = any. The Controller rechecks it at every job.
    allowed_inventory_ids = Column(JSON, nullable=False, default=list, server_default="[]")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


MODEL_ROLES = ("default", "orchestrator", "executor", "validator", "documentalist", "author", "medic", "embeddings")
PRESET_KINDS = ("offline", "routed")
ACTIVE_PRESET_KEY = "ai.active_preset"


class AiPreset(Base):
    """A named set of model profiles. The active one (app_settings
    "ai.active_preset") is what every role resolves in; switching needs no restart.
    kind "offline": the gateway answers with the deterministic stand-in."""
    __tablename__ = "ai_presets"
    __table_args__ = (CheckConstraint("kind IN ('offline', 'routed')", name="ck_ai_presets_kind"),)

    name = Column(String(32), primary_key=True)
    kind = Column(String(16), nullable=False)
    description = Column(Text, nullable=True)
    builtin = Column(Boolean, nullable=False, default=False, server_default=false())


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(JSON, nullable=False)
    updated_by = Column(String(128), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ModelProfile(Base):
    """Which model (and provider credential from the Vault) a role uses in a preset.

    Resolution, in the active preset: the role's profile → the preset's `default`
    profile → .env (GOOGLE_API_KEY, ENGINE_MODEL_<ROLE>).
    """
    __tablename__ = "model_profiles"

    preset = Column(String(32), ForeignKey("ai_presets.name", ondelete="CASCADE", name="fk_model_profiles_preset"),
                    primary_key=True)
    role = Column(String(32), primary_key=True)
    credential_id = Column(UUID(as_uuid=True), ForeignKey("credentials.id"), nullable=True)
    model = Column(String(128), nullable=False)
    fallback_model = Column(String(128), nullable=True)
    rpm = Column(Integer, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    updated_by = Column(String(128), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ModelCall(Base):
    """One model call through the model gateway (audit: who, which model, outcome, tokens)."""
    __tablename__ = "model_calls"
    __table_args__ = (Index("ix_model_calls_created", "created_at"), Index("ix_model_calls_role", "role"),
                      Index("ix_model_calls_activity", "activity_kind", "activity_id"))

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role = Column(String(32), nullable=False)
    requested_model = Column(String(128), nullable=True)
    model = Column(String(128), nullable=False)
    provider = Column(String(16), nullable=False)
    method = Column(String(48), nullable=False)
    status_code = Column(Integer, nullable=False)
    fallback_used = Column(Boolean, nullable=False, default=False)
    account = Column(String(128), nullable=True)     # the credential that answered (failover)
    prompt_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=False)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Money per activity: stamped when the call is written, never rewritten.
    activity_kind = Column(String(16), nullable=True)     # run | plan | chat | index | eda
    activity_id = Column(UUID(as_uuid=True), nullable=True)
    thoughts_tokens = Column(Integer, nullable=True)
    cached_tokens = Column(Integer, nullable=True)
    cost_usd = Column(Numeric(18, 10, asdecimal=False), nullable=True)        # billed (0 on a free tier)
    list_cost_usd = Column(Numeric(18, 10, asdecimal=False), nullable=True)   # the same tokens at the paid price
    price_id = Column(UUID(as_uuid=True), ForeignKey("model_prices.id", ondelete="SET NULL"), nullable=True)


class ModelPrice(Base):
    """A model's price from a date on, per 1M tokens in USD. The price in
    force for a call is the newest row with effective_from <= the call's time."""
    __tablename__ = "model_prices"
    __table_args__ = (UniqueConstraint("provider", "model", "effective_from", name="uq_model_prices_from"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = Column(String(16), nullable=False)
    model = Column(String(128), nullable=False)
    effective_from = Column(DateTime(timezone=True), nullable=False)
    input_per_mtok = Column(Numeric(14, 6, asdecimal=False), nullable=False)
    output_per_mtok = Column(Numeric(14, 6, asdecimal=False), nullable=False)
    cached_input_per_mtok = Column(Numeric(14, 6, asdecimal=False), nullable=True)   # null: the input price
    thinking_per_mtok = Column(Numeric(14, 6, asdecimal=False), nullable=True)       # null: the output price
    source = Column(String(16), nullable=False, default="manual")                    # litellm | google | manual
    updated_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentTeam(Base):
    """A named lineup of the worker agents, chosen
    per change in the chat: for each tunable role, an optional model (put first by the gateway) and an
    addendum (after the operator's). Never deleted: an inactive team leaves the selector. `basic` is built in."""
    __tablename__ = "agent_teams"
    __table_args__ = (UniqueConstraint("name", name="uq_agent_teams_name"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(32), nullable=False)
    description = Column(String(500), nullable=False, default="")
    members = Column(JSON, nullable=False, default=dict)          # {role: {"model": str|None, "addendum": str}}
    builtin = Column(Boolean, nullable=False, default=False)
    active = Column(Boolean, nullable=False, default=True)
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    created_by = Column(String(64), nullable=False)
    updated_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __mapper_args__ = {"version_id_col": revision}
