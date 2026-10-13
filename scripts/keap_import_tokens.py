"""Dev only: copy the OAuth tokens from the Keap prototype's .env into keap_connections.

The earlier standalone Keap prototype stored tokens in a .env file. The sync keeps them
in the database instead, where refreshes are saved under a row lock. Run once against a
LOCAL or demo database, never print the tokens, and never commit the .env.

    python scripts/keap_import_tokens.py --env-file "<path>/.env" [--practice rausch-pt]
"""

import argparse
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from rpt_agent.config import get_settings


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--practice", default=get_settings().stride_practice_slug)
    args = parser.parse_args()
    env = read_env(args.env_file)
    for key in ("KEAP_ACCESS_TOKEN", "KEAP_REFRESH_TOKEN", "KEAP_TOKEN_EXPIRES_AT"):
        if not env.get(key):
            raise SystemExit(f"{key} is missing in {args.env_file.name}")
    expires = datetime.fromtimestamp(float(env["KEAP_TOKEN_EXPIRES_AT"]), UTC)
    url = get_settings().supabase_db_url
    with psycopg.connect(url) as conn:
        practice = conn.execute("select id from practices where slug=%s", (args.practice,)).fetchone()
        if practice is None:
            raise SystemExit(f"practice {args.practice!r} not found")
        conn.execute(
            "insert into keap_connections(practice_id,keap_account,access_token,refresh_token,"
            "access_token_expires_at,status,last_refreshed_at) values(%s,%s,%s,%s,%s,'active',now()) "
            "on conflict(practice_id) do update set keap_account=excluded.keap_account,"
            "access_token=excluded.access_token,refresh_token=excluded.refresh_token,"
            "access_token_expires_at=excluded.access_token_expires_at,status='active',last_error=null",
            (practice[0], env.get("KEAP_EXPECTED_TENANT") or None, env["KEAP_ACCESS_TOKEN"],
             env["KEAP_REFRESH_TOKEN"], expires),
        )
    print(f"tokens saved for {args.practice} (expires {expires:%Y-%m-%d %H:%M} UTC, in {int(expires.timestamp() - time.time())}s)")


if __name__ == "__main__":
    main()
