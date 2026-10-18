#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.app_auth import replace_user_delegations, upsert_app_user
from app.models import UserRole


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update a BookGym login.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--role",
        choices=[role.value for role in UserRole],
        default=UserRole.MEMBER.value,
    )
    parser.add_argument(
        "--manage-email",
        action="append",
        default=[],
        help="Email of a BookGym user this account may manage. Repeat for multiple users.",
    )
    args = parser.parse_args()

    role = UserRole(args.role)
    user = upsert_app_user(name=args.name, email=args.email, role=role)
    managed_users = []
    if args.manage_email:
        managed_users = replace_user_delegations(int(user.id), args.manage_email)
    elif role == UserRole.DELEGATE:
        raise SystemExit("A delegate requires at least one --manage-email value.")

    print(f"Configured {user.name} <{user.email}> as {user.role.value}.")
    if managed_users:
        print("Can manage: " + ", ".join(user.name for user in managed_users))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
