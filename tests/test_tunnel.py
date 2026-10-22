import socket
import threading
import time
from contextlib import contextmanager
import paramiko
import pytest
from ssh_jumper.models import Forward, Hop, HostEndpoint, Profile
from ssh_jumper.tunnel import Tunnel, LocalAgents


def wait_for(condition, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("Condition not reached before timeout")


@contextmanager
def echo_server(transform=lambda data: data):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)
    stop = threading.Event()
    def echo(client):
        with client:
            while data := client.recv(65536):
                client.sendall(transform(data))
    def accept():
        while not stop.is_set():
            try:
                client, _ = listener.accept()
                threading.Thread(target=echo, args=(client,), daemon=True).start()
            except socket.timeout:
                pass
            except OSError:
                break
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        listener.close()
        thread.join(1)


class Server(paramiko.ServerInterface):
    def __init__(self, deny=None, username="tester", password="secret", public_key=None):
        self.destinations = {}
        self.deny = deny
        self.username, self.password, self.public_key = username, password, public_key

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if self.public_key is None and (username, password) == (self.username, self.password) else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "publickey" if self.public_key is not None else "password,publickey"

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if username == self.username and (self.public_key is None or key == self.public_key) else paramiko.AUTH_FAILED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        if self.deny and self.deny(destination):
            return paramiko.OPEN_FAILED_CONNECT_FAILED
        self.destinations[chanid] = destination
        return paramiko.OPEN_SUCCEEDED


@contextmanager
def ssh_server(deny=None, username="tester", password="secret", public_key=None):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)
    stop = threading.Event()
    transports = []
    host_key = paramiko.RSAKey.generate(2048)

    def bridge(channel, destination):
        try:
            remote = socket.create_connection(destination, timeout=2)
        except OSError:
            channel.close()
            return
        remote.settimeout(None)
        def pump(source, target):
            try:
                while data := source.recv(65536):
                    target.sendall(data)
            except (OSError, EOFError):
                pass
            finally:
                try:
                    target.shutdown(socket.SHUT_WR)
                except (OSError, EOFError):
                    pass
        worker = threading.Thread(target=pump, args=(channel, remote), daemon=True)
        worker.start()
        pump(remote, channel)
        worker.join(1)
        channel.close()
        remote.close()

    def serve(client):
        transport = paramiko.Transport(client)
        transports.append(transport)
        transport.add_server_key(host_key)
        server = Server(deny, username, password, public_key)
        try:
            transport.start_server(server=server)
            while transport.is_active() and not stop.is_set():
                channel = transport.accept(0.2)
                if channel:
                    threading.Thread(target=bridge, args=(channel, server.destinations[channel.chanid]), daemon=True).start()
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def accept():
        while not stop.is_set():
            try:
                client, _ = listener.accept()
                threading.Thread(target=serve, args=(client,), daemon=True).start()
            except socket.timeout:
                pass
            except OSError:
                break
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], transports
    finally:
        stop.set()
        listener.close()
        for transport in transports:
            transport.close()
        thread.join(1)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_three_hops_forward_concurrent_streams_and_cleanup():
    with echo_server() as target, ssh_server() as (first, _), ssh_server() as (second, _), ssh_server() as (third, _):
        states = []
        profile = Profile(hops=[Hop("127.0.0.1", port, "tester", "Password", "secret") for port in (first, second, third)],
                          forwards=[Forward(target_port=target, local_port=free_port())], reconnect=False)
        tunnel = Tunnel(profile, status=states.append)
        tunnel.start()
        try:
            wait_for(lambda: "Connected" in states)
            ports = [forward.port for forward in tunnel.forwarders]
            assert len(ports) == len(set(ports)) == 3
            from concurrent.futures import ThreadPoolExecutor
            def exchange(index):
                payload = bytes([index]) * 180000
                with socket.create_connection(("127.0.0.1", profile.forwards[0].local_port), timeout=5) as client:
                    client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                    response = bytearray()
                    while data := client.recv(65536):
                        response.extend(data)
                    assert response == payload
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(exchange, range(4)))
        finally:
            tunnel.disconnect()
            tunnel.thread.join(12)
        assert not tunnel.thread.is_alive()
        assert states[-1] == "Disconnected"
        for port in ports:
            with socket.socket() as client:
                assert client.connect_ex(("127.0.0.1", port)) != 0


