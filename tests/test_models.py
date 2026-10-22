import pytest
from ssh_jumper.models import Forward, Hop, HostEndpoint, Profile


@pytest.mark.parametrize("forwards, message", [
    ([], "at least one"),
    ([Forward(8080), Forward(8080, "another-host", 443)], "used more than once"),
    ([Forward(target_host=" ")], "hostname"),
    ([Forward(local_port=0)], "Forwarding ports"),
    ([Forward(target_port=65536)], "Forwarding ports"),
    ([Forward(health_port=-1)], "Watchdog ports"),
])
def test_invalid_forward_configuration(forwards, message):
    with pytest.raises(ValueError, match=message):
        Profile(hops=[Hop("jump", username="user")], forwards=forwards).validate()


def test_alternative_host_mode_and_validation():
    hop = Hop("primary", username="user", alternatives=[HostEndpoint("backup", 2222, "backup-user")])
    assert hop.endpoints() == [hop]
    hop.multiple_hosts = True
    assert hop.endpoints() == [hop, HostEndpoint("backup", 2222, "backup-user")]
    Profile(hops=[hop]).validate()
    for candidates in ([], [HostEndpoint("primary", 22, "user")], [HostEndpoint("", 22, "user")],
                       [HostEndpoint("backup", 0, "user")], [HostEndpoint("backup", 22)],
                       [HostEndpoint("backup", 22, "user", "Private key file")]):
        hop.alternatives = candidates
        with pytest.raises(ValueError):
            Profile(hops=[hop]).validate()
