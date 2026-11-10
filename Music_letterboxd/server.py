from os import path
import psycopg
import hashlib
import secrets
import json
import base64
import os
import re
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
DATABASE_URL = os.environ.get("DATABASE_URL")

def db():
    return psycopg.connect(DATABASE_URL)

def setup():
    # Tables are created once in the Supabase SQL Editor.
    # This just confirms the connection works at startup.
    with db() as conn:
        conn.execute("SELECT 1")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                bio TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS favorite_albums (
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                slot SMALLINT NOT NULL CHECK (slot BETWEEN 1 AND 4),
                spotify_id TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                release_year TEXT NOT NULL DEFAULT '',
                album_cover_url TEXT,
                PRIMARY KEY (user_id, slot),
                UNIQUE (user_id, spotify_id)
            )
        """)


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
    with db() as conn:
        conn.execute("INSERT INTO sessions (token, user_id) VALUES (%s, %s)",
                     (token, user_id))
    return token

def get_user(token):
    if not token:
        return None
    with db() as conn:
        row = conn.execute("""
            SELECT users.id, users.username
            FROM users JOIN sessions ON users.id = sessions.user_id
            WHERE sessions.token = %s
        """, (token,)).fetchone()
    return User(row[0], row[1]) if row else None

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

    def save_profile(self, raw):
        user = get_user(self.cookies())
        if not user:
            self.send_json(401, {"error": "Authentication required"})
            return

        if len(raw) > 16384:
            self.send_json(413, {"error": "Profile data is too large"})
            return

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Invalid JSON request"})
            return

        if not isinstance(payload, dict):
            self.send_json(400, {"error": "Invalid profile data"})
            return

        bio = payload.get("bio", "")
        favorites = payload.get("favorites")
        if (
            not isinstance(bio, str)
            or len(bio) > 280
            or not isinstance(favorites, list)
            or len(favorites) != 4
        ):
            self.send_json(400, {"error": "Invalid profile data"})
            return

        favorite_rows = []
        seen_ids = set()
        for slot, album in enumerate(favorites, start=1):
            if album is None:
                continue
            if not isinstance(album, dict):
                self.send_json(400, {"error": "Invalid favorite album"})
                return

            spotify_id = album.get("id")
            title = album.get("title")
            artist = album.get("artist")
            release_year = album.get("release_year") or ""
            cover_url = album.get("album_cover_url")
            if (
                not isinstance(spotify_id, str)
                or not re.fullmatch(r"[A-Za-z0-9]{22}", spotify_id)
                or spotify_id in seen_ids
                or not isinstance(title, str)
                or not title.strip()
                or len(title) > 200
                or not isinstance(artist, str)
                or not artist.strip()
                or len(artist) > 200
                or not isinstance(release_year, str)
                or len(release_year) > 10
            ):
                self.send_json(400, {"error": "Invalid favorite album"})
                return
            if cover_url is not None:
                if not isinstance(cover_url, str) or len(cover_url) > 2048:
                    self.send_json(400, {"error": "Invalid album cover"})
                    return
                parsed_cover = urlsplit(cover_url)
                if (
                    parsed_cover.scheme != "https"
                    or parsed_cover.hostname != "i.scdn.co"
                ):
                    self.send_json(400, {"error": "Invalid album cover"})
                    return

            seen_ids.add(spotify_id)
            favorite_rows.append((
                user.id, slot, spotify_id, title.strip(), artist.strip(),
                release_year, cover_url
            ))

        try:
            with db() as conn:
                conn.execute("""
                    INSERT INTO profiles (user_id, bio)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET bio = EXCLUDED.bio
                """, (user.id, bio.strip()))
                conn.execute(
                    "DELETE FROM favorite_albums WHERE user_id = %s",
                    (user.id,)
                )
                for favorite in favorite_rows:
                    conn.execute("""
                        INSERT INTO favorite_albums (
                            user_id, slot, spotify_id, title, artist,
                            release_year, album_cover_url
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, favorite)
        except psycopg.Error as error:
            self.log_error("Could not save profile: %s", error)
            self.send_json(500, {"error": "Could not save profile"})
            return

        self.send_json(200, {"ok": True})

    def save_review(self, raw):
        user = get_user(self.cookies())
        if not user:
            self.send_json(401, {"error": "Authentication required"})
            return
        try:
            d = json.loads(raw)
            album_id, title = d["spotify_album_id"], d["album_name"]
            rating = d.get("rating")
            if rating is not None:
                rating = float(rating)
                if not 0.5 <= rating <= 5:
                    raise ValueError
        except (KeyError, ValueError, TypeError):
            self.send_json(400, {"error": "Invalid review"})
            return

        with db() as conn:
            conn.execute("""
                INSERT INTO reviews (user_id, spotify_album_id, album_name, artist_name,
                                     release_year, cover_url, rating, body, liked, listened_on)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id, spotify_album_id) DO UPDATE SET
                    rating = EXCLUDED.rating, body = EXCLUDED.body,
                    liked = EXCLUDED.liked, listened_on = EXCLUDED.listened_on,
                    created_at = now()
            """, (user.id, album_id, title, d.get("artist_name"), d.get("release_year"),
                  d.get("cover_url"), rating, d.get("body"), bool(d.get("liked")),
                  d.get("listened_on") or None))
        self.send_json(200, {"ok": True})

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

        if request_path.path == "/api/profile":
            if not user:
                self.send_json(401, {"error": "Authentication required"})
                return
            try:
                with db() as conn:
                    profile = conn.execute(
                        "SELECT bio FROM profiles WHERE user_id = %s",
                        (user.id,)
                    ).fetchone()
                    favorites = conn.execute("""
                        SELECT spotify_id, title, artist, release_year,
                               album_cover_url, slot
                        FROM favorite_albums
                        WHERE user_id = %s
                        ORDER BY slot
                    """, (user.id,)).fetchall()
                    albums_listened = conn.execute("""
                        SELECT COUNT(DISTINCT spotify_album_id)
                        FROM reviews
                        WHERE user_id = %s
                    """, (user.id,)).fetchone()[0]
            except psycopg.Error as error:
                self.log_error("Could not load profile: %s", error)
                self.send_json(500, {"error": "Could not load profile"})
                return

            self.send_json(200, {
                "username": user.username,
                "bio": profile[0] if profile else "",
                "albums_listened": albums_listened,
                "favorites": [
                    {
                        "id": row[0],
                        "title": row[1],
                        "artist": row[2],
                        "release_year": row[3],
                        "album_cover_url": row[4],
                        "slot": row[5]
                    }
                    for row in favorites
                ]
            })
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

        if request_path.path == "/api/reviews":
            if not user:
                self.send_json(401, {"error": "Authentication required"})
                return
            params = parse_qs(request_path.query)
            album_id = params.get("album_id", [""])[0]

            if params.get("mine", [""])[0] == "1":
                try:
                    with db() as conn:
                        rows = conn.execute("""
                            SELECT spotify_album_id, album_name, artist_name,
                                   release_year, cover_url, rating, body,
                                   listened_on, liked, created_at
                            FROM reviews
                            WHERE user_id = %s
                            ORDER BY COALESCE(listened_on, created_at::date) DESC,
                                     created_at DESC
                        """, (user.id,)).fetchall()
                except psycopg.Error as error:
                    self.log_error("Could not load profile reviews: %s", error)
                    self.send_json(500, {"error": "Could not load reviews"})
                    return

                self.send_json(200, {
                    "reviews": [
                        {
                            "spotify_id": row[0],
                            "title": row[1],
                            "artist": row[2] or "",
                            "release_year": row[3] or "",
                            "album_cover_url": row[4],
                            "rating": float(row[5]) if row[5] is not None else 0,
                            "review": row[6] or "",
                            "listened_at": row[7].isoformat() if row[7] else None,
                            "liked": bool(row[8]),
                            "created_at": row[9].isoformat()
                        }
                        for row in rows
                    ]
                })
                return

            sql = """SELECT u.username, r.spotify_album_id, r.album_name, r.artist_name,
                            r.cover_url, r.rating, r.body, r.liked, r.listened_on, r.created_at
                     FROM reviews r JOIN users u ON u.id = r.user_id"""
            if params.get("mine", [""])[0] == "1":      # the logged-in user's diary
                sql += " WHERE r.user_id = %s ORDER BY r.created_at DESC"; args = (user.id,)
            elif album_id:                               # everyone's reviews of one album
                sql += " WHERE r.spotify_album_id = %s ORDER BY r.created_at DESC"; args = (album_id,)
            else:                                        # latest reviews (for the home page)
                sql += " ORDER BY r.created_at DESC LIMIT 20"; args = ()

            with db() as conn:
                rows = conn.execute(sql, args).fetchall()

            keys = ["username", "album_id", "album_name", "artist_name", "cover_url",
                    "rating", "body", "liked", "listened_on", "created_at"]
            reviews = []
            for row in rows:
                item = dict(zip(keys, row))
                item["rating"] = float(item["rating"]) if item["rating"] is not None else None
                item["listened_on"] = item["listened_on"].isoformat() if item["listened_on"] else None
                item["created_at"] = item["created_at"].isoformat()
                reviews.append(item)
            self.send_json(200, {"reviews": reviews})
            return

        # Login and registration pages must be available when signed out.
        if request_path.path in ("/login-page", "/login.html"):
            filename = "index.html"
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
            "/": "home.html",
            "/home": "home.html",
            "/index.html": "home.html",
            "/profile": "profile.html",
            "/profile.html": "profile.html",
            "/Account_page.html": "profile.html",
            "/edit-profile": "edit_profile.html",
            "/edit_profile.html": "edit_profile.html",
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
                if filename == "home.html":
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
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        post_path = urlsplit(self.path).path

        if post_path == "/api/profile":
            self.save_profile(raw)
            return

        if post_path == "/api/reviews":
            self.save_review(raw)
            return

        form = parse_qs(raw.decode())
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        
        # Register
        if post_path == "/register":
            hashed = hash_password(password)

            try:
                with db() as conn:
                    user_id = conn.execute(
                        "INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id",
                        (username, hashed)
                    ).fetchone()[0]
            
            except psycopg.errors.UniqueViolation:
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
        elif post_path == "/login":
            with db() as conn:
                user = conn.execute("SELECT id, password FROM users WHERE username = %s",
                                    (username,)).fetchone()

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
        elif post_path == "/logout":
            token = self.cookies()

            with db() as conn:
                conn.execute("DELETE FROM sessions WHERE token = %s", (token,))

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