"""Agent engine runtime.

Shared by the model gateway (model_gateway/) and the A2A workers (workers/):
  - models:        per-role model names (ENGINE_MODEL_<ROLE> → ENGINE_DEFAULT_MODEL)
  - ratelimit:     RPM limiter (in-process and Valkey-shared), bounded slots, daily counters
  - credentials:   Fernet encryption and validation of credential inputs
  - secrets:       secret backends: local Fernet or OpenBao (AppRole, KV v2, stale-if-error)
  - activity:      the X-Engine-Activity header (money per run, plan or chat)
  - deterministic: the fixed-procedure stand-in for a model (offline preset, last resort)
  - stepparams:    key=value step parameters (used by the deterministic procedures)
  - instructions:  each role's base instruction, and how the caller's addendum (A2) and the
                   agent team's (A3) are composed on top of it from the A2A metadata
"""

__version__ = "0.1.0"
