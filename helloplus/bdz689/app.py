import base64
import requests

CLIENT_ID = "81389b37b6fc4cc49f2a68fbcc2b9515"
CLIENT_SECRET = "840383261d2b42a8b23b1987e9772a4d"

# Function to get Spotify access token using requests, base64 basic auth, and client_credentials grant type
def get_spotify_access_token():
    # Encode the client ID and secret using base64
    encoded_credentials = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

    # Set up the headers for the request
    headers = {
        "Authorization": f"Basic {encoded_credentials}"
    }

    # Set up the data for the request
    data = {
        "grant_type": "client_credentials"
    }

    # Make the request to the Spotify API
    response = requests.post("https://accounts.spotify.com/api/token", headers=headers, data=data)

    # Return the access token from the response
    return response.json().get("access_token")

# Function search_spotify(query, token) to GET https://api.spotify.com/v1/search for albums with limit 5 and return items
def search_spotify(query, token):
    # Set up the headers for the request
    headers = {
        "Authorization": f"Bearer {token}"
    }

    # Set up the parameters for the request
    params = {
        "q": query,
        "type": "album",
        "limit": 5
    }

    # Make the request to the Spotify API
    response = requests.get("https://api.spotify.com/v1/search", headers=headers, params=params)

    # Return the items from the response
    return response.json().get("albums", {}).get("items", [])

# Main function: fetch token, prompt user for search query, print top 5 albums with title and artist,
# and prompt user to rate a selected album 1-5 stars with a text review
def main():
    # Fetch the Spotify access token
    token = get_spotify_access_token()

    # Prompt the user for a search query
    query = input("Enter a search query for albums: ")

    # Search for albums using the query and access token
    albums = search_spotify(query, token)

    # Print the top 5 albums with title and artist
    print("\nTop 5 Albums:")
    for i, album in enumerate(albums):
        title = album.get("name")
        artist = album.get("artists")[0].get("name") if album.get("artists") else "Unknown Artist"
        print(f"{i + 1}. {title} by {artist}")

    # Prompt the user to select a album to rate
    selected_index = int(input("\nSelect a album to rate (1-5): ")) - 1

    if 0 <= selected_index < len(albums):
        selected_album = albums[selected_index]
        rating = int(input("Rate the album (1-5 stars): "))
        review = input("Write a text review: ")

        # Print the user's rating and review
        print(f"\nYou rated '{selected_album.get('name')}' by {selected_album.get('artists')[0].get('name')} with {rating} stars.")
        print(f"Your review: {review}")
    else:
        print("Invalid selection. Please run the program again.")

if __name__ == "__main__":
    main()