"""Read-only shape diagnostics: no response values, tokens or program names printed."""

import json

import httpx

from tower.config import Settings
from tower.source import HackerOneClient


def main():
    settings = Settings.load()
    with HackerOneClient(settings.username, settings.api_token) as source:
        programs = source.list_programs()
    print(json.dumps({"public_program_count": len(programs)}))
    if not programs:
        return
    handle = programs[0]["handle"]
    with httpx.Client(
        auth=(settings.username, settings.api_token),
        timeout=20,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for endpoint in ("detail", "structured_scopes", "scope_exclusions"):
            path = f"https://api.hackerone.com/v1/hackers/programs/{handle}"
            if endpoint != "detail":
                path += "/" + endpoint
            response = client.get(path)
            try:
                payload = response.json()
            except ValueError:
                payload = None
            shape = {
                "endpoint": endpoint,
                "status": response.status_code,
                "document_type": type(payload).__name__,
            }
            if isinstance(payload, dict):
                shape["keys"] = sorted(payload.keys())
                shape["field_types"] = {key: type(value).__name__ for key, value in payload.items()}
            if isinstance(payload, list):
                shape["count"] = len(payload)
                if payload and isinstance(payload[0], dict):
                    shape["item_keys"] = sorted(payload[0].keys())
            print(json.dumps(shape))


if __name__ == "__main__":
    main()
