"""Static stand-in for the ERP until the real one signs tokens. Development only.

    python -m scripts.dev_erp keys                      # create .dev-erp/ key pair, print setup SQL
    python -m scripts.dev_erp token --sub 1 --name "ANKIT JAIN" --mobile 8800674795 --role "Super Admin"

The printed token is what the ERP will send on every request:  Authorization: Bearer <token>
When the ERP delivers its own public key, replace config.public_key on the provider row
(and config.issuer if theirs differs). No code changes.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

KEY_DIR = Path(__file__).resolve().parents[1] / ".dev-erp"
ISSUER = "bigship-erp"
AUDIENCE = "whatsapp-escalation"


def make_keys() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    KEY_DIR.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (KEY_DIR / "private.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    (KEY_DIR / "public.pem").write_text(public)
    config = {"issuer": ISSUER, "algorithm": "RS256", "public_key": public, "max_ttl_seconds": 300,
              "name_claim": "name", "phone_claim": "mobile", "role_claim": "role",
              "role_map": {"Super Admin": "owner"}}
    print(f"Wrote {KEY_DIR / 'private.pem'} and public.pem (git-ignored).\n\nSetup SQL (run as the admin role):\n")
    print(f"""INSERT INTO app.tenants (slug, name) VALUES ('bigship', 'Bigship') ON CONFLICT (slug) DO NOTHING;
INSERT INTO app.tenant_modules (tenant_id, module_key)
  SELECT t.id, m.key FROM app.tenants t, app.modules m WHERE t.slug = 'bigship' AND NOT m.is_core
  ON CONFLICT DO NOTHING;
INSERT INTO app.identity_providers (tenant_id, slug, name, kind, config)
  SELECT id, 'bigship-erp-jwt', 'Bigship ERP (signed tokens)', 'erp_jwt', $cfg${json.dumps(config)}$cfg$::jsonb
    FROM app.tenants WHERE slug = 'bigship';""")


def make_token(sub: str, name: str, mobile: str, role: str, ttl: int) -> None:
    import jwt

    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "name": name, "mobile": mobile, "role": role,
              "iat": now, "exp": now + ttl}
    print(jwt.encode(claims, (KEY_DIR / "private.pem").read_text(), algorithm="RS256"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("keys")
    token = commands.add_parser("token")
    token.add_argument("--sub", required=True, help="ERP login_user_id")
    token.add_argument("--name", default="")
    token.add_argument("--mobile", default="")
    token.add_argument("--role", default="")
    token.add_argument("--ttl", type=int, default=60, help="seconds (max 300 is accepted)")
    args = parser.parse_args()
    if args.command == "keys":
        make_keys()
    else:
        make_token(args.sub, args.name, args.mobile, args.role, args.ttl)


if __name__ == "__main__":
    main()
