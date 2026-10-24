"""Persistent environment authorization, separate from disposable history.

Kept as text (pairing.json, 2026-10-14): the paired environments with a hash of each token, and the code
being offered, if any. The running host and commands such as `pair code` both change it, so each change holds a
lock across processes and replaces the file whole."""

import hashlib
import json
import secrets
import time
from pathlib import Path

from .permissions import locked, private_file, write_private


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Pairing:
    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.lock = self.path.with_name(f".{self.path.name}.lock")
        self.clock = clock
        with locked(self.lock):
            if not self.path.exists():
                self.write({"environments": [], "code": None})
            private_file(self.path)

    def read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"environments": [], "code": None}
        if not isinstance(value, dict) or not isinstance(value.get("environments"), list):
            raise ValueError(f"{self.path} is not a pairing file")
        return value

    def write(self, value):
        write_private(self.path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def issue_code(self, *, digits: int, seconds: float, attempts: int) -> str:
        code = "".join(str(secrets.randbelow(10)) for _ in range(digits))
        with locked(self.lock):
            value = self.read()
            value["code"] = {"hash": digest(code), "expires": self.clock() + seconds, "attempts": attempts}
            self.write(value)
        return code

    def pair(self, secret: str, name: str) -> str | None:
        with locked(self.lock):
            value = self.read()
            code = value.get("code")
            if not code or code["expires"] <= self.clock() or code["attempts"] <= 0:
                return None
            if not secrets.compare_digest(code["hash"], digest(secret)):
                code["attempts"] = max(0, code["attempts"] - 1)
                self.write(value)
                return None
            # The code is used once.
            token = "bc1_" + secrets.token_urlsafe(32)
            value["code"] = None
            value["environments"].append({"id": secrets.token_hex(16), "name": name, "tokenHash": digest(token)})
            self.write(value)
            return token

    def authenticate(self, token: str | None) -> str | None:
        if not token:
            return None
        wanted = digest(token)
        for environment in self.read()["environments"]:
            if secrets.compare_digest(environment["tokenHash"], wanted):
                return environment["id"]
        return None

    def environments(self):
        return [{"id": e["id"], "name": e["name"]} for e in self.read()["environments"]]

    def revoke(self, environment: str) -> bool:
        with locked(self.lock):
            value = self.read()
            # Nothing is gained by keeping a revoked environment: its token stops matching either way (2026-10-13).
            kept = [e for e in value["environments"] if e["id"] != environment]
            if len(kept) == len(value["environments"]):
                return False
            value["environments"] = kept
            self.write(value)
            return True
