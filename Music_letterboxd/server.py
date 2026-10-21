from os import path
import sqlite3
import hashlib
import secrets
import json
import base64
import os
import requests
from dotenv import load_dotenv
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit
from http.cookies import SimpleCookie
import mimetypes
import html as html_module

BASE_DIR = Path(__file__).resolve().parent
DB = BASE_DIR / "server.db"
HOST = "127.0.0.1"
PORT = 8080

load_dotenv(BASE_DIR / ".env")


class Album:
    def __init__(self, album_id, title, artist, release_year,album_cover_url):
      # all album info will be got from spotify api and stored in the album class
        self.id = album_id
        self.title = title
        self.artist = artist
        self.release_year = release_year
        self.album_cover_url = album_cover_url
        self.ratings = [] # store a list of tuples (user_id, rating, review)
        self.avg = 0.0

    def add_rating(self, user_id, rating, review):
        self.ratings.append((user_id, rating, review))
        self.avg = sum(r[1] for r in self.ratings) / len(self.ratings) if self.ratings else 0.0
      
class User:
    def __init__(self, user_id, username, spotify_id = None):
        self.id = user_id
        self.username = username
        self.spotify_id = spotify_id # if wanting to link spotify account to user
        self.ratings = [] #store a tuples of (album classes, rating out of 5, review string) )
        self.friends = [] # will store others user IDs
        self.lists = [] # Store lists of ratings

    def add_rating(self, album, rating, review):
        self.ratings.append((album, rating, review))

    def add_friend(self, friend_user):
        self.friends.append(friend_user)
# Database
def db():
    return sqlite3.connect(DB)

def setup():
    conn = db()
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE,
            password TEXT,
            spotify_id TEXT
        ) 
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions ( 
         token TEXT PRIMARY KEY,
         user_id INTEGER 
    ) 
    """) 
    conn.execute("""
        CREATE TABLE IF NOT EXISTS albums (
            id INTEGER PRIMARY KEY,
            spotify_id TEXT UNIQUE,
            title TEXT,
            artist TEXT,
            release_year INTEGER,
            album_cover_url TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            user_id INTEGER,
            album_id INTEGER,
            rating INTEGER,
            review TEXT,
            UNIQUE(user_id, album_id)
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

    row = conn.execute("""
        SELECT users.id, users.username
        FROM users
        JOIN sessions ON users.id = sessions.user_id
        WHERE sessions.token = ?
    """, (token,)).fetchone()

    conn.close()

    if row:
        return User(row[0], row[1])

    return None

def spotify_access_token():
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError(
            "Spotify credentials are not configured. "
            "Create Music_letterboxd/.env with SPOTIFY_CLIENT_ID and "
            "SPOTIFY_CLIENT_SECRET."
        )

    credentials = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode()

    response = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {credentials}"},
        data={"grant_type": "client_credentials"},
        timeout=10
    )
    response.raise_for_status()
    return response.json()["access_token"]

def search_spotify_albums(query):
    token = spotify_access_token()
    response = requests.get(
        "https://api.spotify.com/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": query, "type": "album", "limit": 9},
        timeout=10
    )
    response.raise_for_status()

    albums = []
    for album in response.json().get("albums", {}).get("items", []):
        artists = album.get("artists", [])
        images = album.get("images", [])
        albums.append({
            "id": album.get("id"),
            "title": album.get("name"),
            "artist": artists[0].get("name") if artists else "Unknown artist",
            "release_year": album.get("release_date", "")[:4],
            "album_cover_url": images[0].get("url") if images else None,
            "spotify_url": album.get("external_urls", {}).get("spotify")
        })
    return albums

# Server
class Server(BaseHTTPRequestHandler):

    def cookies(self):
        cookie = SimpleCookie(self.headers.get("Cookie"))
        return cookie.get("session").value if "session" in cookie else None

    def form(self):
        size = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(size).decode()
        return parse_qs(data)

    def serve_static_file(self, path):
        if not path.startswith("/Images/"):
            self.send_error(404, "File not found")
            return

        relative_path = path.lstrip("/")
        file_path = (BASE_DIR / relative_path).resolve()
        images_dir = (BASE_DIR / "Images").resolve()

        if not file_path.is_relative_to(images_dir):
            self.send_error(403, "Forbidden")
            return

        if not file_path.is_file():
            self.send_error(404, "Image not found")
            return

        content_type = (
            mimetypes.guess_type(str(file_path))[0]
            or "application/octet-stream"
        )

        with open(file_path, "rb") as file:
            data = file.read()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)    

    def do_GET(self):
        token = self.cookies()
        user = get_user(token)
        request_path = urlsplit(self.path)

        # Return login status for pages that need to check the current session.
        if request_path.path == "/api/session":
            response = {
                "authenticated": user is not None,
                "username": user.username if user else None
            }
            body = json.dumps(response).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Album search is only available to signed-in users.
        if request_path.path == "/api/albums":
            if not user:
                body = b'{"error":"Authentication required"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            query = parse_qs(request_path.query).get("q", [""])[0].strip()
            if not query:
                body = b'{"error":"Search query is required"}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            try:
                body = json.dumps(
                    {"albums": search_spotify_albums(query)}
                ).encode("utf-8")
                status = 200
            except (requests.RequestException, RuntimeError, KeyError, ValueError):
                body = b'{"error":"Spotify search is unavailable"}'
                status = 502

            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Login and registration pages must be available when signed out.
        if request_path.path in ("/login-page", "/login.html"):
            filename = "login.html"
            try:
                with open(BASE_DIR / filename, "rb") as file:
                    body = file.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_error(404, "login.html not found")
            return

        # Main website pages require a valid session.
        page_routes = {
            "/": "index.html",
            "/home": "index.html",
            "/index.html": "index.html",
            "/Search_page.html": "Search_page.html",
            "/Album_page.html": "Album_page.html",
            "/Settings_page.html": "Settings_page.html"
        }

        if request_path.path in page_routes:
            if not user:
                self.send_response(302)
                self.send_header("Location", "/login-page")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            filename = page_routes[request_path.path]
            try:
                with open(BASE_DIR / filename, "rb") as file:
                    body = file.read()

                # Fill in the signed-in user's name on the home page.
                if filename == "index.html":
                    username = html_module.escape(user.username).encode("utf-8")
                    body = body.replace(b"{{username}}", username)

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_error(404, f"{filename} not found")
            return

        if request_path.path.startswith("/Images/"):
            self.serve_static_file(request_path.path)
            return

        self.send_error(404, "Page not found")

    def do_POST(self):
        form = self.form()

        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]

        # Register
        if urlsplit(self.path).path == "/register":
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
            self.send_header("Location", "/home")
            self.end_headers()

        # Login
        elif urlsplit(self.path).path == "/login":
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
                self.send_header("Location", "/home")
                self.end_headers()

            else:
                self.send_response(401)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Invalid username or password.")

        # Logout
        elif urlsplit(self.path).path == "/logout":
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