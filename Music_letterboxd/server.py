import base64
import hashlib
import html as html_module
import json
import mimetypes
import os
import re
import secrets
import threading
import time
from datetime import date
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg
import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8080

SESSION_DAYS = 7
MAX_BODY_BYTES = 64 * 1024
SPOTIFY_ID_RE = re.compile(r"[A-Za-z0-9]{22}")
USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,30}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

load_dotenv(BASE_DIR / ".env")
DATABASE_URL = os.environ.get("DATABASE_URL")


def db():
    return psycopg.connect(DATABASE_URL)


def setup():
    """Create every table the app needs (safe to run on every startup)."""
    if not DATABASE_URL:
        raise SystemExit(
            "DATABASE_URL is not set. Add it to Music_letterboxd/.env "
            "(see README.md)."
        )
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL
            )
        """)
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # Older databases may not have an expiry column yet.
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                spotify_album_id TEXT NOT NULL,
                album_name TEXT NOT NULL,
                artist_name TEXT,
                release_year TEXT,
                cover_url TEXT,
                rating NUMERIC(2,1),
                body TEXT,
                liked BOOLEAN NOT NULL DEFAULT false,
                listened_on DATE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, spotify_album_id)
            )
        """)
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
    def __init__(self, album_id, title, artist, release_year, album_cover_url):
        # all album info will be got from spotify api and stored in the album class
        self.id = album_id
        self.title = title
        self.artist = artist
        self.release_year = release_year
        self.album_cover_url = album_cover_url
        self.ratings = []  # store a list of tuples (user_id, rating, review)
        self.avg = 0.0

    def add_rating(self, user_id, rating, review):
        self.ratings.append((user_id, rating, review))
        self.avg = sum(r[1] for r in self.ratings) / len(self.ratings)


class User:
    def __init__(self, user_id, username, spotify_id=None):
        self.id = user_id
        self.username = username
        self.spotify_id = spotify_id  # if wanting to link spotify account to user
        self.ratings = []  # tuples of (album, rating out of 5, review string)
        self.friends = []  # other user IDs
        self.lists = []  # lists of ratings

    def add_rating(self, album, rating, review):
        self.ratings.append((album, rating, review))

    def add_friend(self, friend_user):
        self.friends.append(friend_user)


# Passwords
def hash_password(password):
    salt = secrets.token_bytes(16)
    hashed = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ":" + hashed.hex()


def check_password(password, stored):
    try:
        salt_hex, old_hash = stored.split(":")
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    new_hash = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return secrets.compare_digest(new_hash.hex(), old_hash)


# Used so a login for an unknown username takes as long as a real one.
DUMMY_HASH = hash_password("not-a-real-password")


# Sessions
def new_session(user_id):
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < now()")
        conn.execute(
            """INSERT INTO sessions (token, user_id, expires_at)
               VALUES (%s, %s, now() + make_interval(days => %s))""",
            (token, user_id, SESSION_DAYS),
        )
    return token


def get_user(token):
    if not token:
        return None
    with db() as conn:
        row = conn.execute("""
            SELECT users.id, users.username
            FROM users JOIN sessions ON users.id = sessions.user_id
            WHERE sessions.token = %s
              AND (sessions.expires_at IS NULL OR sessions.expires_at > now())
        """, (token,)).fetchone()
    return User(row[0], row[1]) if row else None


# Spotify
_spotify_token = {"value": None, "expires": 0.0}
_spotify_lock = threading.Lock()


def spotify_access_token():
    """Return a cached client-credentials token, refreshing it when needed."""
    with _spotify_lock:
        if _spotify_token["value"] and time.time() < _spotify_token["expires"] - 30:
            return _spotify_token["value"]

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
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        _spotify_token["value"] = data["access_token"]
        _spotify_token["expires"] = time.time() + int(data.get("expires_in", 3600))
        return _spotify_token["value"]


def search_spotify_albums(query):
    token = spotify_access_token()
    response = requests.get(
        "https://api.spotify.com/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": query, "type": "album", "limit": 9},
        timeout=10,
    )
    if response.status_code == 401:
        # Token was revoked early; drop it so the next search fetches a new one.
        _spotify_token["value"] = None
    response.raise_for_status()

    albums = []
    for album in response.json().get("albums", {}).get("items", []):
        artists = album.get("artists") or []
        images = album.get("images") or []
        albums.append({
            "id": album.get("id"),
            "title": album.get("name"),
            "artist": artists[0].get("name") if artists else "Unknown artist",
            "release_year": (album.get("release_date") or "")[:4],
            "album_cover_url": images[0].get("url") if images else None,
            "spotify_url": (album.get("external_urls") or {}).get("spotify"),
        })
    return albums


