from io import BytesIO
from librespot.audio.decoders import AudioQuality, VorbisOnlyAudioQuality
from librespot.core import ApiClient, Session
from librespot.metadata import TrackId, EpisodeId
from pydub import AudioSegment
from pathlib import Path

import json
import os
import re
import requests
import shutil
import time

class Respot:

    def __init__(self,
                 config_dir,
                 force_premium,
                 credentials,
                 output_format,
                 antiban_wait_time):
        self.config_dir = config_dir
        self.credentials = credentials
        self.force_premium = force_premium
        self.output_format = output_format
        self.antiban_wait_time = antiban_wait_time
        self.auth = RespotAuth(self.credentials, self.force_premium)
        self.request = None
        self.progress_bar = False
    
    def is_authenticated(self, username=None, password=None):
        if self.auth.login(username, password):
            self.request = RespotRequest(self.auth)
            return True
        return False
    
    def download(self, track_id, output_path, make_dirs=True):
        handler = RespotTrackHandler(self.auth, self.output_format, self.antiban_wait_time, self.auth.quality)
        handler.download_audio(self.set_progress_bar, track_id, output_path, make_dirs)

    def set_progress_bar(self, dict):
        self.progress_bar = dict

class RespotAuth:

    def __init__(self, credentials, force_premium):
        self.credentials = credentials
        self.force_premium = force_premium
        self.session = None
        self.token = None
        self.token_your_libary = None
        self.quality = None

    def login(self, username, password):
        """Authenticates with Spotify and saves credentials to a file"""
        self._ensure_credentials_directory()
        
        if self._has_stored_credentials():
            return self._authenticate_with_stored_credentials()
        elif username and password:
            return self._authenticate_with_user_pass(username, password)
        else:
            return False

    # librespot does not have a function to store credentials.json correctly
    def _persist_credentials_file(self):
        Path("credentials.json").rename(self.credentials)

    def _ensure_credentials_directory(self):
        self.credentials.parent.mkdir(parents=True, exist_ok=True)
        
    def _has_stored_credentials(self):
        return self.credentials.is_file()

    def _authenticate_with_stored_credentials(self):
        try:
            self.refresh_token()
            self._check_premium()
            return True
        # Assuming RuntimeError is raised in init_token() or check_premium()
        except RuntimeError:
            return False

    def _authenticate_with_user_pass(self, username, password):
        try:
            self.session = Session.Builder().user_pass(
                username, password).create()
            self._persist_credentials_file()
            self._check_premium()
            return True
        # Assuming RuntimeError is raised in refresh_token(), check_premium() or during session creation
        except RuntimeError:
            return False

    def refresh_token(self):
        self.session = Session.Builder().stored_file(
            stored_credentials=str(self.credentials)).create()
        # Remove auto generated credentials.json
        local_credentials = Path("credentials.json")
        if local_credentials.exists():
            local_credentials.unlink()

        self.token = self.session.tokens().get("user-read-email")
        self.token_your_libary = self.session.tokens().get("user-library-read")
        return (self.token, self.token_your_libary)

    def _check_premium(self):
        """If user has Spotify premium, return true"""
        if not self.session:
            raise RuntimeError("You must login first")

        if self.session.get_user_attribute("type") == "premium" or self.force_premium:
            self.quality = AudioQuality.VERY_HIGH
            print("[ DETECTED PREMIUM ACCOUNT - USING VERY_HIGH QUALITY ]\n")
        else:
            self.quality = AudioQuality.HIGH
            print("[ DETECTED FREE ACCOUNT - USING HIGH QUALITY ]\n")

