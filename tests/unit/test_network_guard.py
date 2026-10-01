import socket

import pytest

from reviewer.llm.anthropic import AnthropicClient
from reviewer.llm.base import LLMError
from reviewer.llm.openai import OpenAIClient


def test_external_hosts_are_blocked():
    # A raw IP: the guard must raise before any packet (or DNS lookup) leaves the machine.
    with socket.socket() as sock, pytest.raises(RuntimeError, match="network access blocked"):
        sock.connect(("203.0.113.1", 443))  # TEST-NET-3, reserved for documentation


def test_connect_ex_is_blocked_too():
    with socket.socket() as sock, pytest.raises(RuntimeError, match="network access blocked"):
        sock.connect_ex(("203.0.113.1", 443))


@pytest.mark.parametrize("client_class", [OpenAIClient, AnthropicClient])
def test_sdk_client_without_a_mock_cannot_reach_the_network(client_class):
    # Raw-IP base_url: no DNS lookup, and the guard rejects the connect before any packet.
    client = client_class("sk-fake", "m", base_url="https://203.0.113.1", max_attempts=1)

    # Depending on the SDK, the blocked connect surfaces as LLMError (wrapped as a connection
    # error) or as the guard's own RuntimeError. Either way nothing left the machine.
    with pytest.raises((LLMError, RuntimeError)):
        client.complete("s", "u")
