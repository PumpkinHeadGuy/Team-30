import sqlite3
import hashlib
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs
from http.cookies import SimpleCookie

DB = "users.db"
HOST = "127.0.0.1"
PORT = 8080

# Database
def db():
    return sqlite3.connect(DB)

def setup():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE,
            password TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER
        )
    """)
    conn.commit()
    conn.close()

# Passwords
def hash_password(password):
    salt = secrets.token_bytes(16)
    hashed = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=16384,
        r=8,
        p=1
    )
    return salt.hex() + ":" + hashed.hex()

def check_password(password, stored):
    salt, old_hash = stored.split(":")
    salt = bytes.fromhex(salt)

    new_hash = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=16384,
        r=8,
        p=1
    )

    return secrets.compare_digest(new_hash.hex(), old_hash)

# Sessions
def new_session(user_id):
    token = secrets.token_urlsafe(32)

    conn = db()
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?)",
        (token, user_id)
    )
    conn.commit()
    conn.close()

    return token

def get_user(token):
    if not token:
        return None

    conn = db()

    user = conn.execute("""
        SELECT users.username
        FROM users
        JOIN sessions ON users.id = sessions.user_id
        WHERE sessions.token = ?
    """, (token,)).fetchone()

    conn.close()
    return user

# Server
class Server(BaseHTTPRequestHandler):

    def cookies(self):
        cookie = SimpleCookie(self.headers.get("Cookie"))
        return cookie.get("session").value if "session" in cookie else None

    def form(self):
        size = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(size).decode()
        return parse_qs(data)

    def do_GET(self):
        if self.path == "/":
            try:
                with open("index.html", "rb") as file:
                    html = file.read()

                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html)

            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"index.html not found")

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        form = self.form()

        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]

        # Register
        if self.path == "/register":
            hashed = hash_password(password)

            try:
                conn = db()

                cur = conn.execute(
                    "INSERT INTO users (username, password) VALUES (?, ?)",
                    (username, hashed)
                )

                conn.commit()
                user_id = cur.lastrowid
                conn.close()

            except sqlite3.IntegrityError:
                self.send_response(409)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Username already exists.")
                return

            token = new_session(user_id)

            self.send_response(302)
            self.send_header(
                "Set-Cookie",
                f"session={token}; HttpOnly; SameSite=Strict; Path=/"
            )
            self.send_header("Location", "/")
            self.end_headers()

        # Login
        elif self.path == "/login":
            conn = db()

            user = conn.execute(
                "SELECT id, password FROM users WHERE username = ?",
                (username,)
            ).fetchone()

            conn.close()

            if user and check_password(password, user[1]):
                token = new_session(user[0])

                self.send_response(302)
                self.send_header(
                    "Set-Cookie",
                    f"session={token}; HttpOnly; SameSite=Strict; Path=/"
                )
                self.send_header("Location", "/")
                self.end_headers()

            else:
                self.send_response(401)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Invalid username or password.")

        # Logout
        elif self.path == "/logout":
            token = self.cookies()

            conn = db()

            conn.execute(
                "DELETE FROM sessions WHERE token = ?",
                (token,)
            )

            conn.commit()
            conn.close()

            self.send_response(302)
            self.send_header(
                "Set-Cookie",
                "session=; Max-Age=0; Path=/"
            )
            self.send_header("Location", "/")
            self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

setup()

server = HTTPServer((HOST, PORT), Server)

print(f"Server running at http://{HOST}:{PORT}")

server.serve_forever()