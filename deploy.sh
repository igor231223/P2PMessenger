#!/bin/bash
# Usage: bash deploy.sh root@YOUR_SERVER_IP
# Deploys p2p rendezvous server via Docker in one command.

set -e

TARGET="${1}"

if [ -z "$TARGET" ]; then
  echo "Usage: bash deploy.sh root@1.2.3.4"
  exit 1
fi

echo "==> Deploying to $TARGET ..."

ssh "$TARGET" "bash -s" << 'REMOTE'

set -e

# Install docker if missing
if ! command -v docker &>/dev/null; then
  echo "Installing Docker..."
  apt-get update -q && apt-get install -y -q docker.io
fi

mkdir -p /data/p2p

docker rm -f p2p-rendezvous 2>/dev/null || true

mkdir -p /tmp/p2p-deploy
cat > /tmp/p2p-deploy/rendezvous.py << 'EOF'
import asyncio, hashlib, json, os, secrets, sqlite3
from aiohttp import web

DB_PATH = os.environ.get('DB_PATH', 'rendezvous.db')

def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def _exec(sql, params=(), fetch=None):
    conn = _conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        if fetch == 'one': return cur.fetchone()
        if fetch == 'all': return cur.fetchall()
    finally:
        conn.close()

def init_db():
    d = os.path.dirname(DB_PATH)
    if d: os.makedirs(d, exist_ok=True)
    conn = _conn()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                nickname TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                nickname TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        conn.commit()
    finally:
        conn.close()

def _hash(pw, salt):
    return hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000).hex()

def nick_available(nick):
    return _exec('SELECT 1 FROM users WHERE nickname=?', (nick,), fetch='one') is None

def register_user(nick, pw):
    salt = secrets.token_hex(16)
    try:
        _exec('INSERT INTO users (nickname,password_hash,salt) VALUES (?,?,?)',
              (nick, _hash(pw, salt), salt))
        return True
    except sqlite3.IntegrityError:
        return False

def verify_user(nick, pw):
    row = _exec('SELECT password_hash,salt FROM users WHERE nickname=?', (nick,), fetch='one')
    if not row: return False
    return _hash(pw, row['salt']) == row['password_hash']

def create_session(nick):
    token = secrets.token_hex(32)
    _exec('INSERT INTO sessions (token,nickname) VALUES (?,?)', (token, nick))
    return token

def resolve_token(token):
    row = _exec('SELECT nickname FROM sessions WHERE token=?', (token,), fetch='one')
    return row['nickname'] if row else None

def delete_session(token):
    _exec('DELETE FROM sessions WHERE token=?', (token,))

async def h_check_nick(req):
    nick = req.query.get('nick','').strip()
    if not nick: return web.json_response({'error':'missing nick'},status=400)
    return web.json_response({'available': nick_available(nick)})

async def h_register(req):
    d = await req.json()
    nick, pw = d.get('nickname','').strip(), d.get('password','')
    if len(nick)<3: return web.json_response({'error':'Никнейм слишком короткий'},status=400)
    if len(pw)<4:   return web.json_response({'error':'Пароль слишком короткий'},status=400)
    if not register_user(nick, pw):
        return web.json_response({'error':'Никнейм уже занят'},status=409)
    return web.json_response({'token':create_session(nick),'nickname':nick})

async def h_login(req):
    d = await req.json()
    nick, pw = d.get('nickname','').strip(), d.get('password','')
    if not verify_user(nick, pw):
        return web.json_response({'error':'Неверный никнейм или пароль'},status=401)
    return web.json_response({'token':create_session(nick),'nickname':nick})

async def h_verify(req):
    d = await req.json()
    nick = resolve_token(d.get('token',''))
    if not nick: return web.json_response({'error':'Токен недействителен'},status=401)
    return web.json_response({'nickname':nick})

async def h_logout(req):
    d = await req.json()
    delete_session(d.get('token',''))
    return web.json_response({'ok':True})

async def h_debug(req):
    r = _rendezvous_instance.registry if _rendezvous_instance else {}
    out = {}
    for nick, e in r.items():
        if isinstance(e, dict):
            out[nick] = {'external':f"{e['external'][0]}:{e['external'][1]}",
                         'local':f"{e.get('local_ip')}:{e.get('local_port')}"}
        else:
            out[nick] = str(e)
    return web.json_response(out)

_rendezvous_instance = None

