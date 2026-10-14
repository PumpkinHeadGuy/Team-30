# Music Letterboxd

Music Letterboxd is a small Python web app with account login and Spotify album search.

## Setup

From the `Team-30` directory, install the project dependencies:

```powershell
py -m pip install -r .\requirements.txt
```

Create a local credentials file:

```powershell
Copy-Item .\Music_letterboxd\.env.example .\Music_letterboxd\.env
```

Open `Music_letterboxd/.env` and add the Spotify app credentials:

```env
SPOTIFY_CLIENT_ID=your-spotify-client-id
SPOTIFY_CLIENT_SECRET=your-spotify-client-secret
```

The `.env` file is local and ignored by Git. Never commit it or put the Client Secret in HTML or browser JavaScript.

## Run the app

```powershell
cd .\Music_letterboxd
py .\server.py
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) in a browser. Keep the server terminal open while using the app. Press `Ctrl+C` to stop it.

## Test the app

1. Create an account or log in.
2. Enter an album or artist, such as `Radiohead`.
3. Select **Search**.
4. Confirm that album covers, titles, artists, release years, and Spotify links appear.
5. Log out and confirm the next login starts with an empty search page.

## Project behavior

- The original login and account creation page is preserved.
- Successful login or registration opens the album search page.
- Logout returns to the login page.
- Searches return up to nine Spotify albums.
- Spotify credentials stay on the server.
- Local `.env`, database, and Python cache files are excluded from Git.

## Troubleshooting

If Spotify search reports that it is unavailable, check that `Music_letterboxd/.env` exists and contains valid credentials. Restart the server after changing `.env`.

Do not run the database file directly. Start the application with `py .\server.py`.