def test_bad_password_leaves_no_listener():
    with ssh_server() as (port, _):
        states = []
        profile = Profile(hops=[Hop("127.0.0.1", port, "tester", "Password", "wrong")], reconnect=False, forwards=[Forward(local_port=free_port())])
        tunnel = Tunnel(profile, status=states.append)
        tunnel.start()
        tunnel.thread.join(8)
        assert states[-1] == "Disconnected"
        assert "Connected" not in states
        assert not tunnel.clients and not tunnel.forwarders


def test_watchdog_rebuilds_after_ssh_drop():
    with echo_server() as target, ssh_server() as (port, transports):
        states = []
        profile = Profile(hops=[Hop("127.0.0.1", port, "tester", "Password", "secret")],
                          forwards=[Forward(target_port=target, local_port=free_port()),
                                    Forward(target_port=target, local_port=free_port())], interval=3)
        tunnel = Tunnel(profile, status=states.append)
        tunnel.start()
        try:
            wait_for(lambda: "Connected" in states)
            transports[0].close()
            wait_for(lambda: "Reconnecting" in states, 15)
            wait_for(lambda: states[-1] == "Connected", 12)
            assert len(transports) >= 2
            for forward in profile.forwards:
                with socket.create_connection(("127.0.0.1", forward.local_port), timeout=5) as client:
                    client.sendall(b"restored")
                    assert client.recv(8) == b"restored"
        finally:
            tunnel.disconnect()
            tunnel.thread.join(12)


def test_both_agents_are_queried(monkeypatch):
    import os
    if os.name != "nt":
        pytest.skip("Windows dual-agent discovery")
    from ssh_jumper import tunnel as module
    connections = []
    class FakeAgent:
        def _connect(self, connection):
            connections.append(connection)
        def get_keys(self):
            return ()
        def _close(self):
            pass
    monkeypatch.setattr(module, "AgentSSH", FakeAgent)
    monkeypatch.setattr(module.win_openssh, "can_talk_to_agent", lambda: True)
    monkeypatch.setattr(module.win_pageant, "can_talk_to_agent", lambda: True)
    monkeypatch.setattr(module.win_openssh, "OpenSSHAgentConnection", lambda: "openssh")
    monkeypatch.setattr(module.win_pageant, "PageantConnection", lambda: "pageant")
    agents = LocalAgents("All local agents", lambda _: None)
    assert connections == ["openssh", "pageant"]
    agents.close()


def test_encrypted_private_key_file(tmp_path):
    key_file = tmp_path / "id_rsa"
    paramiko.RSAKey.generate(2048).write_private_key_file(str(key_file), password="key-secret")
    with echo_server() as target, ssh_server() as (port, _):
        states = []
        profile = Profile(hops=[Hop("127.0.0.1", port, "tester", "Private key file", "key-secret", str(key_file))],
                          forwards=[Forward(target_port=target, local_port=free_port())], reconnect=False)
        tunnel = Tunnel(profile, status=states.append)
        tunnel.start()
        try:
            wait_for(lambda: "Connected" in states)
        finally:
            tunnel.disconnect()
            tunnel.thread.join(12)


def test_probe_uses_configured_health_port():
    destinations = []
    class Channel:
        def close(self):
            pass
    class Transport:
        def is_active(self):
            return True
        def open_channel(self, kind, destination, origin, timeout):
            destinations.append(destination)
            return Channel()
    tunnel = Tunnel(Profile(forwards=[Forward(target_host="device.internal", target_port=443, health_port=22)]))
    tunnel.probe(Transport())
    assert destinations == [("device.internal", 22)]


def test_occupied_local_port_cleans_up_ssh():
    with echo_server() as occupied, ssh_server() as (port, _):
        states = []
        tunnel = Tunnel(Profile(hops=[Hop("127.0.0.1", port, "tester", "Password", "secret")],
                                forwards=[Forward(local_port=occupied)], reconnect=False), status=states.append)
        tunnel.start()
        tunnel.thread.join(12)
        assert states[-1] == "Disconnected"
        assert "Connected" not in states
        assert not tunnel.clients and not tunnel.forwarders


