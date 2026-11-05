"""Enforce the suite's offline contract, including accidental DNS requests."""

import socket

import pytest


@pytest.fixture(autouse=True)
def prevent_live_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Tests must stay offline; use fixtures and httpx.MockTransport")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket.socket, "sendto", denied)