class RespotRequest:

    def __init__(self, auth: RespotAuth):
        self.auth = auth
        self.token = auth.token
        self.token_your_libary = auth.token_your_libary
    
    def authorized_get_request(self, url, token_bearer=None, retry_count=0, **kwargs):
        """Makes a request to the Spotify API with the authorization token"""
        if retry_count > 3:
            raise RuntimeError("Connection Error: Too many retries")
        
        if token_bearer is None:
                token_bearer = self.token

        try:
            response = requests.get(url,
                                    headers={"Authorization": f"Bearer {token_bearer}"},
                                    **kwargs)
            if response.status_code == 401:
                print("Token expired, refreshing...")
                tokens = self.auth.refresh_token()
                self.token = tokens[0]
                self.token_your_libary = tokens[1]
                return self.authorized_get_request(url, token_bearer, retry_count + 1, **kwargs)
            return response
        except requests.exceptions.ConnectionError:
            return self.authorized_get_request(url, token_bearer, retry_count + 1, **kwargs)
    
    def get_track_info(self, track_id):
        """Retrieves metadata for downloaded songs"""
        try:

            info = json.loads(
                self.authorized_get_request(
                    "https://api.spotify.com/v1/tracks?ids="
                    + track_id
                    + "&market=from_token"
                ).text
            )

            # Sum the size of the images, compares and saves the index of the
            # largest image size
            sum_total = []
            for sum_px in info['tracks'][0]['album']['images']:
                sum_total.append(sum_px['height'] + sum_px['width'])

            img_index = sum_total.index(max(sum_total)) if sum_total else -1

            artist_id = info['tracks'][0]['artists'][0]['id']
            artists = []
            for data in info["tracks"][0]["artists"]:
                artists.append(RespotUtils.sanitize_data(data["name"]))
            artist_name = artists
            album_artist = RespotUtils.sanitize_data(info["tracks"][0]["album"]["artists"][0]['name'])
            album_name = RespotUtils.sanitize_data(info["tracks"][0]["album"]["name"])
            song_name = RespotUtils.sanitize_data(info["tracks"][0]["name"])
            image_url = info["tracks"][0]["album"]["images"][img_index]["url"] if img_index >= 0 else None
            release_year = info["tracks"][0]["album"]["release_date"].split("-")[0]
            disc_number = info["tracks"][0]["disc_number"]
            track_number = info["tracks"][0]["track_number"]
            scraped_song_id = info["tracks"][0]["id"]
            is_playable = info["tracks"][0]["is_playable"]
            release_date = info["tracks"][0]["album"]["release_date"]

            # TODO: Implement genre checking
            # TODO: Implement Track dataclass
            return {'id': track_id,
                    'artist_id': artist_id,
                    'artist_name': RespotUtils.conv_artist_format(artist_name),
                    'album_artist': album_artist,
                    'album_name': album_name,
                    'audio_name': song_name,
                    'image_url': image_url,
                    'release_year': release_year,
                    'disc_number': disc_number,
                    'audio_number': track_number,
                    'scraped_song_id': scraped_song_id,
                    'is_playable': is_playable,
                    'release_date': release_date}
        except Exception as e:
            print("###   get_track_info - FAILED TO QUERY METADATA   ###")
            print("track_id:", track_id)
            print(e)
            return None

    def get_all_user_playlists(self):
        """Returns list of users playlists"""
        playlists = []
        limit = 50
        offset = 0

        while True:
            resp = self.authorized_get_request(
                "https://api.spotify.com/v1/me/playlists",
                params={"limit": limit, "offset": offset}).json()
            offset += limit
            playlists.extend(resp["items"])

            if len(resp["items"]) < limit:
                break

        return {"playlists": playlists}

    def get_playlist_songs(self, playlist_id):
        """returns list of songs in a playlist"""
        offset = 0
        limit = 100
        audios = []

        while True:
            resp = self.authorized_get_request(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
                params={"limit": limit, "offset": offset},
            ).json()
            offset += limit
            for song in resp["items"]:
                if song["track"] is not None:
                    audios.append({"id": song["track"]["id"],
                                   "name": song["track"]["name"],
                                   "artist": song["track"]["artists"][0]["name"]})

            if len(resp["items"]) < limit:
                break
        return audios

    def get_playlist_info(self, playlist_id):
        """Returns information scraped from playlist"""
        resp = self.authorized_get_request(
            f"https://api.spotify.com/v1/playlists/{playlist_id}?fields=name,owner(display_name)&market=from_token"
        ).json()
        return {
            "name": resp["name"].strip(),
            "owner": resp["owner"]["display_name"].strip(),
            "id": playlist_id}

    def get_album_songs(self, album_id):
        """Returns album tracklist"""
        audios = []
        offset = 0
        limit = 50
        include_groups = "album,compilation"

        while True:
            resp = self.authorized_get_request(
                f"https://api.spotify.com/v1/albums/{album_id}/tracks",
                params={"limit": limit,
                        "include_groups": include_groups,
                        "offset": offset},
            ).json()
            offset += limit
            for song in resp["items"]:
                audios.append({"id": song["id"],
                               "name": song["name"],
                               "number": song["track_number"],
                               "disc_number": song["disc_number"]})

            if len(resp["items"]) < limit:
                break

        return audios

    def get_album_info(self, album_id):
        """Returns album name"""
        resp = self.authorized_get_request(
            f"https://api.spotify.com/v1/albums/{album_id}"
        ).json()

        artists = []
        for artist in resp["artists"]:
            artists.append(RespotUtils.sanitize_data(artist["name"]))

        if m := re.search("(\\d{4})", resp["release_date"]):
            return {
                "artists": RespotUtils.conv_artist_format(artists),
                "name": resp["name"],
                "total_tracks": resp["total_tracks"],
                "release_date": m.group(1)}
        else:
            return {
                "artists": RespotUtils.conv_artist_format(artists),
                "name": resp["name"],
                "total_tracks": resp["total_tracks"],
                "release_date": resp["release_date"]}

    def get_artist_albums(self, artists_id):
        """returns list of albums in an artist"""

        offset = 0
        limit = 50
        include_groups = "album,compilation,single"

        albums = []
        resp = self.authorized_get_request(
            f"https://api.spotify.com/v1/artists/{artists_id}/albums",
            params={"limit": limit,
                    "include_groups": include_groups,
                    "offset": offset},
        ).json()
        print("###   Albums" "###")
        for album in resp["items"]:
            if m := re.search("(\\d{4})", album["release_date"]):
                print(" #", album["name"])
                albums.append({"id": album["id"],
                               "name": album["name"],
                               "release_date": m.group(1),
                               "total_tracks": album["total_tracks"]})
            else:
                print(" #", album["name"])
                albums.append({"id": album["id"],
                               "name": album["name"],
                               "release_date": album["release_date"],
                               "total_tracks": album["total_tracks"]})
        return resp["items"]

    def get_liked_tracks(self):
        """Returns user's saved tracks"""
        songs = []
        offset = 0
        limit = 50

        while True:
            resp = self.authorized_get_request(
                "https://api.spotify.com/v1/me/tracks",
                self.token_your_libary,
                params={"limit": limit, "offset": offset}).json()
            offset += limit
            for song in resp["items"]:
                songs.append({'id': song["track"]["id"],
                              'name': song["track"]["name"],
                              'artist': song["track"]["artists"][0]["name"]})

            if len(resp["items"]) < limit:
                break

        return songs

    def get_artist_info(self, artist_id):
        """ Retrieves metadata for downloaded songs """

        try:
            info = json.loads(
                self.authorized_get_request(
                    "https://api.spotify.com/v1/artists/"
                    + artist_id
                ).text
            )

            return {
                "name": RespotUtils.sanitize_data(
                    info["name"]), "genres": RespotUtils.conv_artist_format(
                    info["genres"])}
        except Exception as e:
            print("###   get_artist_info - FAILED TO QUERY METADATA   ###")
            print("artist_id:", artist_id)
            print(e)

    def get_episode_info(self, episode_id_str):
        info = json.loads(
            self.authorized_get_request(
                "https://api.spotify.com/v1/episodes/" + episode_id_str
            ).text
        )
        if not info:
            return None
        sum_total = []
        for sum_px in info['images']:
            sum_total.append(sum_px['height'] + sum_px['width'])

        img_index = sum_total.index(max(sum_total)) if sum_total else -1

        show_id = info['show']['id']
        show_publisher = info['show']['publisher']
        show_name = RespotUtils.sanitize_data(info['show']['name'])
        episode_name = RespotUtils.sanitize_data(info["name"])
        image_url = info["images"][img_index]["url"] if img_index >= 0 else None
        release_year = info["release_date"].split("-")[0]
        scraped_episode_id = ["id"]
        is_playable = info["is_playable"]
        release_date = info["release_date"]

        return {'id': episode_id_str,
                'artist_id': show_id,
                'artist_name': show_publisher,
                'show_name': show_name,
                'audio_name': episode_name,
                'image_url': image_url,
                'release_year': release_year,
                'disc_number': None,
                'audio_number': None,
                'scraped_episode_id': scraped_episode_id,
                'is_playable': is_playable,
                'release_date': release_date}

    def get_show_episodes(self, show_id_str):
        """returns episodes of a show"""
        episodes = []
        offset = 0
        limit = 50

        while True:
            resp = self.authorized_get_request(
                f"https://api.spotify.com/v1/shows/{show_id_str}/episodes",
                params={"limit": limit, "offset": offset},
            ).json()
            offset += limit
            for episode in resp["items"]:
                episodes.append({"id": episode["id"],
                                 "name": episode["name"],
                                 "release_date": episode["release_date"]})

            if len(resp["items"]) < limit:
                break

        return episodes

    def get_show_info(self, show_id_str):
        """returns show info"""
        resp = self.authorized_get_request(
            f"https://api.spotify.com/v1/shows/{show_id_str}"
        ).json()
        return {"name": RespotUtils.sanitize_data(resp["name"]),
                "publisher": resp["publisher"],
                "id": resp["id"],
                'total_episodes': resp["total_episodes"]}
    
    def search(self, search_term, search_limit):
        """Searches Spotify's API for relevant data"""

        resp = self.authorized_get_request(
            "https://api.spotify.com/v1/search",
            params={
                "limit": search_limit,
                "offset": "0",
                "q": search_term,
                "type": "track,album,playlist,artist"
            }
        )
        ret_tracks = []
        tracks = resp.json()["tracks"]["items"]
        if len(tracks) > 0:
            for track in tracks:
                if track["explicit"]:
                    explicit = "[E]"
                else:
                    explicit = ""
                ret_tracks.append({'id': track['id'], 'name': explicit + track["name"],
                                   "artists": ','.join([artist['name'] for artist in track['artists']])})
        ret_albums = []
        albums = resp.json()["albums"]["items"]
        if len(albums) > 0:
            for album in albums:
                _year = re.search("(\\d{4})", album["release_date"]).group(1)
                ret_albums.append({'name': album['name'],
                                   'year': _year,
                                   'artists': ','.join([artist['name'] for artist in album['artists']]),
                                   'total_tracks': album['total_tracks'],
                                   'id': album['id']})

        ret_playlists = []
        playlists = resp.json()["playlists"]["items"]
        for playlist in playlists:
            ret_playlists.append({'name': playlist['name'],
                                  'owner': playlist['owner']['display_name'],
                                  'total_tracks': playlist['tracks']['total'],
                                  'id': playlist['id']})

        ret_artists = []
        artists = resp.json()["artists"]["items"]
        for artist in artists:
            ret_artists.append({'name': artist['name'],
                                'genres': '/'.join(artist['genres']),
                                'id': artist['id']})

        # TODO: Add search in episodes and shows

        if len(ret_tracks) + len(ret_albums) + \
                len(ret_playlists) + len(ret_artists) == 0:
            return None
        else:
            return {'tracks': ret_tracks,
                    'albums': ret_albums,
                    'playlists': ret_playlists,
                    'artists': ret_artists}

