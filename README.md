Fully vibecoded app which i find kind of usefull for own purposes. 


# SSH Jumper

A Windows tray application for reaching multiple TCP services through one ordered chain of SSH servers.

## Run

Open `dist/SSH-Jumper/SSH-Jumper.exe` after building. Keep the entire `SSH-Jumper` folder together; Python is bundled. For development, install Python 3.13 and run `./run.ps1` in PowerShell.

1. Name your tunnel and click **Add hop**. Enter each SSH server in order, from the first reachable server to the last jump server. Add, edit, remove, or reorder any number of hops.
2. Choose authentication separately for each hop: both local agents, Windows OpenSSH agent, Pageant, password, or an OpenSSH/PEM private key file. For PuTTY `.ppk` keys, load them in Pageant and select Pageant. Run agents under the same Windows user as this app.
   Optionally enable **Multiple hosts** in the hop editor. **Add host** and **Edit host** open the same host editor, with an independent hostname, SSH port, username, authentication method, password/key passphrase, and private-key file for each alternative. Double-click an alternative to edit it. New alternatives start with blank credentials; changes to the primary host do not affect them. Alternatives cannot contain nested alternatives. The primary host is tried first on a new connection; alternatives follow in their listed order. Disabling the mode preserves the alternative list but uses only the primary host. The hop itself is not skipped.
3. Under **TCP forwards · from last hop**, edit the initial forward or click **Add forward** for each additional destination. Each entry has its own **local port**, **destination host** as reachable from the last SSH server, and **destination TCP port**. For example, forward local `8443` to `192.168.20.10:443` and local `13389` to `192.168.20.20:3389` simultaneously. Local ports must be unique within a profile. Select an entry to edit or remove it; double-click also edits it.
   Each forward also has an optional **Description**, such as “Office desktop” or “Router admin”, shown alongside its addresses in the list and saved with the profile. Existing forwards start with an empty description.
   Select a forward and click **Copy** to open the same dialog with all its values filled in, including its description and ports. Change the local port to an unused one before saving the new entry. Cancel leaves the original and the list unchanged.
4. Set the shared watchdog interval. Each forward can specify a different TCP probe port on its destination host; zero uses that forward's destination port.
5. Click **Save profile** or **Connect tunnel** (which also saves). Point your client to `127.0.0.1:<local port>`.

Closing the window hides it in the system tray. Double-click the tray icon to reopen; its menu offers connect, disconnect, and quit. Quit disconnects the tunnel. One saved profile runs at a time in each application instance. Edits stay in memory when switching profiles; click Save before quitting to persist them.

Only one SSH Jumper instance can run in a Windows session, including while it is hidden in the tray. Additional launches exit immediately. The instance lock is released automatically on exit or a crash; open the existing app from its tray icon.

To rename a profile, edit its name above the SSH route. The sidebar updates as you type. Press Enter in the name field or click **Save profile** to persist the change; switching profiles retains the edit in memory until you save.

Select a profile and click **Copy** below the saved-tunnel list to duplicate its complete configuration, including current unsaved edits, hop alternatives, credentials, forwards, endpoint SSH settings, and watchdog options. The copy is selected with a unique “(copy)” name and can be edited independently. Click **Save profile** to persist it. Profile copying is available while disconnected.

## PuTTY / KiTTY and WinSCP

Edit a forward and enable **SSH endpoint**. Set the destination TCP port to the device's SSH port (usually 22), then enter its own SSH username and authentication settings. These credentials belong to the destination device and are independent of the hop credentials. Copying a forward copies its endpoint settings too.

Open **App settings** to choose PuTTY or KiTTY as the terminal and configure all three executable paths. Installed clients are detected automatically; **Find installed clients** repeats detection. Settings are shared across profiles and saved in `%APPDATA%\SSH Jumper\settings.json`.

Once the tunnel is connected, select an SSH-enabled forward and click its **Open with PuTTY / KiTTY** or **Open with WinSCP** icon button. Both clients connect to that forward's `127.0.0.1` port. You can select different forwards while connected; route editing stays locked. WinSCP opens an SFTP session in a new instance with isolated default settings. Client sessions may need reopening after tunnel reconnection.

External clients have their own authentication capabilities:

- **Pageant / All local agents:** these integrations use Pageant. The tunnel engine continues to support both agents. **OpenSSH agent** is supported by compatible WinSCP builds; unsupported clients show an explanation instead of silently changing the selected method. PuTTY/KiTTY require Pageant.
- **Password:** current PuTTY builds and WinSCP can receive saved passwords through temporary files, never command-line password values. Older PuTTY/KiTTY builds prompt for the password. PuTTY's secure file option requires version 0.77 or a compatible fork.
- **Private key file:** select a PuTTY `.ppk` key for these external clients, or convert an OpenSSH/PEM key with PuTTYgen. WinSCP can receive the saved passphrase through a file; PuTTY/KiTTY prompt for it. Loading an encrypted key into Pageant avoids repeated passphrase prompts.

Password handoff files use a private Windows directory accessible to the current user and administrators. They are deleted when the client exits, after two minutes, or when SSH Jumper exits; expired files from an interrupted run are cleaned up on the next client launch. Persistent credentials remain DPAPI-encrypted. External clients perform their own host-key checks and may prompt on first connection; the profile's **Ignore SSH host keys** setting applies to the tunnel's hops.

