#!/usr/bin/env python3
"""Root-only R2 delegation: gateway receives only expiring bucket/prefix credentials."""
from datetime import datetime, timedelta, timezone
import grp
import json
import os
from pathlib import Path
import sys
import tempfile

import httpx


def main():
    if os.geteuid() != 0:
        raise PermissionError("Credential issuer must run as root")
    config = json.loads(Path(sys.argv[1]).read_text())
    destination = Path("/run/openinvest-hosted-r2/credentials.json")
    issued = datetime.now(timezone.utc)
    with httpx.Client(timeout=20, trust_env=False) as client:
        response = client.post(
            f"https://api.cloudflare.com/client/v4/accounts/{config['account']}/r2/temp-access-credentials",
            headers={"Authorization": "Bearer " + Path(config["token_file"]).read_text().strip()},
            json={"parentAccessKeyId": config["parent_access_key_id"], "bucket": config["bucket"],
                  "permission": "object-read-write", "ttlSeconds": 3600,
                  "prefixes": ["research/", "backups/"]})
        response.raise_for_status()
        result = response.json()
        if not result.get("success"):
            raise ValueError("Credential delegation rejected")
        data = result["result"]
    credentials = {"Version": 1, "AccessKeyId": data["accessKeyId"],
                   "SecretAccessKey": data["secretAccessKey"], "SessionToken": data["sessionToken"],
                   "Expiration": (issued + timedelta(seconds=3540)).isoformat()}
    if not all(credentials[key] for key in ("AccessKeyId", "SecretAccessKey", "SessionToken")):
        raise ValueError("Incomplete credentials")
    descriptor, name = tempfile.mkstemp(dir=destination.parent, prefix=".refresh-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(credentials, output)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, 0, grp.getgrnam("openinvest-hosted").gr_gid)
        temporary.chmod(0o640)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    print("Scoped R2 credentials refreshed")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("R2 credential refresh failed; previous credentials retained", file=sys.stderr)
        raise SystemExit(1) from None