def test_multiple_destinations_share_last_hop_and_release_all_ports():
    from concurrent.futures import ThreadPoolExecutor
    with echo_server(bytes.upper) as upper, echo_server(bytes.lower) as lower, \
            ssh_server() as (first, first_transports), ssh_server() as (last, last_transports):
        profile = Profile(hops=[Hop("127.0.0.1", port, "tester", "Password", "secret") for port in (first, last)],
            forwards=[Forward(free_port(), "127.0.0.1", upper), Forward(free_port(), "localhost", lower)], reconnect=False)
        states = []
        tunnel = Tunnel(profile, status=states.append)
        tunnel.start()
        try:
            wait_for(lambda: "Connected" in states)
            assert len(first_transports) == len(last_transports) == 1
            assert len(tunnel.forwarders) == 3  # one intermediate, two final listeners
            def exchange(index):
                with socket.create_connection(("127.0.0.1", profile.forwards[index].local_port), timeout=5) as client:
                    client.sendall(b"MiXeD")
                    client.shutdown(socket.SHUT_WR)
                    response = b""
                    while data := client.recv(1024):
                        response += data
                    return response
            with ThreadPoolExecutor(max_workers=2) as executor:
                assert list(executor.map(exchange, range(2))) == [b"MIXED", b"mixed"]
            ports = [forward.port for forward in tunnel.forwarders]
        finally:
            tunnel.disconnect()
            tunnel.thread.join(12)
        for port in ports:
            with socket.socket() as client:
                assert client.connect_ex(("127.0.0.1", port)) != 0


def test_later_port_conflict_rolls_back_earlier_listener():
    with echo_server() as occupied, ssh_server() as (port, _):
        available = free_port()
        states = []
        tunnel = Tunnel(Profile(hops=[Hop("127.0.0.1", port, "tester", "Password", "secret")],
            forwards=[Forward(available), Forward(occupied)], reconnect=False), status=states.append)
        tunnel.start()
        tunnel.thread.join(12)
        assert states[-1] == "Disconnected"
        assert "Connected" not in states
        with socket.socket() as client:
            assert client.connect_ex(("127.0.0.1", available)) != 0
        assert not tunnel.clients and not tunnel.forwarders


def test_watchdog_checks_remaining_destinations_after_failure():
    destinations = []
    closed = []
    class Channel:
        def close(self):
            closed.append(True)
    class Transport:
        def is_active(self):
            return True
        def open_channel(self, kind, destination, origin, timeout):
            destinations.append(destination)
            if destination[0] == "offline":
                raise ConnectionError()
            return Channel()
    logs = []
    tunnel = Tunnel(Profile(forwards=[Forward(8000, "offline", 80, 22), Forward(8001, "online", 443)]), log=logs.append)
    assert tunnel.probe(Transport()) == 1
    assert "offline:22 (local 8000)" in logs[-1]
    assert "keeping tunnel connected" in logs[-1]
    assert destinations == [("offline", 22), ("online", 443)]
    assert closed == [True]


def test_watchdog_keeps_partial_route_and_resets_total_outage_count(monkeypatch):
    # Run watchdog cycles deterministically: two complete outages, one partial
    # recovery, then three complete outages. Only the last cycle may reconnect.
    from types import SimpleNamespace
    states, logs = [], []
    availability = iter([set(), set(), {"one"}, set(), set(), set()])
    current = {"one"}
    attempts = []
    tunnel = Tunnel(Profile(forwards=[Forward(8000, "one", 22), Forward(8001, "two", 22)]),
                    status=states.append, log=logs.append)
    class Transport:
        def is_active(self):
            return True
        def open_channel(self, kind, destination, origin, timeout):
            if destination[0] not in current:
                raise ConnectionError()
            return SimpleNamespace(close=lambda: None)
    transport = Transport()
    def establish():
        attempts.append(True)
        assert tunnel.probe(transport) == 1  # Partial reachability at startup.
        return transport
    def wait(seconds):
        nonlocal current
        if seconds == 2:  # Reconnect backoff: end the test after observing it.
            return True
        current = next(availability)
        return False
    monkeypatch.setattr(tunnel, "establish", establish)
    monkeypatch.setattr(tunnel.stop, "wait", wait)
    tunnel.run()
    assert len(attempts) == 1
    assert states == ["Connected", "Unhealthy (1/3)", "Unhealthy (2/3)", "Connected",
                      "Unhealthy (1/3)", "Unhealthy (2/3)", "Unhealthy (3/3)", "Reconnecting", "Disconnected"]
    assert any("All destination TCP probes failed" in entry for entry in logs)


