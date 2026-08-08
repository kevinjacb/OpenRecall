"""Ed25519 signing and verification of device commands (§D).

The server signs a command's canonical bytes with its private key; the device
verifies with the server's public key. The relay only moves the signed envelope and
cannot forge or tamper without breaking verification.

The device trusts the verified ``payload`` bytes as the source of truth and parses
the command from them — so :meth:`SignedCommand.from_wire` derives ``command`` from
``payload``, never the other way around.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from .model import Command


@dataclass(frozen=True, slots=True)
class SignedCommand:
    command: Command
    payload: bytes  # exact signed bytes == command.canonical_bytes()
    signature: bytes

    def to_wire(self) -> dict[str, str]:
        """JSON-safe envelope for the (untrusted) relay to forward verbatim."""
        return {
            "payload": self.payload.decode("utf-8"),
            "sig": base64.b64encode(self.signature).decode("ascii"),
        }

    @classmethod
    def from_wire(cls, data: dict[str, str]) -> "SignedCommand":
        payload = data["payload"].encode("utf-8")
        return cls(
            command=Command.model_validate_json(payload),  # payload is source of truth
            payload=payload,
            signature=base64.b64decode(data["sig"]),
        )


class CommandSigner:
    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key

    @classmethod
    def generate(cls) -> "CommandSigner":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_private_bytes(cls, data: bytes) -> "CommandSigner":
        return cls(Ed25519PrivateKey.from_private_bytes(data))

    @property
    def public_key_bytes(self) -> bytes:
        return self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    @property
    def private_key_bytes(self) -> bytes:
        return self._key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())

    def sign(self, command: Command) -> SignedCommand:
        payload = command.canonical_bytes()
        return SignedCommand(command=command, payload=payload, signature=self._key.sign(payload))


def verify_command(signed: SignedCommand, public_key_bytes: bytes) -> bool:
    """True iff ``signed.signature`` is a valid signature over ``signed.payload``."""
    public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    try:
        public_key.verify(signed.signature, signed.payload)
        return True
    except InvalidSignature:
        return False


def load_or_create_signer(path: str | Path) -> CommandSigner:
    """Load the server's signing key from ``path``, generating + persisting it once.

    Gives the server a stable identity across restarts so the device's provisioned
    public key keeps verifying. The raw 32-byte private key is written with 0600
    permissions.
    """
    key_path = Path(path)
    if key_path.exists():
        return CommandSigner.from_private_bytes(key_path.read_bytes())
    signer = CommandSigner.generate()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(signer.private_key_bytes)
    try:
        key_path.chmod(0o600)
    except OSError:  # pragma: no cover - non-POSIX filesystems
        pass
    return signer