class RespotTrackHandler:

    CHUNK_SIZE = 50000
    RETRY_DOWNLOAD = 30

    def __init__(self, auth, format, antiban_wait_time, quality):
        """
        Initialises the track processor with necessary attributes.

        Args:
            format (str): The desired format for the converted audio.
            quality (str): The quality setting for the conversion (e.g., 'high', 'medium', 'low').
        """
        self.auth = auth
        self.format = format
        self.anti_ban_wait_time = antiban_wait_time
        self.quality = quality

    def download_audio(self, set_progress_bar, track_id, output_path, make_dirs):
        """Downloads raw song audio from Spotify"""
        # TODO: ADD disc_number IF > 1

        try:
            try:
                _track_id = TrackId.from_base62(track_id)
                stream = self.auth.session.content_feeder().load(
                    _track_id, VorbisOnlyAudioQuality(self.quality), False, None
                )
            except Exception as e:
                if isinstance(e, ApiClient.StatusCodeException):
                    _track_id = EpisodeId.from_base62(track_id)
                    stream = self.auth.session.content_feeder().load(
                        _track_id, VorbisOnlyAudioQuality(self.quality), False, None
                    )
                else:
                    raise e

            total_size = stream.input_stream.size
            chunk_size = self.CHUNK_SIZE
            downloaded = 0
            failCount = 0

            # Callback function
            set_progress_bar({"track_id": track_id,
                             "total": total_size,
                             "downloaded": downloaded})

            segments = []

            while downloaded <= total_size:
                data = stream.input_stream.stream().read(chunk_size)

                downloaded += len(data)
                segments.append(data)

                # TODO: Inefficient, refactor later
                set_progress_bar({"track_id": track_id,
                             "total": total_size,
                             "downloaded": downloaded})

                if (total_size - downloaded) < chunk_size:
                    chunk_size = total_size - downloaded
                if len(data) == 0:
                    failCount += 1
                if failCount > self.RETRY_DOWNLOAD:
                    break

            set_progress_bar(False)

            # Create output directories
            _dirs_path = output_path.parent
            if make_dirs:
                _dirs_path.mkdir(parents=True, exist_ok=True)
            elif not _dirs_path.exists():
                raise FileNotFoundError(
                    f"Directory {str(_dirs_path)} does not exist")

            # Save raw audio as BytesIO object and convert from there
            # TODO: Implement raw audio handler here
            self._convert_audio_format(BytesIO(b"".join(segments)), output_path)

            # Sleep to avoid ban
            time.sleep(self.anti_ban_wait_time)

            return True
        except Exception as e:
            print("###   download_track - FAILED TO DOWNLOAD   ###")
            print(e)
            print(track_id, output_path)
            return False
        
    def _convert_audio_format(self, audio_bytes: BytesIO, output_path):
        """Converts raw audio (ogg vorbis) to user specified format"""

        bitrate = "160k"
        if self.quality == AudioQuality.VERY_HIGH:
            bitrate = "320k"

        AudioSegment.from_file(audio_bytes).export(output_path, format=self.format, bitrate=bitrate)

