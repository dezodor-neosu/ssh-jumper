"""Windows DPAPI protects credentials for the current Windows account."""
import base64
import ctypes
from ctypes import wintypes
from dataclasses import asdict
import json
import os
import shutil
from pathlib import Path
from .models import Forward, Hop, HostEndpoint, Profile, SSHAuth


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def crypt(value: bytes, decrypt=False) -> bytes:
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    dll = ctypes.WinDLL("crypt32", use_last_error=True)
    function = dll.CryptUnprotectData if decrypt else dll.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(result.data)


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ["APPDATA"]) / "SSH Jumper" / "profiles.json"

    def save(self, profiles):
        payload = []
        for profile in profiles:
            item = asdict(profile)
            for hop in item["hops"]:
                for host in [hop, *hop["alternatives"]]:
                    secret = host.pop("password")
                    host["protected_password"] = base64.b64encode(crypt(secret.encode())).decode() if secret else ""
            for forward in item["forwards"]:
                if forward["ssh"] is not None:
                    secret = forward["ssh"].pop("password")
                    forward["ssh"]["protected_password"] = base64.b64encode(crypt(secret.encode())).decode() if secret else ""
            payload.append(item)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            previous_version = json.loads(self.path.read_text(encoding="utf-8")).get("version")
            if previous_version in (1, 2, 3, 4):
                backup = self.path.with_suffix(f".v{previous_version}.bak")
                if not backup.exists():
                    shutil.copy2(self.path, backup)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"version": 5, "profiles": payload}, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def load(self):
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("version") not in (1, 2, 3, 4, 5):
            raise ValueError("Unsupported profile file version")
        profiles = []
        for item in data["profiles"]:
            hops = []
            for hop in item.pop("hops"):
                secret = hop.pop("protected_password", "")
                hop["password"] = crypt(base64.b64decode(secret), True).decode() if secret else ""
                alternatives = []
                for endpoint in hop.get("alternatives", []):
                    if data["version"] < 4:
                        endpoint.update({key: hop.get(key, getattr(HostEndpoint(), key)) for key in
                                         ("username", "auth", "password", "key_file")})
                    else:
                        secret = endpoint.pop("protected_password", "")
                        endpoint["password"] = crypt(base64.b64decode(secret), True).decode() if secret else ""
                    alternatives.append(HostEndpoint(**endpoint))
                hop["alternatives"] = alternatives
                hops.append(Hop(**hop))
            if data["version"] == 1:
                forwards = [Forward(**{key: item.pop(key) for key in
                    ("local_port", "target_host", "target_port", "health_port") if key in item})]
            else:
                forwards = []
                for forward in item.pop("forwards"):
                    if forward.get("ssh") is not None:
                        secret = forward["ssh"].pop("protected_password", "")
                        forward["ssh"]["password"] = crypt(base64.b64decode(secret), True).decode() if secret else ""
                        forward["ssh"] = SSHAuth(**forward["ssh"])
                    forwards.append(Forward(**forward))
            profiles.append(Profile(hops=hops, forwards=forwards, **item))
        return profiles