class Rendezvous(asyncio.DatagramProtocol):
    def __init__(self):
        self.registry = {}
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        global _rendezvous_instance
        _rendezvous_instance = self

    def _addr_of(self, nick):
        e = self.registry.get(nick)
        return e['external'] if isinstance(e, dict) else e

    def datagram_received(self, data, addr):
        try: msg = json.loads(data)
        except: return
        nick = resolve_token(msg.get('token',''))
        if not nick:
            self.transport.sendto(json.dumps({'err':'unauthorized'}).encode(), addr)
            return
        cmd = msg.get('cmd')
        if cmd == 'register':
            self.registry[nick] = {
                'external': addr,
                'local_ip': msg.get('local_ip'),
                'local_port': msg.get('local_port'),
            }
            self.transport.sendto(json.dumps({'ok':True}).encode(), addr)
            print(f'online: {nick} @ {addr[0]}:{addr[1]}  local={msg.get("local_ip")}:{msg.get("local_port")}')
        elif cmd == 'connect':
            dst = msg.get('to','')
            entry = self.registry.get(dst)
            if not entry:
                self.transport.sendto(json.dumps({'err':f'{dst} не в сети'}).encode(), addr)
                return
            existing = self.registry.get(nick, {})
            self.registry[nick] = {
                'external': addr,
                'local_ip': existing.get('local_ip') if isinstance(existing,dict) else None,
                'local_port': existing.get('local_port') if isinstance(existing,dict) else None,
            }
            dst_ext = entry['external']
            same_nat = (addr[0] == dst_ext[0])
            dst_ip, dst_port = (entry['local_ip'], entry['local_port']) \
                if same_nat and entry.get('local_ip') else dst_ext
            src_ip, src_port = (self.registry[nick]['local_ip'], self.registry[nick]['local_port']) \
                if same_nat and self.registry[nick].get('local_ip') else (addr[0], addr[1])
            self.transport.sendto(json.dumps({
                'cmd':'peer','id':dst,'ip':dst_ip,'port':dst_port,
                'ext_ip':dst_ext[0],'ext_port':dst_ext[1]
            }).encode(), addr)
            self.transport.sendto(json.dumps({
                'cmd':'peer','id':nick,'ip':src_ip,'port':src_port,
                'ext_ip':addr[0],'ext_port':addr[1]
            }).encode(), dst_ext)
            print(f'punch [{"LAN" if same_nat else "WAN"}]: {nick} <-> {dst}')
        elif cmd == 'relay_hello':
            dst_addr = self._addr_of(msg.get('to',''))
            if dst_addr:
                self.transport.sendto(json.dumps({'cmd':'relay_hello','from':nick,'pub':msg.get('pub','')}).encode(), dst_addr)
        elif cmd == 'relay_msg':
            dst_addr = self._addr_of(msg.get('to',''))
            if dst_addr:
                self.transport.sendto(json.dumps({'cmd':'relay_msg','from':nick,'n':msg.get('n',''),'c':msg.get('c','')}).encode(), dst_addr)

async def main():
    init_db()
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(Rendezvous, local_addr=('0.0.0.0', 5555))
    app = web.Application()
    app.router.add_get('/check_nick',   h_check_nick)
    app.router.add_post('/register',    h_register)
    app.router.add_post('/login',       h_login)
    app.router.add_post('/verify_token',h_verify)
    app.router.add_post('/logout',      h_logout)
    app.router.add_get('/debug',        h_debug)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 5556).start()
    print('rendezvous  UDP :5555  |  HTTP :5556')
    try:
        await asyncio.sleep(float('inf'))
    finally:
        transport.close()
        await runner.cleanup()

if __name__ == '__main__':
    asyncio.run(main())
EOF

cat > /tmp/p2p-deploy/Dockerfile << 'EOF'
FROM python:3.12-slim
WORKDIR /app
COPY rendezvous.py .
RUN pip install --no-cache-dir aiohttp
CMD ["python", "-u", "rendezvous.py"]
EOF

cd /tmp/p2p-deploy
docker build -t p2p-rendezvous . 2>&1 | tail -4
docker run -d \
  --name p2p-rendezvous \
  --restart unless-stopped \
  -p 5555:5555/udp \
  -p 5556:5556/tcp \
  -v /data/p2p:/data \
  -e DB_PATH=/data/rendezvous.db \
  p2p-rendezvous

sleep 2
docker logs p2p-rendezvous
rm -rf /tmp/p2p-deploy

SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo ""
echo "============================================"
echo "  ГОТОВО!"
echo "  UDP: ${SERVER_IP}:5555"
echo "  HTTP: ${SERVER_IP}:5556"
echo "  В клиенте укажи сервер: ${SERVER_IP}"
echo "============================================"
REMOTE