class RespotUtils:
    def parse_url(search_input):
        pattern = r'intl-[^/]+/'
        search_input = re.sub(pattern, '', search_input)

        track_uri_search = re.search(
            r"^spotify:track:(?P<TrackID>[0-9a-zA-Z]{22})$", search_input
        )
        track_url_search = re.search(
            r"^(https?://)?open\.spotify\.com/track/(?P<TrackID>[0-9a-zA-Z]{22})(\?si=.+?)?$",
            search_input,
        )

        album_uri_search = re.search(
            r"^spotify:album:(?P<AlbumID>[0-9a-zA-Z]{22})$", search_input
        )
        album_url_search = re.search(
            r"^(https?://)?open\.spotify\.com/album/(?P<AlbumID>[0-9a-zA-Z]{22})(\?si=.+?)?$",
            search_input,
        )

        playlist_uri_search = re.search(
            r"^spotify:playlist:(?P<PlaylistID>[0-9a-zA-Z]{22})$", search_input
        )
        playlist_url_search = re.search(
            r"^(https?://)?open\.spotify\.com/playlist/(?P<PlaylistID>[0-9a-zA-Z]{22})(\?si=.+?)?$",
            search_input,
        )

        episode_uri_search = re.search(
            r"^spotify:episode:(?P<EpisodeID>[0-9a-zA-Z]{22})$", search_input
        )
        episode_url_search = re.search(
            r"^(https?://)?open\.spotify\.com/episode/(?P<EpisodeID>[0-9a-zA-Z]{22})(\?si=.+?)?$",
            search_input,
        )

        show_uri_search = re.search(
            r"^spotify:show:(?P<ShowID>[0-9a-zA-Z]{22})$", search_input
        )
        show_url_search = re.search(
            r"^(https?://)?open\.spotify\.com/show/(?P<ShowID>[0-9a-zA-Z]{22})(\?si=.+?)?$",
            search_input,
        )

        artist_uri_search = re.search(
            r"^spotify:artist:(?P<ArtistID>[0-9a-zA-Z]{22})$", search_input
        )
        artist_url_search = re.search(
            r"^(https?://)?open\.spotify\.com/artist/(?P<ArtistID>[0-9a-zA-Z]{22})(\?si=.+?)?$",
            search_input,
        )

        if track_uri_search is not None or track_url_search is not None:
            track_id_str = (
                track_uri_search if track_uri_search is not None else track_url_search
            ).group("TrackID")
        else:
            track_id_str = None

        if album_uri_search is not None or album_url_search is not None:
            album_id_str = (
                album_uri_search if album_uri_search is not None else album_url_search
            ).group("AlbumID")
        else:
            album_id_str = None

        if playlist_uri_search is not None or playlist_url_search is not None:
            playlist_id_str = (
                playlist_uri_search
                if playlist_uri_search is not None
                else playlist_url_search
            ).group("PlaylistID")
        else:
            playlist_id_str = None

        if episode_uri_search is not None or episode_url_search is not None:
            episode_id_str = (
                episode_uri_search if episode_uri_search is not None else episode_url_search
            ).group("EpisodeID")
        else:
            episode_id_str = None

        if show_uri_search is not None or show_url_search is not None:
            show_id_str = (
                show_uri_search if show_uri_search is not None else show_url_search
            ).group("ShowID")
        else:
            show_id_str = None

        if artist_uri_search is not None or artist_url_search is not None:
            artist_id_str = (
                artist_uri_search if artist_uri_search is not None else artist_url_search
            ).group("ArtistID")
        else:
            artist_id_str = None

        return {'track': track_id_str, 'album': album_id_str,
                'playlist': playlist_id_str, 'episode': episode_id_str,
                'show': show_id_str, 'artist': artist_id_str}

    def conv_artist_format(artists):
        """Returns converted artist format"""
        formatted = ""
        for artist in artists:
            formatted += artist + ", "
        return formatted[:-2]
    
    def sanitize_data(value: str) -> str:
        """Returns the string with problematic characters removed."""
        SANITIZE_CHARS = ["\\", "/", ":", "*", "?", "'", "<", ">", '"', "|"]

        for char in SANITIZE_CHARS:
            value = value.replace(char, "" if char != "|" else "-")
        return value