# Validation helpers
def optional_text(value, max_len):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_len:
        raise ValueError("invalid text")
    return value


def valid_cover_url(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid cover")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "i.scdn.co":
        raise ValueError("invalid cover")
    return value


# Server
class Server(BaseHTTPRequestHandler):

    # ---------- low-level helpers ----------

    def send_json(self, status, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def cookies(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie"))
        except CookieError:
            return None
        return cookie["session"].value if "session" in cookie else None

    def read_body(self):
        """Return the request body, or None if its size is missing/invalid/too big."""
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if size < 0 or size > MAX_BODY_BYTES:
            return None
        return self.rfile.read(size)

    @staticmethod
    def session_cookie(token):
        return (
            "Set-Cookie",
            f"session={token}; HttpOnly; SameSite=Strict; Path=/; "
            f"Max-Age={SESSION_DAYS * 86400}",
        )

    @staticmethod
    def cleared_cookie():
        return ("Set-Cookie", "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")

    def serve_html(self, filename, user):
        try:
            with open(BASE_DIR / filename, "rb") as file:
                body = file.read()
        except FileNotFoundError:
            self.send_error(404, f"{filename} not found")
            return

        if user and b"{{username}}" in body:
            username = html_module.escape(user.username).encode("utf-8")
            body = body.replace(b"{{username}}", username)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Pages contain per-user content; don't let the back button show them
        # after logout.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_static_file(self, path):
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
            mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        )
        with open(file_path, "rb") as file:
            data = file.read()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def guarded(self, handler):
        """Turn unexpected errors into a clean 500 instead of a dropped connection."""
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except psycopg.Error as error:
            self.log_error("Database error: %s", error)
            self.safe_error()
        except Exception as error:  # noqa: BLE001 - last-resort handler
            self.log_error("Unexpected error: %s", error)
            self.safe_error()

    def safe_error(self):
        try:
            self.send_json(500, {"error": "Server error"})
        except Exception:  # headers may already be sent
            pass

    # ---------- API: profile ----------

    def save_profile(self, raw, user):
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
            if (
                not isinstance(spotify_id, str)
                or not SPOTIFY_ID_RE.fullmatch(spotify_id)
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
            try:
                cover_url = valid_cover_url(album.get("album_cover_url"))
            except ValueError:
                self.send_json(400, {"error": "Invalid album cover"})
                return

            seen_ids.add(spotify_id)
            favorite_rows.append((
                user.id, slot, spotify_id, title.strip(), artist.strip(),
                release_year, cover_url,
            ))

        with db() as conn:
            conn.execute("""
                INSERT INTO profiles (user_id, bio)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET bio = EXCLUDED.bio
            """, (user.id, bio.strip()))
            conn.execute(
                "DELETE FROM favorite_albums WHERE user_id = %s", (user.id,)
            )
            for favorite in favorite_rows:
                conn.execute("""
                    INSERT INTO favorite_albums (
                        user_id, slot, spotify_id, title, artist,
                        release_year, album_cover_url
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, favorite)

        self.send_json(200, {"ok": True})

    # ---------- API: reviews ----------

    def save_review(self, raw, user):
        try:
            d = json.loads(raw)
            if not isinstance(d, dict):
                raise ValueError("not an object")

            album_id = d["spotify_album_id"]
            title = d["album_name"]
            if not isinstance(album_id, str) or not SPOTIFY_ID_RE.fullmatch(album_id):
                raise ValueError("bad album id")
            if not isinstance(title, str) or not title.strip() or len(title) > 200:
                raise ValueError("bad title")

            artist = optional_text(d.get("artist_name"), 200)
            year = optional_text(d.get("release_year"), 10)
            cover = valid_cover_url(d.get("cover_url"))
            body = optional_text(d.get("body"), 5000)

            rating = d.get("rating")
            if rating is not None:
                rating = float(rating)
                # 0.5 - 5 in half-star steps (NaN fails the comparison too).
                if not 0.5 <= rating <= 5 or (rating * 2) % 1:
                    raise ValueError("bad rating")

            liked = d.get("liked")
            if liked is not None and not isinstance(liked, bool):
                raise ValueError("bad liked")

            listened_on = d.get("listened_on") or None
            if listened_on is not None:
                if not isinstance(listened_on, str):
                    raise ValueError("bad date")
                date.fromisoformat(listened_on)
        except (KeyError, ValueError, TypeError):
            self.send_json(400, {"error": "Invalid review"})
            return

        # Fields the client leaves out (null) keep their saved value, so saving
        # from one page can't wipe a rating or un-like an album by accident.
        with db() as conn:
            conn.execute("""
                INSERT INTO reviews (user_id, spotify_album_id, album_name,
                                     artist_name, release_year, cover_url,
                                     rating, body, liked, listened_on)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s::boolean, false), %s::date)
                ON CONFLICT (user_id, spotify_album_id) DO UPDATE SET
                    album_name   = EXCLUDED.album_name,
                    artist_name  = COALESCE(EXCLUDED.artist_name, reviews.artist_name),
                    release_year = COALESCE(EXCLUDED.release_year, reviews.release_year),
                    cover_url    = COALESCE(EXCLUDED.cover_url, reviews.cover_url),
                    rating       = COALESCE(EXCLUDED.rating, reviews.rating),
                    body         = COALESCE(EXCLUDED.body, reviews.body),
                    liked        = COALESCE(%s::boolean, reviews.liked),
                    listened_on  = COALESCE(EXCLUDED.listened_on, reviews.listened_on),
                    created_at   = now()
            """, (user.id, album_id, title.strip(), artist, year, cover,
                  rating, body, liked, listened_on, liked))
            albums_listened = conn.execute(
                "SELECT COUNT(*) FROM reviews WHERE user_id = %s", (user.id,)
            ).fetchone()[0]

        self.send_json(200, {"ok": True, "albums_listened": albums_listened})

    def list_reviews(self, params, user):
        if params.get("mine", [""])[0] == "1":
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
                        "created_at": row[9].isoformat(),
                    }
                    for row in rows
                ]
            })
            return

        sql = """SELECT u.username, r.spotify_album_id, r.album_name, r.artist_name,
                        r.release_year, r.cover_url, r.rating, r.body, r.liked, r.listened_on, r.created_at
                 FROM reviews r JOIN users u ON u.id = r.user_id"""
        album_id = params.get("album_id", [""])[0]
        if album_id:  # everyone's reviews of one album
            sql += " WHERE r.spotify_album_id = %s ORDER BY r.created_at DESC"
            args = (album_id,)
        else:  # latest reviews (for the home page)
            sql += " ORDER BY r.created_at DESC LIMIT 20"
            args = ()

        with db() as conn:
            rows = conn.execute(sql, args).fetchall()

        keys = ["username", "album_id", "album_name", "artist_name", "release_year",
                "cover_url", "rating", "body", "liked", "listened_on", "created_at"]
        reviews = []
        for row in rows:
            item = dict(zip(keys, row))
            item["rating"] = float(item["rating"]) if item["rating"] is not None else None
            item["listened_on"] = item["listened_on"].isoformat() if item["listened_on"] else None
            item["created_at"] = item["created_at"].isoformat()
            reviews.append(item)
        self.send_json(200, {"reviews": reviews})

    # ---------- API: discover ----------

    def discover(self):
        """Top albums by a popularity-weighted rating, each with 3 recent reviews."""
        PRIOR_WEIGHT = 3  # an album needs a few ratings before it can beat the average
        TOP_N = 10
        REVIEWS_PER_ALBUM = 3

        with db() as conn:
            stats = conn.execute("""
                SELECT spotify_album_id, MAX(album_name), MAX(artist_name),
                       MAX(release_year), MAX(cover_url),
                       COUNT(rating), AVG(rating), COUNT(*),
                       SUM(CASE WHEN liked THEN 1 ELSE 0 END)
                FROM reviews
                GROUP BY spotify_album_id
                HAVING COUNT(rating) > 0
            """).fetchall()

            if not stats:
                self.send_json(200, {"albums": []})
                return

            total_ratings = sum(row[5] for row in stats)
            overall_avg = sum(float(row[6]) * row[5] for row in stats) / total_ratings

            albums = []
            for row in stats:
                count, avg = row[5], float(row[6])
                # Weighted rating: pulls albums with few ratings toward the
                # site-wide average, so popular AND well-rated albums rise.
                score = (count * avg + PRIOR_WEIGHT * overall_avg) / (count + PRIOR_WEIGHT)
                albums.append({
                    "id": row[0],
                    "title": row[1],
                    "artist": row[2] or "Unknown artist",
                    "release_year": row[3] or "",
                    "album_cover_url": row[4],
                    "avg_rating": round(avg, 2),
                    "rating_count": count,
                    "review_count": row[7],
                    "likes": int(row[8] or 0),
                    "score": score,
                    "reviews": [],
                })

            albums.sort(key=lambda a: (a["score"], a["rating_count"]), reverse=True)
            albums = albums[:TOP_N]
            by_id = {a["id"]: a for a in albums}

            marks = ",".join(["%s"] * len(by_id))
            rows = conn.execute(f"""
                SELECT r.spotify_album_id, u.username, r.rating, r.body,
                       r.listened_on, r.created_at
                FROM reviews r JOIN users u ON u.id = r.user_id
                WHERE r.spotify_album_id IN ({marks})
                  AND r.body IS NOT NULL AND r.body <> ''
                ORDER BY r.created_at DESC
            """, list(by_id)).fetchall()

        for row in rows:
            album = by_id[row[0]]
            if len(album["reviews"]) < REVIEWS_PER_ALBUM:
                album["reviews"].append({
                    "username": row[1],
                    "rating": float(row[2]) if row[2] is not None else None,
                    "body": row[3],
                    "listened_on": row[4].isoformat() if row[4] else None,
                    "created_at": row[5].isoformat(),
                })

        for album in albums:
            del album["score"]
        self.send_json(200, {"albums": albums})

    # ---------- API: account ----------

    def get_account(self, user):
        with db() as conn:
            row = conn.execute(
                "SELECT username, email FROM users WHERE id = %s", (user.id,)
            ).fetchone()
        if not row:
            self.send_json(404, {"error": "Account not found"})
            return
        self.send_json(200, {"username": row[0], "email": row[1] or ""})

    def save_account(self, raw, user):
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("not an object")
            username = payload.get("username", user.username)
            email = payload.get("email") or None
            if not isinstance(username, str):
                raise ValueError("bad username")
            username = username.strip()
            if username != user.username and not USERNAME_RE.fullmatch(username):
                self.send_json(400, {
                    "error": "Username must be 3-30 characters: letters, "
                             "numbers, dot, dash or underscore."
                })
                return
            if email is not None:
                if (not isinstance(email, str) or len(email) > 254
                        or not EMAIL_RE.fullmatch(email.strip())):
                    self.send_json(400, {"error": "Enter a valid email address."})
                    return
                email = email.strip()
        except (ValueError, TypeError):
            self.send_json(400, {"error": "Invalid account data"})
            return

        try:
            with db() as conn:
                conn.execute(
                    "UPDATE users SET username = %s, email = %s WHERE id = %s",
                    (username, email, user.id),
                )
        except psycopg.errors.UniqueViolation:
            self.send_json(409, {"error": "Username already exists."})
            return
        self.send_json(200, {"ok": True, "username": username})

    def delete_account(self, user):
        with db() as conn:
            for table in ("reviews", "favorite_albums", "profiles", "sessions"):
                conn.execute(f"DELETE FROM {table} WHERE user_id = %s", (user.id,))
            conn.execute("DELETE FROM users WHERE id = %s", (user.id,))
        self.send_json(200, {"ok": True}, [self.cleared_cookie()])

    # ---------- GET ----------

    def do_GET(self):
        self.guarded(self.handle_get)

    def handle_get(self):
        request_path = urlsplit(self.path)
        path = request_path.path

        # Images are public and don't need a session lookup.
        if path.startswith("/Images/"):
            self.serve_static_file(path)
            return

        user = get_user(self.cookies())

        if path == "/api/session":
            self.send_json(200, {
                "authenticated": user is not None,
                "username": user.username if user else None,
            })
            return

        if path.startswith("/api/"):
            if not user:
                self.send_json(401, {"error": "Authentication required"})
                return
            params = parse_qs(request_path.query)

            if path == "/api/profile":
                self.get_profile(user)
            elif path == "/api/albums":
                self.search_albums(params)
            elif path == "/api/reviews":
                self.list_reviews(params, user)
            elif path == "/api/account":
                self.get_account(user)
            elif path == "/api/discover":
                self.discover()
            else:
                self.send_json(404, {"error": "Not found"})
            return

        # Login page: visible when signed out; signed-in users go to Home.
        if path in ("/login-page", "/login.html", "/index.html"):
            if user:
                self.redirect("/home")
            else:
                self.serve_html("index.html", None)
            return

        # Main website pages require a valid session.
        page_routes = {
            "/": "home.html",
            "/home": "home.html",
            "/profile": "profile.html",
            "/profile.html": "profile.html",
            "/Account_page.html": "profile.html",
            "/edit-profile": "edit_profile.html",
            "/edit_profile.html": "edit_profile.html",
            "/Search_page.html": "Search_page.html",
            "/Album_page.html": "Album_page.html",
            "/Settings_page.html": "Settings_page.html",
            "/discover": "Discover_page.html",
            "/Discover_page.html": "Discover_page.html",
        }
        if path in page_routes:
            if not user:
                self.redirect("/login-page")
                return
            self.serve_html(page_routes[path], user)
            return

        self.send_error(404, "Page not found")

    def get_profile(self, user):
        with db() as conn:
            profile = conn.execute(
                "SELECT bio FROM profiles WHERE user_id = %s", (user.id,)
            ).fetchone()
            favorites = conn.execute("""
                SELECT spotify_id, title, artist, release_year,
                       album_cover_url, slot
                FROM favorite_albums
                WHERE user_id = %s
                ORDER BY slot
            """, (user.id,)).fetchall()
            albums_listened = conn.execute(
                "SELECT COUNT(DISTINCT spotify_album_id) FROM reviews WHERE user_id = %s",
                (user.id,),
            ).fetchone()[0]

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
                    "slot": row[5],
                }
                for row in favorites
            ],
        })

    def search_albums(self, params):
        query = params.get("q", [""])[0].strip()
        if not query:
            self.send_json(400, {"error": "Search query is required"})
            return
        if len(query) > 200:
            self.send_json(400, {"error": "Search query is too long"})
            return
        try:
            albums = search_spotify_albums(query)
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as error:
            # Print the real reason in the server terminal (never sent to the browser).
            detail = ""
            response = getattr(error, "response", None)
            if response is not None:
                detail = f" | HTTP {response.status_code}: {response.text[:300]}"
            self.log_error("Spotify search failed: %s%s", error, detail)
            self.send_json(502, {"error": "Spotify search is unavailable"})
            return
        self.send_json(200, {"albums": albums})

    # ---------- POST ----------

    def do_POST(self):
        self.guarded(self.handle_post)

    def handle_post(self):
        raw = self.read_body()
        if raw is None:
            self.send_json(413, {"error": "Request body is missing or too large"})
            return
        path = urlsplit(self.path).path

        if path == "/register":
            self.register(raw)
            return
        if path == "/login":
            self.login(raw)
            return
        if path == "/logout":
            self.logout()
            return

        # Everything below needs a signed-in user.
        user = get_user(self.cookies())
        if not user:
            self.send_json(401, {"error": "Authentication required"})
            return

        if path == "/api/profile":
            self.save_profile(raw, user)
        elif path == "/api/reviews":
            self.save_review(raw, user)
        elif path == "/api/account":
            self.save_account(raw, user)
        elif path == "/api/account/delete":
            self.delete_account(user)
        else:
            self.send_json(404, {"error": "Not found"})

    @staticmethod
    def parse_credentials(raw):
        form = parse_qs(raw.decode("utf-8", "replace"))
        username = form.get("username", [""])[0].strip()
        password = form.get("password", [""])[0]
        return username, password

    def register(self, raw):
        username, password = self.parse_credentials(raw)

        if not USERNAME_RE.fullmatch(username):
            self.send_text(400, "Username must be 3-30 characters: letters, "
                                "numbers, dot, dash or underscore.")
            return
        if not 8 <= len(password) <= 128:
            self.send_text(400, "Password must be 8-128 characters.")
            return

        try:
            with db() as conn:
                user_id = conn.execute(
                    "INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id",
                    (username, hash_password(password)),
                ).fetchone()[0]
        except psycopg.errors.UniqueViolation:
            self.send_text(409, "Username already exists.")
            return

        token = new_session(user_id)
        self.send_json(200, {"ok": True}, [self.session_cookie(token)])

    def login(self, raw):
        username, password = self.parse_credentials(raw)

        user = None
        if username and len(username) <= 100 and len(password) <= 128:
            with db() as conn:
                user = conn.execute(
                    "SELECT id, password FROM users WHERE username = %s",
                    (username,),
                ).fetchone()

        # Always run one hash check so timing doesn't reveal valid usernames.
        stored = user[1] if user else DUMMY_HASH
        password_ok = check_password(password[:128], stored)

        if user and password_ok:
            token = new_session(user[0])
            self.send_json(200, {"ok": True}, [self.session_cookie(token)])
        else:
            self.send_text(401, "Invalid username or password.")

    def logout(self):
        token = self.cookies()
        if token:
            with db() as conn:
                conn.execute("DELETE FROM sessions WHERE token = %s", (token,))
        self.send_json(200, {"ok": True}, [self.cleared_cookie()])


def main():
    setup()
    server = ThreadingHTTPServer((HOST, PORT), Server)
    print(f"Server running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()