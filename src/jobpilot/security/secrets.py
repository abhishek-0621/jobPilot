"""Credentials live in the macOS Keychain via keyring; never in YAML, prompts or logs."""

import keyring

SERVICE = "jobpilot"


def get_secret(name: str) -> str | None:
    return keyring.get_password(SERVICE, name)


def set_secret(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)