Client options follow the [PuTTY command-line documentation](https://the.earth.li/~sgtatham/putty/0.85/htmldoc/Chapter3.html) and [WinSCP command-line documentation](https://winscp.net/eng/docs/commandline).

## How the chain works

The first SSH connection goes directly from your PC to hop 1. Each additional hop connects through a loopback TCP forward carried by the previous hop. Windows assigns an available intermediate port by binding port `0`; listeners stay bound so there is no port-selection race. All listeners bind only to `127.0.0.1`. No random listening ports are opened on the SSH servers.

Each hop authenticates from your PC; credentials and agent sockets are not installed or forwarded onto jump hosts. Both agents are queried explicitly when **All local agents** is selected. SSH servers must permit `direct-tcpip` forwarding. Hostnames after the first hop are resolved by the preceding SSH server; the destination is resolved by the final server.

All final forwards share the same SSH connections. If any local listener cannot bind, the connection attempt cleans up every listener and SSH connection rather than leaving a partial tunnel.

With multiple hosts enabled, a candidate must authenticate and provide a working route through every subsequent hop to at least one configured destination probe. A host that accepts SSH but cannot reach any destination is rejected. Failed candidates are cleaned up while the working earlier hops are retained; if every candidate at a later hop fails, the app also tries alternatives at earlier hops. The connection log shows candidate failures and the selected hostname/port for each hop.

The watchdog opens and closes a TCP connection to every configured destination through the last SSH transport. It confirms TCP reachability, not application-level health. A check cycle probes all entries and logs changes in endpoint reachability. The tunnel remains Connected while at least one destination responds; an individual endpoint outage does not interrupt working forwards. After three consecutive cycles where all endpoints are unreachable (or the SSH transport is closed), automatic reconnect rebuilds the full chain and all forwards. Any reachable endpoint resets that failure counter. At least one destination must pass the initial probe before the profile reports Connected. Initial connection failures also retry, with a 2–60 second exponential backoff. Existing client TCP sessions must reconnect after a tunnel rebuild. SSH keepalives run every 15 seconds.

On watchdog failure, automatic reconnect starts with the next candidate at the last hop that has alternatives. It falls back through the other candidates if necessary and can select a recovered primary later. It keeps a healthy selected host rather than periodically switching back to the primary. Enable **Automatically reconnect** for watchdog-driven failover; initial candidate selection works even with automatic reconnect disabled. Local forwarding ports and destination addresses stay the same when switching hosts.

## Credentials and host keys

Profiles live at `%APPDATA%\SSH Jumper\profiles.json`. Passwords and key passphrases for primary hosts, alternative hosts, and SSH endpoints use Windows DPAPI encryption scoped to the current Windows account. Hosts, usernames, ports, and key paths are readable profile metadata. Copying the profile file to another Windows account does not transfer usable credentials.

Older profiles with a single destination are loaded automatically as one-entry forward lists. Existing hops without alternatives default to single-host mode. Alternatives saved by version 3 inherit a one-time copy of their parent's authentication settings during migration, preserving existing behavior; each is then independently editable. Existing forwards default to SSH integration disabled. The first save upgrades version 1–4 profiles to version 5 and keeps the original as `profiles.v1.bak`, `profiles.v2.bak`, `profiles.v3.bak`, or `profiles.v4.bak` in the same directory. Older app builds cannot read version 5; use the new build after saving.

Host-key checks are disabled by default, as requested; server identity is not verified in that mode. Uncheck **Ignore SSH host keys** to require trusted entries in `%USERPROFILE%\.ssh\known_hosts`. The real hostname and SSH port are used for verification even when the connection arrives through an intermediate loopback port. Unknown and changed keys are rejected in strict mode.

Password and key-file authentication are noninteractive. Interactive MFA prompts and hardware-key touch workflows are not implemented. Agent key support depends on Paramiko and the agent's signing capabilities. No SSH server configuration is modified by this app.

## Build and verify

```powershell
./build.ps1
```

This installs the pinned development dependencies, runs tests, then creates the windowed executable in `dist/SSH-Jumper` and checks its startup. The packaging step prevents incompatible ICU libraries on the build machine's PATH from shadowing Windows' ICU API and supplies Qt's Microsoft runtime version. Builds are unsigned. For tests only:

```powershell
./.venv/Scripts/python.exe -m pytest -q
```

Integration tests run local SSH servers and exercise three-hop forwarding, simultaneous traffic to different destinations over shared hops, large streams, TCP half-close, cleanup, bad credentials, encrypted key files, partial-start rollback on occupied ports, and recovery of multiple forwards. Storage tests exercise real Windows DPAPI and migration of older profiles. UI tests cover adding, editing, removing, saving, and rejecting duplicate local ports. Agent discovery is mocked in tests; end-to-end authentication against your running Pageant/OpenSSH agents and actual network still needs verification with your profiles.

Built with [Paramiko](https://www.paramiko.org/) and [Qt for Python / PySide6](https://doc.qt.io/qtforpython-6/). Dependency licenses are included under the distribution's `licenses` directory.
