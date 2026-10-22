# SSH Jumper

A Windows tray application for reaching multiple TCP services through one ordered chain of SSH servers.

## Linux

Linux desktop support uses the same Qt interface, SSH tunnels, native terminal and
file browser. On Linux, OpenSSH agent authentication uses the session's
`SSH_AUTH_SOCK`; Pageant and the MSTSC launcher are Windows-only. Select a local
executable in App settings for external editing.

Install from a checkout with Python 3.10 or newer (subject to the pinned packages'
Python support):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python install_linux.py
```

Launch **SSH Jumper** from the application menu, or run
`~/.local/bin/ssh-jumper`. This per-user installation runs from the checkout and
its virtual environment; keep both in place. To update, update the checkout and
reinstall requirements. Remove `~/.local/bin/ssh-jumper` and
`~/.local/share/applications/ssh-jumper.desktop` to remove the launchers.

Profiles and settings use `$XDG_CONFIG_HOME/ssh-jumper`, defaulting to
`~/.config/ssh-jumper`. Passwords and private-key passphrases are authenticated
Fernet ciphertext in version 6 profiles. The encryption key is stored in the
login session's Secret Service keyring under service `SSH Jumper`, entry
`profile-encryption-v1`. A running, unlocked Secret Service provider is required
when loading or saving nonempty credentials (for example GNOME Keyring, or KDE's
Secret Service integration). There is no plaintext fallback. If the keyring is
unavailable, saving fails without replacing the profile. A load failure disables
profile editing to protect the original file; unlock the keyring and restart.
Uncheck **Save passwords and key passphrases** to connect without storing secrets;
saving in this mode clears stored credentials from all profiles while keeping
them in memory for this session.

Back up the keyring along with profiles: deleting the encryption key makes saved
Linux credentials unrecoverable. Windows retains DPAPI and version 5 profiles.
Encrypted credentials cannot be moved between Windows and Linux; transfer a copy
of the profile metadata with `protected_password` fields cleared and re-enter
credentials. Private-key paths and editor settings may also need adjustment.

For a standalone Linux bundle, run `./build_linux.sh` on Linux. It runs the tests
and builds `dist/SSH-Jumper`; the target desktop still needs a Secret Service
provider and the system libraries required by Qt. Linux distributions and desktop
sessions should be tested individually. The Windows build remains `build.ps1`.

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

## Native terminal and SFTP

Edit a forward and enable **SSH endpoint**. Set the destination TCP port to the device's SSH port (usually 22), then enter its own SSH username and authentication settings. These credentials belong to the destination device and are independent of the hop credentials. Copying a forward copies its endpoint settings too.

Open **App settings** to select your preferred text editor executable, such as Notepad++, Notepad, or another editor. Optional arguments are entered as a JSON list (for example, `["-multiInst"]` for Notepad++); the filename is appended automatically. Settings are shared across profiles in `%APPDATA%\SSH Jumper\settings.json`. Old external SSH client settings migrate automatically to the new editor settings; choose an editor once after upgrading.

### Native SSH terminal

Connect the tunnel, select an SSH-enabled forward, and click **Open terminal**. Each click opens an independent native Qt terminal window through that forward's loopback listener. The endpoint gets its own SSH authentication and an `xterm-256color` PTY. Run `htop`, `top`, `less`, or your shell as usual; resizing the window updates the remote PTY dimensions.

The terminal supports UTF-8 (including wide and combining characters), ANSI/256/RGB colors, text attributes, cursor movement, erase/insert/delete operations, scrolling regions, DEC line drawing, alternate screens, cursor visibility, application cursor keys, F1–F12 and standard Ctrl/Alt shortcuts. Alternate-screen TUIs restore the shell display on exit. Up to 2,000 lines of shell scrollback are retained per window.

- Drag to select text; **Ctrl+Shift+C** copies it. **Ctrl+C** remains the remote interrupt key.
- **Ctrl+Shift+V** or **Shift+Insert** pastes. Bracketed paste is supported when the remote application enables it; otherwise pasted newlines can execute commands. Clipboard control characters are filtered and a single paste is limited to 1 MiB.
- Use the mouse wheel or **Shift+PageUp / Shift+PageDown** for shell scrollback. Typing returns to the live display.
- Closing a terminal disconnects only that endpoint session. Quitting SSH Jumper closes all native sessions. A remote disconnect leaves the screen and status visible; reopen the terminal after tunnel reconnection.

Native authentication supports passwords, OpenSSH/PEM key files, Windows OpenSSH agent, Pageant, and both agents together. Load `.ppk` keys into Pageant for native use. Credentials remain in memory for the connection; no temporary credential handoff files are created. As with hop authentication, interactive password/MFA prompts are not implemented: configure credentials in the forward editor.

The native terminal follows the profile's **Ignore SSH host keys** setting for the endpoint as well as the hops. When disabled, it verifies the real destination hostname and SSH port against `%USERPROFILE%\.ssh\known_hosts`, not the loopback address. Unknown or changed keys are rejected. When enabled, endpoint identity is not verified.

This is a basic terminal: remote mouse reporting, images, hyperlinks, tabs, search, and automatic session reconnection are not implemented. TUIs are operated with the keyboard. The renderer and session UI are part of SSH Jumper; VT parsing uses [pyte](https://github.com/selectel/pyte).

Keyboard sending runs independently of remote reads and output backpressure. Rendering updates changed rows, with a per-frame output processing budget to keep input responsive during bursts. The stream adapter consumes unsupported xterm extensions without displaying their payload or disconnecting the session; it responds to basic Vim terminal queries and leaves advanced keyboard protocols disabled.

### Native SFTP commander

In a connected native terminal, click **SFTP files…** to open the separate file browser. It uses channels on that terminal's existing authenticated SSH connection; it does not open another login or ask for credentials. If SFTP negotiation fails while SSH remains connected, it automatically tries legacy SCP for file transfers and remote shell commands for browsing. The remote panel shows **SCP** when the fallback is in use. Failed file operations are never retried using a different protocol.

The SCP fallback requires a POSIX shell, `scp`, GNU/BusyBox-compatible `stat -c` or standard `ls -ldn`, and standard file commands (`ln`, `mv`, `rm`, `mkdir`, `rmdir`, `chmod`, `pwd`) on the endpoint. Devices offering only a vendor command interface or an SCP-only restricted command cannot provide folder browsing through this fallback. If both methods fail, the browser reports the SFTP failure and fallback error. SCP uploads use a local temporary staging file to determine the size required by the legacy protocol. Log tail previews must read through the preceding bytes over SCP. Existing destination files are protected using exclusive temporary creation and no-replace hard-link publication; filesystems without hard links report an error. File edits use a temporary upload followed by remote `mv` replacement. When `stat -c` is unavailable, metadata comes from `ls`; modification times are estimates with the precision of its displayed date, and the device timezone may differ.

- The left panel browses local files and the right panel browses SFTP. **Tab** switches panels; the active panel is labeled. Double-click or press **Enter** to enter a folder or open a regular file. **Backspace**, **Up**, or the `..` row opens the parent folder (`..` is omitted at the root). Click any segment in the breadcrumb path below the address bar to jump directly to that ancestor. Local paths use the local separator; remote paths use `/`. The `..` row remains visible while filtering and is never included in copy/move operations. Both address bars accept paths and Enter; **Refresh** reloads each listing. Hidden files are included.
- **Space** toggles the current file/folder and advances to the next visible row. Arrow keys preserve this group selection; **F5/F6** act on the selected group. Mouse Ctrl/Shift selection also remains available. The `..` row cannot be selected with Space for transfers.
- Click **Name**, **Type**, **Size**, or **Modified** to sort; click again to reverse. Names use case-insensitive natural ordering (`file2` before `file10`), sizes sort numerically, and modification times chronologically. `..` stays first and folders stay above files. Each panel keeps its own sort order across navigation and refresh.
- Each panel has a fuzzy filename filter for its current directory. **Ctrl+P** focuses the active filter. Matching is case-insensitive and accepts letters in order with gaps (for example, `aplg` matches `application.log`). **Enter** or **Down** returns to the results; **Escape** clears the filter. Filtering does not search subdirectories, and hidden results are excluded from selection.
- **F3** opens the selected file in the read-only viewer; **F4** opens your selected external text editor. **F5** copies selected files/folders into the opposite panel's directory. **F6** moves them there. **F7** creates a directory, **F8** permanently deletes the selected files/folders after confirmation, and **F9** creates an empty file in the active panel. Deletion includes folder contents, bypasses the Recycle Bin, and cannot be undone. Links themselves are removed without following their targets; cancellation stops further deletion but cannot restore removed items.
- Copies and moves include nested directories and empty folders. Symbolic links and special files are rejected during transfers. Existing destination names are never overwritten or merged. A move removes each source file only after its transfer succeeds and its size/modification time still match. Cancellation or a later error leaves completed transfers in place; partial destination directories can remain.
- **Upload files…** and **Download selected…** retain the file/destination picker workflows.
- **Terminal → Files** queries the interactive shell's current directory using `pwd` and opens it in the browser. **Files → Terminal** sends a quoted, portable `cd` command for the displayed folder; check the terminal for any shell error. These buttons support shells with bracketed paste enabled and basic POSIX device shells whose visible prompt ends in `$` or `#`. Paste markers are sent only when the terminal enables them. Return to an empty shell prompt before syncing; the buttons refuse to send commands while a full-screen program or partially entered command is detected. Vendor CLI prompts such as `device>` are not treated as POSIX shells. If automatic sync is unavailable, type the remote folder into the browser's address bar. Directory queries time out after five seconds without changing the displayed folder.

