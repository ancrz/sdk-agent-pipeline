"""Agent engine runtime (extracted from vanguard-control's orchestrator package).

Shared by the model gateway (model_gateway/) and the A2A workers (workers/):
  - models:        per-role model names (VANGUARD_MODEL_<ROLE> → VANGUARD_DEFAULT_MODEL)
  - ratelimit:     RPM limiter (in-process and Valkey-shared), bounded slots, daily counters
  - credentials:   Fernet encryption and validation of credential inputs
  - secrets:       secret backends: local Fernet or OpenBao (AppRole, KV v2, stale-if-error)
  - activity:      the X-Vanguard-Activity header (money per run, plan or chat)
  - deterministic: the fixed-procedure stand-in for a model (offline preset, last resort)
  - stepparams:    key=value step parameters (used by the deterministic procedures)
  - instructions:  each role's base instruction, and how the caller's addendum (A2) and the
                   agent team's (A3) are composed on top of it from the A2A metadata

vanguard-control's planning modules (sdd_parser, swarm_planner, plangraph, playbooks) are its
Ansible domain and are not part of the engine.
"""

__version__ = "0.1.0"