def test_initial_partial_reachability_keeps_primary_route():
    with echo_server() as target, ssh_server(deny=lambda destination: destination[1] == 1) as (port, sessions):
        states = []
        profile = Profile(hops=[Hop("127.0.0.1", port, "tester", "Password", "secret")],
                          forwards=[Forward(free_port(), "127.0.0.1", 1),
                                    Forward(free_port(), "127.0.0.1", target)], reconnect=False)
        tunnel = Tunnel(profile, status=states.append)
        tunnel.start()
        try:
            wait_for(lambda: "Connected" in states)
            assert len(sessions) == 1 and len(tunnel.forwarders) == 2
            with socket.create_connection(("127.0.0.1", profile.forwards[1].local_port), timeout=5) as client:
                client.sendall(b"still-working")
                assert client.recv(13) == b"still-working"
        finally:
            tunnel.disconnect()
            tunnel.thread.join(12)


def test_third_hop_tries_alternative_when_primary_cannot_reach_destination():
    with echo_server() as target, ssh_server() as (first, first_sessions), \
            ssh_server() as (second, second_sessions), ssh_server(deny=lambda _: True) as (bad, bad_sessions), \
            ssh_server() as (good, good_sessions):
        hops = [Hop("127.0.0.1", port, "tester", "Password", "secret") for port in (first, second, bad)]
        hops[2].multiple_hosts = True
        hops[2].alternatives = [HostEndpoint("127.0.0.1", good, "tester", "Password", "secret")]
        states = []
        tunnel = Tunnel(Profile(hops=hops, forwards=[Forward(free_port(), "127.0.0.1", target)], reconnect=False), status=states.append)
        tunnel.start()
        try:
            wait_for(lambda: "Connected" in states)
            assert tunnel.selected == {0: 0, 1: 0, 2: 1}
            assert len(first_sessions) == len(second_sessions) == len(bad_sessions) == len(good_sessions) == 1
            assert not bad_sessions[0].is_active()
            assert len(tunnel.clients) == 3
            assert len(tunnel.forwarders) == 3
            with socket.create_connection(("127.0.0.1", tunnel.profile.forwards[0].local_port), timeout=5) as client:
                client.sendall(b"alternative")
                assert client.recv(11) == b"alternative"
        finally:
            tunnel.disconnect()
            tunnel.thread.join(12)


def test_watchdog_switches_candidate_and_can_return_to_recovered_primary():
    primary_failed, backup_failed = threading.Event(), threading.Event()
    with echo_server() as target, ssh_server() as (first, _), \
            ssh_server(deny=lambda _: primary_failed.is_set()) as (primary, primary_sessions), \
            ssh_server(deny=lambda _: backup_failed.is_set()) as (backup, backup_sessions):
        final_hop = Hop("127.0.0.1", primary, "tester", "Password", "secret", multiple_hosts=True,
                        alternatives=[HostEndpoint("127.0.0.1", backup, "tester", "Password", "secret")])
        states = []
        profile = Profile(hops=[Hop("127.0.0.1", first, "tester", "Password", "secret"), final_hop],
                          forwards=[Forward(free_port(), "127.0.0.1", target)], interval=3)
        tunnel = Tunnel(profile, status=states.append)
        tunnel.start()
        try:
            wait_for(lambda: states and states[-1] == "Connected")
            assert tunnel.selected[1] == 0
            primary_failed.set()
            wait_for(lambda: states[-1] == "Connected" and tunnel.selected.get(1) == 1, 20)
            assert len(primary_sessions) == len(backup_sessions) == 1
            with socket.create_connection(("127.0.0.1", profile.forwards[0].local_port), timeout=5) as client:
                client.sendall(b"ok")
                assert client.recv(2) == b"ok"
            primary_failed.clear()
            backup_failed.set()
            wait_for(lambda: states[-1] == "Connected" and tunnel.selected.get(1) == 0, 20)
            assert len(primary_sessions) == 2
        finally:
            tunnel.disconnect()
            tunnel.thread.join(12)


def test_unreachable_primary_uses_alternative_and_all_failed_routes_cleanup():
    with echo_server() as target, ssh_server() as (good, _):
        hop = Hop("127.0.0.1", free_port(), "tester", "Password", "secret", multiple_hosts=True,
                  alternatives=[HostEndpoint("127.0.0.1", good, "tester", "Password", "secret")])
        states = []
        tunnel = Tunnel(Profile(hops=[hop], forwards=[Forward(free_port(), "127.0.0.1", target)], reconnect=False), status=states.append)
        tunnel.start()
        try:
            wait_for(lambda: "Connected" in states)
            assert tunnel.selected[0] == 1
        finally:
            tunnel.disconnect()
            tunnel.thread.join(12)
        assert not tunnel.clients and not tunnel.forwarders
        # Authentication failure must also exhaust candidates and release resources.
        hop.password = "incorrect"
        hop.alternatives[0].password = "also-incorrect"
        states.clear()
        tunnel = Tunnel(tunnel.profile, status=states.append)
        tunnel.start()
        tunnel.thread.join(12)
        assert states[-1] == "Disconnected"
        assert "Connected" not in states
        assert not tunnel.clients and not tunnel.forwarders


