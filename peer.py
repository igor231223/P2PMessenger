import asyncio
import json
import os
import socket
import sys

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

_BAD_PREFIXES = ('127.', '169.254.', '198.18.', '198.19.')


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        if not any(ip.startswith(p) for p in _BAD_PREFIXES):
            return ip
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not any(ip.startswith(p) for p in _BAD_PREFIXES):
                return ip
    except Exception:
        pass
    return '127.0.0.1'


class PeerProtocol(asyncio.DatagramProtocol):
    def __init__(self, my_id, token, on_message=None, on_connected=None, on_status=None):
        self.my_id      = my_id
        self.token      = token
        self.transport  = None
        self._loop      = None
        self._priv      = X25519PrivateKey.generate()
        self._pub       = self._priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.peer_addr  = None
        self.cipher     = None
        self._relay     = False
        self._peer_id   = None
        self._rendezvous= None
        self._on_message   = on_message   or (lambda t: None)
        self._on_connected = on_connected or (lambda: None)
        self._on_status    = on_status    or (lambda s: None)

    def connection_made(self, transport):
        self.transport = transport
        self._loop     = asyncio.get_running_loop()

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data)
        except Exception:
            return

        cmd = msg.get('cmd')

        if cmd == 'peer':
            self._peer_id  = msg.get('id')
            primary        = (msg['ip'], msg['port'])
            ext            = (msg.get('ext_ip', msg['ip']), msg.get('ext_port', msg['port']))
            addrs = [primary]
            if ext != primary:
                addrs.append(ext)
            self.peer_addr = primary
            self._loop.create_task(self._punch(addrs))

        elif cmd == 'hello':
            if self.cipher:
                return
            their_pub   = X25519PublicKey.from_public_bytes(bytes.fromhex(msg['pub']))
            shared      = self._priv.exchange(their_pub)
            self.cipher = ChaCha20Poly1305(shared[:32])
            self.peer_addr = addr
            self._relay    = False
            self._send_hello(addr)
            self._on_connected()

        elif cmd == 'relay_hello':
            if self.cipher:
                return
            their_pub   = X25519PublicKey.from_public_bytes(bytes.fromhex(msg['pub']))
            shared      = self._priv.exchange(their_pub)
            self.cipher = ChaCha20Poly1305(shared[:32])
            self._relay = True
            self._peer_id = msg.get('from', self._peer_id)
            self._send_relay_hello()
            self._on_connected()

        elif cmd == 'msg':
            if not self.cipher:
                return
            try:
                nonce = bytes.fromhex(msg['n'])
                ct    = bytes.fromhex(msg['c'])
                self._on_message(self.cipher.decrypt(nonce, ct, None).decode())
            except Exception:
                pass

        elif cmd == 'relay_msg':
            if not self.cipher:
                return
            try:
                nonce = bytes.fromhex(msg['n'])
                ct    = bytes.fromhex(msg['c'])
                self._on_message(self.cipher.decrypt(nonce, ct, None).decode())
            except Exception:
                pass

    def _send_hello(self, addr):
        self.transport.sendto(
            json.dumps({'cmd': 'hello', 'pub': self._pub.hex()}).encode(), addr
        )

    def _send_relay_hello(self):
        if not self._rendezvous or not self._peer_id:
            return
        self.transport.sendto(json.dumps({
            'cmd':   'relay_hello',
            'to':    self._peer_id,
            'token': self.token,
            'pub':   self._pub.hex(),
        }).encode(), self._rendezvous)

    async def _punch(self, addrs, attempts=20):
        self._on_status('connecting')
        for _ in range(attempts):
            if self.cipher:
                return
            for a in addrs:
                self._send_hello(a)
            await asyncio.sleep(0.3)

        if not self.cipher:
            await self._punch_relay()

    async def _punch_relay(self, attempts=20):
        if not self._rendezvous or not self._peer_id:
            self._on_status('timeout')
            return
        self._on_status('relay')
        for _ in range(attempts):
            if self.cipher:
                return
            self._send_relay_hello()
            await asyncio.sleep(0.4)
        if not self.cipher:
            self._on_status('timeout')

    def register(self, rendezvous):
        self._rendezvous = rendezvous
        local_port = self.transport.get_extra_info('sockname')[1]
        self.transport.sendto(json.dumps({
            'cmd':        'register',
            'token':      self.token,
            'local_ip':   _local_ip(),
            'local_port': local_port,
        }).encode(), rendezvous)

    def request_connect(self, target_id, rendezvous):
        self._peer_id    = target_id
        self._rendezvous = rendezvous
        self.transport.sendto(json.dumps({
            'cmd':   'connect',
            'to':    target_id,
            'token': self.token,
        }).encode(), rendezvous)

    def send_message(self, text):
        if not self.cipher:
            return False
        nonce = os.urandom(12)
        ct    = self.cipher.encrypt(nonce, text.encode(), None)
        if self._relay:
            self.transport.sendto(json.dumps({
                'cmd':   'relay_msg',
                'to':    self._peer_id,
                'token': self.token,
                'n':     nonce.hex(),
                'c':     ct.hex(),
            }).encode(), self._rendezvous)
        else:
            self.transport.sendto(
                json.dumps({'cmd': 'msg', 'n': nonce.hex(), 'c': ct.hex()}).encode(),
                self.peer_addr
            )
        return True


async def _cli_main():
    if len(sys.argv) < 4:
        print('usage: python peer.py <id> <token> <host:port>')
        sys.exit(1)
    my_id = sys.argv[1]
    token = sys.argv[2]
    host, port_str = sys.argv[3].rsplit(':', 1)
    rendezvous = (host, int(port_str))

    loop = asyncio.get_running_loop()
    _, proto = await loop.create_datagram_endpoint(
        lambda: PeerProtocol(
            my_id, token,
            on_message  = lambda t: print(f'\npeer: {t}', flush=True),
            on_connected= lambda:   print('\n[connected]', flush=True),
            on_status   = lambda s: print(f'[{s}]', flush=True),
        ),
        local_addr=('0.0.0.0', 0),
    )
    proto.register(rendezvous)
    print(f'registered as "{my_id}"  local_ip={_local_ip()}')
    print('  /connect <id>  — connect to peer\n')

    while True:
        line = await loop.run_in_executor(None, input, '> ')
        line = line.strip()
        if not line:
            continue
        if line.startswith('/connect '):
            proto.request_connect(line[9:].strip(), rendezvous)
            print('connecting...')
        else:
            proto.send_message(line)


if __name__ == '__main__':
    asyncio.run(_cli_main())
