"""Persistent reply addresses and model-free listener checks."""

import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct


def read_endpoint(state):
    path = state / 'reply.json'
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('Unsafe reply address record')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('Unsupported reply address record')
    return value


def pin_address(state, address=None):
    """Keep the first advertised address, including an explicitly recovered legacy route."""
    from . import relay

    config = json.loads((state / 'config.json').read_text())
    target = relay.resolve_session(config['session_id'])
    directory = Path(target['messagingSocketPath']).parent
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('Unsafe socket directory')
    saved = read_endpoint(state)
    if saved is not None:
        if saved.get('session_id') != config['session_id']:
            raise ValueError('Reply address belongs to a different enrolled session')
        if address is not None and address != saved.get('address'):
            raise ValueError('An advertised reply address cannot be reassigned')
        address = saved.get('address')
    if address is None:
        try:
            previous = json.loads((state / 'daemon.json').read_text())
        except FileNotFoundError:
            previous = {}
        if previous.get('session_id') == config['session_id']:
            address = previous.get('address')
        if address is None:
            name = 'c' + hashlib.sha256(str(state.resolve()).encode()).hexdigest()[:15]
            address = 'uds:' + str(directory / (name + '.sock'))
    if not isinstance(address, str) or not address.startswith('uds:'):
        raise ValueError('Invalid reply address')
    path = Path(address[4:])
    if (not path.is_absolute() or '..' in path.parts or path.parent != directory
            or not path.name.endswith('.sock') or path == Path(target['messagingSocketPath'])):
        raise ValueError('Reply address must be a distinct socket in the enrolled session directory')
    if len(str(path).encode()) > 103:
        raise ValueError('Reply socket path exceeds the supported Unix socket path length')
    if saved is None:
        saved = {'version': 1, 'session_id': config['session_id'], 'address': address}
        # A running old daemon can be adopted without restarting or notifying it.
        try:
            metadata = json.loads((state / 'daemon.json').read_text())
            if metadata.get('address') == address and listener_alive(metadata):
                current = path.lstat()
                saved.update(device=current.st_dev, inode=current.st_ino)
        except FileNotFoundError:
            pass
        relay.atomic_json(state / 'reply.json', saved)
    return path


def listener_alive(metadata):
    """Connect only to our listener; send no frames and never contact a model."""
    from . import relay

    try:
        if relay.proc_start(metadata['pid']) != metadata['proc_start']:
            return False
        path = Path(metadata['address'][4:])
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return False
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(0.25)
            client.connect(str(path))
            pid, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            return pid == metadata['pid'] and uid == os.getuid() and relay.proc_start(pid) == metadata['proc_start']
    except (OSError, ValueError, KeyError):
        return False


def prepare_socket(state, path):
    """Under daemon.lock, remove only a recorded dead socket owned by this mailbox."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    saved = read_endpoint(state) or {}
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or (info.st_dev, info.st_ino) != (saved.get('device'), saved.get('inode'))):
        raise ValueError('Reply address is occupied by an unowned endpoint; no socket was replaced')
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(0.25)
        try:
            client.connect(str(path))
        except ConnectionRefusedError:
            pass
        else:
            raise ValueError('Reply address is still accepting connections; no socket was replaced')
    remove_socket(path, info)


def record_socket(state, path):
    from . import relay

    info = path.lstat()
    saved = read_endpoint(state)
    relay.atomic_json(state / 'reply.json', {**saved, 'device': info.st_dev, 'inode': info.st_ino})
    return info


def remove_socket(path, owned):
    try:
        info = path.lstat()
        if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == (owned.st_dev, owned.st_ino):
            path.unlink()
    except FileNotFoundError:
        pass
