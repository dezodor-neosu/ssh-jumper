from dataclasses import dataclass, field


@dataclass
class HostEndpoint:
    host: str = ""
    port: int = 22
    username: str = ""
    auth: str = "All local agents"
    password: str = ""
    key_file: str = ""

    def validate(self):
        if not self.host.strip() or not self.username.strip():
            raise ValueError("Every host needs a hostname and username.")
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH ports must be between 1 and 65535.")
        if self.auth == "Private key file" and not self.key_file:
            raise ValueError("Select a private key file for key-file authentication.")


@dataclass
class Hop(HostEndpoint):
    multiple_hosts: bool = False
    alternatives: list[HostEndpoint] = field(default_factory=list)

    def endpoints(self):
        return [self] + (self.alternatives if self.multiple_hosts else [])


@dataclass
class SSHAuth:
    username: str = ""
    auth: str = "All local agents"
    password: str = ""
    key_file: str = ""

    def validate(self):
        if not self.username.strip():
            raise ValueError("Enter an SSH username for this endpoint.")
        if self.auth not in ("All local agents", "OpenSSH agent", "Pageant", "Password", "Private key file"):
            raise ValueError("Select a supported SSH authentication method.")
        if self.auth == "Private key file" and not self.key_file.strip():
            raise ValueError("Select a private key file for this endpoint.")


@dataclass
class Forward:
    local_port: int = 8080
    target_host: str = "127.0.0.1"
    target_port: int = 80
    health_port: int = 0
    description: str = ""
    ssh: SSHAuth | None = None

    def validate(self):
        if not self.target_host.strip():
            raise ValueError("Enter the destination hostname or IP for each forward.")
        if not all(1 <= p <= 65535 for p in (self.local_port, self.target_port)):
            raise ValueError("Forwarding ports must be between 1 and 65535.")
        if not 0 <= self.health_port <= 65535:
            raise ValueError("Watchdog ports must be between 0 and 65535.")
        if self.ssh is not None:
            self.ssh.validate()


@dataclass
class Profile:
    name: str = "New tunnel"
    hops: list[Hop] = field(default_factory=list)
    forwards: list[Forward] = field(default_factory=lambda: [Forward()])
    interval: int = 15
    reconnect: bool = True
    ignore_host_keys: bool = True

    def validate(self):
        if not self.name.strip() or not self.hops:
            raise ValueError("Give the profile a name and add at least one SSH hop.")
        for hop in self.hops:
            if hop.multiple_hosts and not hop.alternatives:
                raise ValueError("Add at least one alternative host or disable multiple hosts mode.")
            seen = set()
            for endpoint in hop.endpoints():
                endpoint.validate()
                address = (endpoint.host.strip().casefold(), endpoint.port)
                if address in seen:
                    raise ValueError("A hop cannot contain the same host and SSH port more than once.")
                seen.add(address)
        if not self.forwards:
            raise ValueError("Add at least one TCP forward.")
        ports = set()
        for forward in self.forwards:
            forward.validate()
            if forward.local_port in ports:
                raise ValueError(f"Local port {forward.local_port} is used more than once. Each forward needs a unique local port.")
            ports.add(forward.local_port)
        if not 3 <= self.interval <= 3600:
            raise ValueError("Invalid watchdog interval.")
