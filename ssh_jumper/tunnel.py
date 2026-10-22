import socket
import threading
from pathlib import Path
import paramiko
from paramiko.agent import AgentSSH
from paramiko import win_openssh, win_pageant


class LocalListenerError(OSError):
    """A local port problem cannot be fixed by selecting another SSH host."""


class LocalAgents:
    """Query both agents explicitly; Paramiko's default chooses only one."""
    def __init__(self, mode, log):
        self.agents = []
        for name, module, factory in (
            ("OpenSSH agent", win_openssh, win_openssh.OpenSSHAgentConnection),
            ("Pageant", win_pageant, win_pageant.PageantConnection),
        ):
            if mode not in ("All local agents", name):
                continue
            agent = AgentSSH()
            try:
                if module.can_talk_to_agent():
                    agent._connect(factory())
                    self.agents.append(agent)
                    log(f"{name}: {len(agent.get_keys())} key(s) available")
            except Exception:
                agent._close()
                log(f"{name}: unavailable")

    def keys(self):
        seen = set()
        for agent in self.agents:
            for key in agent.get_keys():
                if key.asbytes() not in seen:
                    seen.add(key.asbytes())
                    yield key

    def close(self):
        for agent in self.agents:
            agent._close()


class Forwarder:
    """Own an OS-assigned loopback listener and all of its active bridges."""
    def __init__(self, transport, destination, port=0, log=lambda _: None):
        self.transport, self.destination, self.log = transport, destination, log
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.connections = set()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listener.bind(("127.0.0.1", port))
            self.listener.listen(64)
            self.listener.settimeout(0.3)
        except Exception:
            self.listener.close()
            raise
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stop.is_set():
            try:
                client, address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                if self.stop.is_set():
                    client.close()
                    break
                self.connections.add(client)
            threading.Thread(target=self.bridge, args=(client, address), daemon=True).start()

    def bridge(self, client, address):
        channel = None
        try:
            channel = self.transport.open_channel("direct-tcpip", self.destination, address, timeout=10)
            with self.lock:
                if self.stop.is_set():
                    return
                self.connections.add(channel)

            def pump(source, target):
                try:
                    while not self.stop.is_set():
                        data = source.recv(65536)
                        if not data:
                            break
                        target.sendall(data)
                except (OSError, EOFError, paramiko.SSHException):
                    pass
                finally:
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except (OSError, EOFError):
                        pass

            upstream = threading.Thread(target=pump, args=(client, channel), daemon=True)
            upstream.start()
            pump(channel, client)
            upstream.join(timeout=1)
        except Exception:
            if not self.stop.is_set():
                self.log(f"Forward to {self.destination[0]}:{self.destination[1]} failed")
        finally:
            for connection in (client, channel):
                if connection is not None:
                    connection.close()
                    with self.lock:
                        self.connections.discard(connection)

    def close(self):
        self.stop.set()
        self.listener.close()
        with self.lock:
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except (OSError, EOFError):
                    pass
                connection.close()
            self.connections.clear()
        self.thread.join(timeout=1)


