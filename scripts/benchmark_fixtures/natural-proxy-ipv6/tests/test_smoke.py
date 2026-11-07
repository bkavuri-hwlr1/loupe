from src import subject


def test_smoke():
    client = subject._TunnelConnection(
        "https",
        "docs.example.invalid",
        "192.0.2.1",
        443,
        3,
        subject.ssl.create_default_context(),
        ("127.0.0.1", 8080),
    )
    assert client._tunnel_port == 443
    client.close()
