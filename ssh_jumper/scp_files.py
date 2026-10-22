"""Legacy SCP transfers and POSIX shell metadata over an authenticated transport.

Requires a POSIX shell, stat or standard long-format ls, and scp.
No subprocess, extra login, or local scp installation is used.
"""
import posixpath
from datetime import datetime, timezone
import re
import shlex
import stat
import tempfile

import paramiko


def quote(path):
    if not path or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in path):
        raise ValueError("Enter a remote path without control characters.")
    # Prefix relative operands so leading '-' never becomes a command option.
    return shlex.quote(path if path.startswith('/') else './' + path)


class ScpFiles:
    def __init__(self, operation):
        self.operation = operation
        self._metadata = None

    def channel(self, command, combine=False, cleanup=False):
        if not cleanup:
            self.operation.check()
        channel = self.operation.transport.open_session(timeout=10)
        self.operation.channel = channel
        channel.settimeout(15)
        channel.set_combine_stderr(combine)
        try:
            channel.exec_command(command)
        except Exception:
            channel.close()
            raise
        return channel

    def command(self, command, cleanup=False):
        channel = self.channel(command, combine=True, cleanup=cleanup)
        try:
            blocks = []
            while True:
                if not cleanup:
                    self.operation.check()
                block = channel.recv(65536)
                if not block:
                    break
                blocks.append(block)
            output = b''.join(blocks).decode('utf-8', errors='strict')
            if channel.recv_exit_status() != 0:
                raise OSError(output.strip() or 'Remote shell command failed')
            return output
        finally:
            channel.close()

    def normalize(self, path):
        return self.command(f'cd {quote(path)} && pwd -P').rstrip('\n')

    @staticmethod
    def attributes(line):
        mode, size, modified, name = line.split('\t', 3)
        attr = paramiko.SFTPAttributes()
        attr.st_mode, attr.st_size, attr.st_mtime = int(mode, 16), int(size), int(modified)
        attr.filename = posixpath.basename(name)
        quote(attr.filename)
        return attr

    def metadata_command(self, operand, follow=False):
        if self._metadata is None:
            # Probe format support, not just command presence (BSD stat differs).
            self._metadata = self.command(
                "if stat -c '%f\t%s\t%Y\t%n' . >/dev/null 2>&1; "
                "then printf stat; else printf ls; fi").strip()
        if self._metadata == 'stat':
            return f"stat {'-L ' if follow else ''}-c '%f\t%s\t%Y\t%n' {operand}"
        return f"LC_ALL=C ls -ldn{'L' if follow else ''} {operand}"

    def parse_metadata(self, output, path):
        if self._metadata == 'stat':
            return self.attributes(output.rstrip('\n'))
        # The filename comes from the requested operand, never from ls output:
        # spaces, quotes, and a symlink's ' -> target' cannot alter its identity.
        match = re.match(
            r'^([bcdlps-][rwxstST-]{9})[+@.]?\s+\d+\s+\S+\s+\S+\s+'
            r'(\d+(?:,\s*\d+)?)\s+([A-Za-z]{3})\s+(\d{1,2})\s+'
            r'(\d{2}:\d{2}(?::\d{2})?|\d{4})\s+', output)
        if not match:
            raise OSError('Cannot read remote ls metadata: ' + output[:200].strip())
        permissions, size, month, day, clock = match.groups()
        attr = paramiko.SFTPAttributes()
        attr.filename = posixpath.basename(path)
        quote(attr.filename)
        attr.st_mode = {'-': stat.S_IFREG, 'd': stat.S_IFDIR, 'l': stat.S_IFLNK,
                        'b': stat.S_IFBLK, 'c': stat.S_IFCHR, 'p': stat.S_IFIFO,
                        's': stat.S_IFSOCK}[permissions[0]]
        for index, char in enumerate(permissions[1:]):
            if char not in '-ST':
                attr.st_mode |= 1 << (8 - index)
        if permissions[3] in 'sS':
            attr.st_mode |= stat.S_ISUID
        if permissions[6] in 'sS':
            attr.st_mode |= stat.S_ISGID
        if permissions[9] in 'tT':
            attr.st_mode |= stat.S_ISVTX
        attr.st_size = 0 if ',' in size else int(size)
        now = datetime.now(timezone.utc)
        month_number = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec').index(month) + 1
        if ':' in clock:
            parts = [int(part) for part in clock.split(':')]
            modified = datetime(now.year, month_number, int(day), *parts, tzinfo=timezone.utc)
            if modified.timestamp() > now.timestamp() + 86400:
                modified = modified.replace(year=now.year - 1)
        else:
            modified = datetime(int(clock), month_number, int(day), tzinfo=timezone.utc)
        attr.st_mtime = int(modified.timestamp())
        return attr

    def lstat(self, path):
        # Test separately so access/command errors cannot masquerade as missing files.
        metadata = self.metadata_command(quote(path))
        output = self.command(f'if [ -e {quote(path)} ] || [ -L {quote(path)} ]; then '
                              f"{metadata}; "
                              "else printf 'MISSING\\n'; fi")
        if output == 'MISSING\n':
            raise FileNotFoundError(path)
        return self.parse_metadata(output, path)

    def stat(self, path):
        return self.parse_metadata(self.command(self.metadata_command(quote(path), follow=True)), path)

    def listdir_iter(self, path):
        metadata = self.metadata_command('"$p"')
        output = self.command(f'cd {quote(path)} && '
            'for p in ./* ./.[!.]* ./..?*; do '
            '[ -e "$p" ] || [ -L "$p" ] || continue; '
            f"printf '%s\\n' \"$p\"; {metadata} || exit; done")
        lines = output.splitlines()
        if len(lines) % 2:
            raise OSError('Invalid remote directory listing; filenames with line breaks are unsupported')
        return iter(self.parse_metadata(lines[index + 1], lines[index])
                    for index in range(0, len(lines), 2))

    def mkdir(self, path):
        self.command(f'mkdir {quote(path)}')

    def rmdir(self, path):
        self.command(f'rmdir {quote(path)}')

    def remove(self, path):
        self.command(f'rm {quote(path)}', cleanup=self.operation.cancelled.is_set())

    def rename(self, source, target):
        # Hard-link publication fails if target exists, including dangling links.
        self.command(f'ln -T {quote(source)} {quote(target)} && rm {quote(source)}')

    def posix_rename(self, source, target):
        self.command(f'mv -fT {quote(source)} {quote(target)}')

    def chmod(self, path, mode):
        self.command(f'chmod {mode:o} {quote(path)}')

    def open(self, path, mode):
        if mode == 'rb':
            return ScpReader(self, path)
        if mode == 'wx':
            return ScpWriter(self, path)
        raise ValueError('Unsupported SCP file mode')

    def close(self):
        if self.operation.channel is not None:
            self.operation.channel.close()


