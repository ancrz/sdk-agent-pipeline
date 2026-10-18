"""Engine administration CLI: provider keys, model profiles, presets (what vanguard-control's
API and its AI Gateway screen do, without a UI).

    python -m store.admin status
    python -m store.admin preset activate paid
    echo -n "$KEY" | python -m store.admin credential put gemini-main --provider gemini --tier paid
    python -m store.admin profile set default --model gemini-3.5-flash-lite --credential gemini-main --preset paid
    python -m store.admin profile set orchestrator --model gemini-3.5-pro --credential gemini-main --preset paid
    python -m store.admin team set sql-scripts --role executor --model gemini-3.5-flash --addendum "Prefer psql."
    python -m store.admin run new nightly-report --team sql-scripts      # prints the run id

A key is read from stdin, never from argv (it would show in the process list). It goes to the
configured backend (VANGUARD_SECRET_BACKEND): `openbao` with the writer AppRole (write-only:
it cannot read the key back) or `local` (Fernet with VANGUARD_CREDENTIALS_KEY). Run it inside
the `admin` service of docker-compose.yml, which has the database and the writer AppRole.
A preset switch or a profile change takes effect on the gateway's next call (no restart).

Agent teams (G-AGENTS A3): a run created with --team makes the gateway put the team's model first
for each role the team names, when the worker's calls carry X-Vanguard-Activity run:<id> (send
`vanguard_activity` in the A2A metadata). The team's addendum is sent by the caller in the A2A
metadata too (`vanguard_instructions`, orchestrator/instructions.py); `run new` prints it ready to send.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from orchestrator.credentials import MODEL_PROVIDERS, TIERS, CredentialError

import json

from .models.activity import ActivityRun, RunStatus
from .models.automation import MODEL_ROLES, AgentTeam, Credential, ModelCall, ModelProfile
from .schema import database_url
from .services import credentials as svc
from .services import presets

ACTOR = "cli"


async def _credential_put(db, args) -> str:
    key = sys.stdin.read().strip()
    inputs = {"provider": args.provider}
    if key:
        inputs["api_key"] = key
    for field in ("tier", "base_url", "project", "location"):
        value = getattr(args, field)
        if value:
            inputs[field] = value
    existing = (await db.execute(select(Credential).where(Credential.name == args.name))).scalar_one_or_none()
    cred = await svc.store(db, args.name, "model_provider", inputs, ACTOR, existing=existing)
    if args.purpose:
        cred.purpose = args.purpose
    await db.commit()
    return f"credential {args.name}: stored in {cred.backend} ({cred.summary})"


async def _profile_set(db, args) -> str:
    if await presets.get(db, args.preset) is None:
        raise SystemExit(f"unknown preset {args.preset}")
    cred_id = None
    if args.credential:
        cred = (await db.execute(select(Credential).where(Credential.name == args.credential))).scalar_one_or_none()
        if cred is None:
            raise SystemExit(f"unknown credential {args.credential}")
        cred_id = cred.id
    row = await db.get(ModelProfile, (args.preset, args.role))
    if row is None:
        row = ModelProfile(preset=args.preset, role=args.role)
        db.add(row)
    row.model, row.credential_id, row.fallback_model, row.rpm = args.model, cred_id, args.fallback_model, args.rpm
    row.active, row.updated_by = True, ACTOR
    await db.commit()
    return f"profile {args.preset}/{args.role}: {args.model}" + (f" with {args.credential}" if args.credential else "")


async def _preset_activate(db, args) -> str:
    try:
        preset = await presets.activate(db, args.name, ACTOR)
    except KeyError:
        raise SystemExit(f"unknown preset {args.name}")
    await db.commit()
    return f"active preset: {preset.name} ({preset.kind})"


async def _team_set(db, args) -> str:
    team = (await db.execute(select(AgentTeam).where(AgentTeam.name == args.name))).scalar_one_or_none()
    if team is None:
        team = AgentTeam(name=args.name, description=args.description or "", members={}, created_by=ACTOR, updated_by=ACTOR)
        db.add(team)
    elif team.builtin:
        raise SystemExit(f"{args.name} is built in")
    members = dict(team.members or {})
    if args.role:
        members[args.role] = {"model": args.model or None, "addendum": (args.addendum or "")[:4000]}
    team.members, team.updated_by, team.active = members, ACTOR, True
    if args.description is not None:
        team.description = args.description
    await db.commit()
    return f"team {args.name}: " + (", ".join(f"{r}={m.get('model') or 'default'}" for r, m in members.items()) or "no members")


async def _run_new(db, args) -> str:
    team = None
    if args.team:
        team = (await db.execute(select(AgentTeam).where(AgentTeam.name == args.team))).scalar_one_or_none()
        if team is None:
            raise SystemExit(f"unknown team {args.team}")
    run = ActivityRun(run_name=args.name, sdd_yaml="", status=RunStatus.RUNNING,
                      agent_team_id=team.id if team and not team.builtin else None)
    db.add(run)
    await db.commit()
    metadata = {"vanguard_activity": f"run:{run.id}"}
    if team is not None and not team.builtin:
        for role, member in (team.members or {}).items():
            if (member or {}).get("addendum"):
                metadata[f"vanguard_instructions ({role})"] = json.dumps(
                    {"version": 0, "text": "", "team": team.name, "team_text": member["addendum"]})
    return f"{run.id}\nA2A metadata: {json.dumps(metadata)}"


async def _status(db, _args) -> str:
    active = await presets.active(db)
    lines = [f"active preset: {active.name} ({active.kind})", "presets: " + ", ".join(
        p.name for p in await presets.all_presets(db))]
    names = {c.id: c.name for c in (await db.execute(select(Credential))).scalars()}
    lines.append("credentials: " + (", ".join(sorted(names.values())) or "(none: the gateway uses GOOGLE_API_KEY)"))
    for p in (await db.execute(select(ModelProfile).order_by(ModelProfile.preset, ModelProfile.role))).scalars():
        lines.append(f"  profile {p.preset}/{p.role}: {p.model}{' via ' + names.get(p.credential_id, '?') if p.credential_id else ''}"
                     f"{'' if p.active else ' (inactive)'}")
    rows = (await db.execute(select(ModelCall.role, ModelCall.status_code, func.count())
                             .group_by(ModelCall.role, ModelCall.status_code))).all()
    lines.append("calls: " + (", ".join(f"{r}={s}×{n}" for r, s, n in rows) or "none yet"))
    teams = (await db.execute(select(AgentTeam).order_by(AgentTeam.name))).scalars().all()
    lines.append("teams: " + (", ".join(f"{t.name}({','.join(t.members or {}) or '-'})" for t in teams) or "none"))
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m store.admin", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="active preset, profiles, credentials, call counts")
    pr = sub.add_parser("preset", help="switch the active preset").add_subparsers(dest="action", required=True)
    pa = pr.add_parser("activate")
    pa.add_argument("name")
    cr = sub.add_parser("credential", help="store a provider key").add_subparsers(dest="action", required=True)
    cp = cr.add_parser("put", help="the api_key is read from stdin")
    cp.add_argument("name")
    cp.add_argument("--provider", required=True, choices=MODEL_PROVIDERS)
    cp.add_argument("--tier", choices=TIERS)
    cp.add_argument("--purpose", choices=("primary", "backup", "embeddings", "local", "testing", "other"))
    cp.add_argument("--base-url", dest="base_url")
    cp.add_argument("--project")
    cp.add_argument("--location")
    pf = sub.add_parser("profile", help="which model a role uses").add_subparsers(dest="action", required=True)
    ps = pf.add_parser("set")
    ps.add_argument("role", choices=MODEL_ROLES)
    ps.add_argument("--model", required=True)
    ps.add_argument("--credential")
    ps.add_argument("--preset", default="custom")
    ps.add_argument("--fallback-model", dest="fallback_model")
    ps.add_argument("--rpm", type=int)
    tm = sub.add_parser("team", help="agent teams (A3)").add_subparsers(dest="action", required=True)
    ts = tm.add_parser("set", help="create a team or set one member")
    ts.add_argument("name")
    ts.add_argument("--role", choices=("executor", "validator", "medic"))
    ts.add_argument("--model")
    ts.add_argument("--addendum")
    ts.add_argument("--description")
    rn = sub.add_parser("run", help="runs the gateway attributes calls to").add_subparsers(dest="action", required=True)
    rr = rn.add_parser("new", help="create a run (optionally with a team); prints its id")
    rr.add_argument("name")
    rr.add_argument("--team")
    return ap


COMMANDS = {("status", None): _status, ("preset", "activate"): _preset_activate,
            ("credential", "put"): _credential_put, ("profile", "set"): _profile_set,
            ("team", "set"): _team_set, ("run", "new"): _run_new}


async def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    engine = create_async_engine(database_url(), pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            print(await COMMANDS[(args.cmd, getattr(args, "action", None))](db, args))
    except CredentialError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
