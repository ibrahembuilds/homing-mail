"""A tiny SMTP server for tests and local runs. It accepts mail, keeps it in
memory and can be told to refuse particular recipients."""
import base64
import socketserver
import threading


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        catcher = self.server.catcher
        self._send('220 mailcatcher ESMTP')
        sender, recipients, user = None, [], None
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode('utf-8', 'replace').strip()
            upper = command.upper()
            if upper.startswith(('EHLO', 'HELO')):
                self.wfile.write(b'250-mailcatcher\r\n250-8BITMIME\r\n250-AUTH PLAIN\r\n250 SMTPUTF8\r\n')
            elif upper.startswith('AUTH PLAIN'):
                parts = command.split()
                if len(parts) < 3:
                    self._send('334 ')
                    parts.append(self.rfile.readline().decode().strip())
                try:
                    _, name, password = base64.b64decode(parts[2]).decode().split('\0')
                except ValueError:
                    self._send('501 bad auth')
                    continue
                if catcher.passwords and catcher.passwords.get(name) != password:
                    self._send('535 5.7.8 Username and Password not accepted')
                    continue
                user = name
                with catcher.lock:
                    catcher.logins.append(name)
                self._send('235 2.7.0 Accepted')
            elif upper.startswith('MAIL FROM:'):
                sender, recipients = command[10:].split()[0].strip('<>'), []
                self._send('250 OK')
            elif upper.startswith('RCPT TO:'):
                address = command[8:].split()[0].strip('<>')
                reply = catcher.refuse.get(address.lower())
                if reply:
                    self._send(reply)
                else:
                    recipients.append(address)
                    self._send('250 OK')
            elif upper == 'DATA':
                if not recipients:
                    self._send('554 no valid recipients')
                    continue
                self._send('354 end with .')
                lines = []
                while True:
                    data = self.rfile.readline()
                    if not data or data in (b'.\r\n', b'.\n'):
                        break
                    lines.append(data[1:] if data.startswith(b'..') else data)
                raw = b''.join(lines)
                with catcher.lock:
                    catcher.messages.append({'from': sender, 'to': list(recipients), 'raw': raw, 'user': user})
                if catcher.echo:
                    print(f'--- mail from {sender} to {", ".join(recipients)} ({len(raw)} bytes) ---')
                    print(raw.decode('utf-8', 'replace')[:1500])
                    print('--- end ---')
                self._send('250 queued')
            elif upper in ('RSET', 'NOOP'):
                self._send('250 OK')
            elif upper == 'QUIT':
                self._send('221 bye')
                return
            else:
                self._send('502 not implemented')

    def _send(self, text):
        self.wfile.write(text.encode() + b'\r\n')


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class MailCatcher:
    def __init__(self, port=0, echo=False):
        self.messages = []
        self.refuse = {}          # address -> SMTP reply such as "550 5.1.1 no such user"
        self.passwords = {}       # user -> password; when set, only these may sign in
        self.logins = []
        self.lock = threading.Lock()
        self.echo = echo
        self.server = _Server(('127.0.0.1', port), _Handler)
        self.server.catcher = self
        self.port = self.server.server_address[1]

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