def test_failed_later_hop_backtracks_to_earlier_alternative():
    with echo_server() as target, ssh_server(deny=lambda _: True) as (primary, primary_sessions), \
            ssh_server() as (backup, backup_sessions), ssh_server() as (last, last_sessions):
        hops = [Hop("127.0.0.1", primary, "tester", "Password", "secret", multiple_hosts=True,
                    alternatives=[HostEndpoint("127.0.0.1", backup, "tester", "Password", "secret")]),
                Hop("127.0.0.1", last, "tester", "Password", "secret")]
        states = []
        tunnel = Tunnel(Profile(hops=hops, forwards=[Forward(free_port(), "127.0.0.1", target)], reconnect=False), status=states.append)
        tunnel.start()
        try:
            wait_for(lambda: "Connected" in states)
            assert tunnel.selected == {0: 1, 1: 0}
            assert len(primary_sessions) == len(backup_sessions) == len(last_sessions) == 1
            assert not primary_sessions[0].is_active()
        finally:
            tunnel.disconnect()
            tunnel.thread.join(12)


def test_local_port_conflict_does_not_try_other_ssh_hosts():
    with echo_server() as occupied, ssh_server() as (primary, _), ssh_server() as (backup, backup_sessions):
        hop = Hop("127.0.0.1", primary, "tester", "Password", "secret", multiple_hosts=True,
                  alternatives=[HostEndpoint("127.0.0.1", backup, "tester", "Password", "secret")])
        logs = []
        tunnel = Tunnel(Profile(hops=[hop], forwards=[Forward(occupied, "127.0.0.1", occupied)], reconnect=False), log=logs.append)
        tunnel.start()
        tunnel.thread.join(12)
        assert not tunnel.thread.is_alive()
        assert not backup_sessions
        assert any(f"Cannot listen on local 127.0.0.1:{occupied}" in line for line in logs)
        assert not tunnel.clients and not tunnel.forwarders


def test_failover_uses_independent_username_auth_and_encrypted_key(tmp_path):
    key = paramiko.RSAKey.generate(2048)
    key_file = tmp_path / "backup_key"
    key.write_private_key_file(str(key_file), password="backup-key-passphrase")
    primary_failed = threading.Event()
    with echo_server() as target, \
            ssh_server(deny=lambda _: primary_failed.is_set(), username="primary-user", password="primary-password") as (primary, _), \
            ssh_server(username="backup-user", public_key=key) as (backup, backup_sessions):
        hop = Hop("127.0.0.1", primary, "primary-user", "Password", "primary-password", multiple_hosts=True,
                  alternatives=[HostEndpoint("127.0.0.1", backup, "backup-user", "Private key file", "backup-key-passphrase", str(key_file))])
        states = []
        tunnel = Tunnel(Profile(hops=[hop], forwards=[Forward(free_port(), "127.0.0.1", target)], interval=3), status=states.append)
        tunnel.start()
        try:
            wait_for(lambda: states and states[-1] == "Connected")
            assert tunnel.selected[0] == 0
            primary_failed.set()
            wait_for(lambda: states[-1] == "Connected" and tunnel.selected.get(0) == 1, 20)
            assert len(backup_sessions) == 1
            with socket.create_connection(("127.0.0.1", tunnel.profile.forwards[0].local_port), timeout=5) as client:
                client.sendall(b"independent-auth")
                assert client.recv(16) == b"independent-auth"
        finally:
            tunnel.disconnect()
            tunnel.thread.join(12)


def test_linux_agent_discovery(monkeypatch):
    import os
    if os.name == "nt":
        pytest.skip("Linux agent discovery")
    from types import SimpleNamespace
    closed = []
    key = SimpleNamespace(asbytes=lambda: b"public-key")
    monkeypatch.setattr(paramiko, "Agent", lambda: SimpleNamespace(
        get_keys=lambda: [key], close=lambda: closed.append(True)))
    agents = LocalAgents("All local agents", lambda _: None)
    assert list(agents.keys()) == [key]
    agents.close()
    assert closed == [True]