class Tunnel:
    def __init__(self, profile, log=lambda _: None, status=lambda _: None):
        self.profile, self.log, self.status = profile, log, status
        self.stop = threading.Event()
        self.clients = []
        self.forwarders = []
        self.lock = threading.RLock()
        self.thread = None
        self.preferred = [0 for _ in profile.hops]
        self.selected = {}
        self.unreachable = None

    def start(self):
        self.profile.validate()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def disconnect(self):
        self.stop.set()
        # Network cancellation is off the GUI thread.
        threading.Thread(target=self.cleanup, daemon=True).start()

    def cleanup(self):
        self.cleanup_to(0, 0)

    def cleanup_to(self, client_count, forward_count):
        with self.lock:
            clients, self.clients = self.clients[client_count:], self.clients[:client_count]
            forwards, self.forwarders = self.forwarders[forward_count:], self.forwarders[:forward_count]
            for client in reversed(clients):
                client.close()
            for forward in reversed(forwards):
                forward.close()

    def connect_hop(self, hop, endpoint):
        if self.stop.is_set():
            raise InterruptedError()
        client = paramiko.SSHClient()
        with self.lock:
            if self.stop.is_set():
                raise InterruptedError()
            self.clients.append(client)
        if self.profile.ignore_host_keys:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            known = Path.home() / ".ssh" / "known_hosts"
            if known.exists():
                client.load_host_keys(str(known))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        agents = LocalAgents(hop.auth, self.log)
        try:
            keys = list(agents.keys())
            if hop.auth == "Private key file":
                keys = [paramiko.PKey.from_path(hop.key_file, password=hop.password.encode() if hop.password else None)]
            if not keys and hop.auth not in ("Password",):
                raise ValueError("No keys available from the selected local agent or key file")
            attempts = keys if keys else [None]
            for index, key in enumerate(attempts):
                if self.stop.is_set():
                    raise InterruptedError()
                sock = socket.create_connection(endpoint, timeout=10)
                try:
                    client.connect(hostname=hop.host, port=hop.port, username=hop.username,
                                   sock=sock, pkey=key,
                                   password=hop.password if hop.auth == "Password" else None,
                                   allow_agent=False, look_for_keys=False, timeout=10,
                                   auth_timeout=15, banner_timeout=10, channel_timeout=10)
                    if self.stop.is_set():
                        client.close()
                        raise InterruptedError()
                    client.get_transport().set_keepalive(15)
                    return client.get_transport()
                except paramiko.AuthenticationException:
                    client.close()
                    sock.close()
                    if index == len(attempts) - 1:
                        raise
                except Exception:
                    client.close()
                    sock.close()
                    raise
        finally:
            agents.close()

    def add_forward(self, transport, destination, port=0):
        with self.lock:
            if self.stop.is_set():
                raise InterruptedError()
            try:
                forward = Forwarder(transport, destination, port, self.log)
            except OSError as exc:
                raise LocalListenerError(
                    f"Cannot listen on local 127.0.0.1:{port or 'automatic'}: {exc}. "
                    "Choose an available local port; changing SSH hosts will not fix this.") from exc
            self.forwarders.append(forward)
            return forward

    def establish(self):
        self.status("Connecting")
        self.selected = {}
        self.unreachable = None
        transport = self.establish_hop(0, None)
        for forward in self.profile.forwards:
            self.add_forward(transport, (forward.target_host, forward.target_port), forward.local_port)
            self.log(f"Listening on 127.0.0.1:{forward.local_port} → {forward.target_host}:{forward.target_port}")
        return transport

    def establish_hop(self, index, upstream):
        """Try candidate routes, keeping the working prefix while backtracking."""
        hop = self.profile.hops[index]
        candidates = hop.endpoints()
        start = self.preferred[index] % len(candidates)
        last_error = None
        for offset in range(len(candidates)):
            if self.stop.is_set():
                raise InterruptedError()
            choice = (start + offset) % len(candidates)
            candidate = candidates[choice]
            with self.lock:
                checkpoint = (len(self.clients), len(self.forwarders))
            try:
                endpoint = (candidate.host, candidate.port)
                if upstream is not None:
                    forward = self.add_forward(upstream, endpoint)
                    endpoint = ("127.0.0.1", forward.port)
                    self.log(f"Intermediate loopback port: {forward.port}")
                self.log(f"Hop {index + 1}: connecting to {candidate.username}@{candidate.host}:{candidate.port}"
                         f" (candidate {choice + 1}/{len(candidates)})")
                transport = self.connect_hop(candidate, endpoint)
                self.log(f"Hop {index + 1}: authenticated")
                if index + 1 < len(self.profile.hops):
                    final_transport = self.establish_hop(index + 1, transport)
                else:
                    self.probe(transport)
                    final_transport = transport
                self.selected[index] = choice
                self.preferred[index] = choice
                self.log(f"Hop {index + 1}: selected {candidate.host}:{candidate.port}")
                return final_transport
            except (InterruptedError, LocalListenerError):
                self.cleanup_to(*checkpoint)
                raise
            except Exception as exc:
                last_error = exc
                self.log(f"Hop {index + 1}: route via {candidate.host}:{candidate.port} failed: {exc}")
                self.cleanup_to(*checkpoint)
        raise ConnectionError(f"Hop {index + 1}: no working route through {len(candidates)} host(s): {last_error}") from last_error

    def prefer_alternative(self):
        # Start with another candidate at the deepest multi-host hop. The route
        # search will backtrack to earlier alternatives if no downstream route works.
        for index in reversed(range(len(self.profile.hops))):
            candidates = self.profile.hops[index].endpoints()
            if len(candidates) > 1 and index in self.selected:
                choice = (self.selected[index] + 1) % len(candidates)
                self.preferred[index] = choice
                candidate = candidates[choice]
                self.log(f"Failover: next attempt starts with hop {index + 1} at {candidate.host}:{candidate.port}")
                return

    def probe(self, transport):
        if not transport.is_active():
            raise ConnectionError("SSH connection closed")
        # Probe every endpoint through the last hop, not just our local listeners.
        failures = []
        for forward in self.profile.forwards:
            if self.stop.is_set():
                raise InterruptedError()
            port = forward.health_port or forward.target_port
            try:
                channel = transport.open_channel("direct-tcpip",
                    (forward.target_host, port), ("127.0.0.1", 0), timeout=10)
                channel.close()
            except Exception:
                failures.append(f"{forward.target_host}:{port} (local {forward.local_port})")
        if not transport.is_active():
            raise ConnectionError("SSH connection closed")
        previous = self.unreachable
        self.unreachable = tuple(failures)
        reachable = len(self.profile.forwards) - len(failures)
        if not reachable:
            raise ConnectionError("All destination TCP probes failed: " + ", ".join(failures))
        if self.unreachable != previous:
            if failures:
                self.log(f"{reachable}/{len(self.profile.forwards)} destination(s) reachable; keeping tunnel connected. "
                         "Unreachable: " + ", ".join(failures))
            else:
                self.log(f"All {reachable} destination(s) reachable")
        return reachable

    def run(self):
        delay = 2
        try:
            while not self.stop.is_set():
                try:
                    transport = self.establish()
                    self.status("Connected")
                    delay = 2
                    failures = 0
                    while not self.stop.wait(self.profile.interval):
                        try:
                            self.probe(transport)
                            failures = 0
                            self.status("Connected")
                        except Exception as exc:
                            failures += 1
                            self.status(f"Unhealthy ({failures}/3)")
                            self.log(f"Watchdog: {exc}")
                            if failures >= 3:
                                if self.profile.reconnect:
                                    self.prefer_alternative()
                                raise ConnectionError("Three consecutive destination checks failed")
                except Exception as exc:
                    if not self.stop.is_set():
                        # Authentication exceptions don't contain submitted passwords.
                        self.log(f"Connection failed: {type(exc).__name__}: {exc}")
                finally:
                    self.cleanup()
                if self.stop.is_set() or not self.profile.reconnect:
                    break
                self.status("Reconnecting")
                self.log(f"Retrying in {delay}s")
                if self.stop.wait(delay):
                    break
                delay = min(delay * 2, 60)
        finally:
            self.status("Disconnected")
