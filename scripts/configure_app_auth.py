#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import secrets


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"


def _set_env_value(source: str, key: str, value: str) -> str:
    rendered = f"{key}={json.dumps(value)}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(source):
        return pattern.sub(rendered, source)
    separator = "" if not source or source.endswith("\n") else "\n"
    return f"{source}{separator}{rendered}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure BookGym member/admin email login.")
    parser.add_argument("--member-name", default="Member")
    parser.add_argument("--member-email", required=True)
    parser.add_argument("--admin-name", default="Administrator")
    parser.add_argument("--admin-email", required=True)
    parser.add_argument(
        "--secure-cookie",
        action="store_true",
        help="Require HTTPS for the login cookie. Enable this once the public HTTPS tunnel is live.",
    )
    args = parser.parse_args()

    member_email = args.member_email.strip().casefold()
    admin_email = args.admin_email.strip().casefold()
    if not member_email or "@" not in member_email:
        raise SystemExit("A valid member email is required.")
    if not admin_email or "@" not in admin_email:
        raise SystemExit("A valid admin email is required.")
    if member_email == admin_email:
        raise SystemExit("Member and admin email addresses must be different.")

    source = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    values = {
        "BOOKGYM_APP_AUTH_ENABLED": "true",
        "BOOKGYM_APP_AUTH_SECRET": secrets.token_urlsafe(48),
        "BOOKGYM_APP_SESSION_DAYS": "30",
        "BOOKGYM_APP_SECURE_COOKIE": "true" if args.secure_cookie else "false",
        "BOOKGYM_APP_MEMBER_NAME": args.member_name.strip() or "Member",
        "BOOKGYM_APP_MEMBER_EMAIL": member_email,
        "BOOKGYM_APP_ADMIN_NAME": args.admin_name.strip() or "Administrator",
        "BOOKGYM_APP_ADMIN_EMAIL": admin_email,
    }
    for key, value in values.items():
        source = _set_env_value(source, key, value)
    ENV_PATH.write_text(source, encoding="utf-8")
    print(f"Configured {args.member_name} as the BookGym member and {args.admin_name} as administrator.")
    print("Restart the API and scheduler to apply the change.")


if __name__ == "__main__":
    main()