F4 opens local files directly in the selected editor. For remote files, SSH Jumper downloads a complete working copy (up to 2 MiB), preserving the filename extension and all bytes; the external editor handles encoding and line endings. Save in that editor, then click **Upload changes** in the SSH Jumper editing window. You can repeat saves and uploads without closing the editor. This works with editors that reuse an existing process. Uploads compare the current remote contents with the last downloaded/uploaded version and refuse conflicts. They write a temporary file and replace the original using the SFTP POSIX rename extension; unsupported servers report an error without truncating the original. Failed uploads retain the local edits. Closing with changes not uploaded prompts for confirmation. Local working copies remain in the displayed `ssh-jumper-edit-*` temporary directory after closing so an editor's open tab cannot lose its file; remove those directories manually when finished. Close the editor tab when done. F3 remains the built-in read-only viewer.

The viewer supports **h/j/k/l**, **w/b/e**, **0/^/$**, **gg/G**, numeric counts such as **10j** and **42G**, and **Ctrl+D/U/F/B** for half/full-page movement. Use **/** or **?** for forward/backward literal text search, **n/N** to repeat or reverse, and **q** to close. Search wraps within the loaded preview. Files are decoded as UTF-8 with replacement for invalid bytes.

Toggle **F** (Shift+F) or **Follow name (tail -F)** to follow a log. Follow mode polls once per second through background reads, reopening the path each time so rotation, truncation, and a temporarily missing file are handled. It shows the latest 2 MiB and scrolls to the end; normal viewing shows the first 2 MiB. The size and preview limit are displayed. This is a bounded preview, not a full Vim editor or an archive of every log event between polls. Toggle follow off to browse/search without automatic scrolling.

Navigation and transfers run in the background, leaving the shell responsive. Progress and errors appear at the bottom; **Cancel** stops the active operation. Closing the browser cancels its work and leaves the terminal connected. Closing the terminal also closes its browser.

Existing destination files are never overwritten. Uploads and downloads are written to temporary `.ssh-jumper-….part` files and published under their final names after completion. Completed files in a batch remain if a later file fails. Temporary files are cleaned up when possible; a dropped connection may leave a remote `.part` file. Downloads whose names cannot be represented on Windows report an error.

File browsing always uses the original endpoint and SSH user, including in SCP mode. Running `sudo`, `su`, or nested `ssh` in the shell does not change the browser's host or permissions; do not sync directories from a different nested host. The browser initially opens the SSH login's home directory, independently of the terminal shell, until you sync or navigate.

For forwards whose destination TCP port is `3389`, select the forward and click **Open in MSTSC** to launch Windows Remote Desktop at `127.0.0.1:<local port>`. The button is available while the tunnel is connected; sign in using the Remote Desktop client.

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

Native terminal tests cover SSH PTYs, resize and keyboard traffic, terminal queries, alternate screens, incremental rendering, and input delivery independent of remote output. A real Vim smoke test opens, edits, saves, and exits when Vim is available on PATH or bundled with Git for Windows; otherwise that test is skipped.