def exact(channel, count):
    blocks = []
    while count:
        block = channel.recv(count)
        if not block:
            raise EOFError('SCP connection closed before the transfer completed')
        blocks.append(block)
        count -= len(block)
    return b''.join(blocks)


def line(channel):
    result = bytearray()
    while len(result) < 8192:
        byte = exact(channel, 1)
        if byte == b'\n':
            return bytes(result)
        result.extend(byte)
    raise OSError('SCP response exceeds the maximum header length')


def acknowledgement(channel):
    code = exact(channel, 1)
    if code != b'\0':
        raise OSError('SCP: ' + line(channel).decode('utf-8', errors='replace'))


class ScpReader:
    def __init__(self, client, path):
        self.client = client
        self.channel = client.channel(f'scp -f {quote(path)}')
        try:
            self.channel.sendall(b'\0')
            header = line(self.channel)
            if not header.startswith(b'C'):
                raise OSError('SCP: ' + header.decode('utf-8', errors='replace'))
            mode, size, name = header[1:].split(b' ', 2)
            self.remaining = int(size)
            if self.remaining < 0 or len(mode) != 4 or not all(c in b'01234567' for c in mode):
                raise OSError('Invalid SCP file header')
            # Never use the server-supplied filename to choose a local path.
            self.position = 0
            self.channel.sendall(b'\0')
            self.done = False
        except Exception:
            self.channel.close()
            raise

    def read(self, size=-1):
        self.client.operation.check()
        count = self.remaining if size < 0 else min(size, self.remaining)
        data = exact(self.channel, count)
        self.remaining -= count
        self.position += count
        if not self.remaining and not self.done:
            acknowledgement(self.channel)
            self.channel.sendall(b'\0')
            self.done = True
        return data

    def seek(self, offset):
        if offset < self.position:
            raise OSError('SCP streams support forward seeking only')
        while self.position < offset:
            if not self.read(min(65536, offset - self.position)):
                raise OSError('File changed during SCP read')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.channel.close()


class ScpWriter:
    def __init__(self, client, path):
        self.client, self.path = client, path
        self.stream = tempfile.TemporaryFile()
        self.created = False
        try:
            # Reserve exclusively before scp opens its destination for writing.
            client.command(f'(umask 077; set -C; : > {quote(path)})')
            self.created = True
        except Exception:
            self.stream.close()
            raise

    def set_pipelined(self, enabled):
        pass

    def write(self, data):
        self.client.operation.check()
        return self.stream.write(data)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *args):
        channel = None
        success = False
        try:
            if exc_type is not None:
                return
            size = self.stream.tell()
            self.stream.seek(0)
            channel = self.client.channel(f'scp -t {quote(self.path)}')
            acknowledgement(channel)
            channel.sendall(f'C0600 {size} data\n'.encode('ascii'))
            acknowledgement(channel)
            sent = 0
            while True:
                self.client.operation.check()
                block = self.stream.read(65536)
                if not block:
                    break
                channel.sendall(block)
                sent += len(block)
                self.client.operation.progress = f'SCP upload: {sent:,} / {size:,} bytes'
            channel.sendall(b'\0')
            acknowledgement(channel)
            success = True
        finally:
            self.stream.close()
            if channel is not None:
                channel.close()
            if not success and self.created:
                try:
                    self.client.remove(self.path)
                except (OSError, EOFError, paramiko.SSHException):
                    pass
