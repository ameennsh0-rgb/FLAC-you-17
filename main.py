import os
import re
import urllib.parse
import traceback
from fastapi import FastAPI, HTTPException, Query
import httpx

app = FastAPI(title="Eclipse TorBox Bridge")

# Environment Variables
raw_jackett_url = os.getenv("JACKETT_URL", "http://localhost:9117").strip()
if raw_jackett_url and not raw_jackett_url.startswith(("http://", "https://")):
    JACKETT_URL = f"https://{raw_jackett_url}"
else:
    JACKETT_URL = raw_jackett_url

JACKETT_API_KEY = os.getenv("JACKETT_API_KEY", "").strip()
TORBOX_API_KEY = os.getenv("TORBOX_API_KEY", "").strip()

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# -------------------------------------------------------------------
# 1. Root & Manifest Endpoints
# -------------------------------------------------------------------
@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Eclipse TorBox Bridge",
        "manifest": "/manifest.json"
    }

@app.get("/manifest.json")
async def manifest():
    return {
        "id": "com.user.torbox-debrid",
        "name": "TorBox FLAC Bridge",
        "version": "1.0.0",
        "description": "Searches Jackett, checks TorBox cache, and streams 24-bit/16-bit FLACs.",
        "resources": ["search", "stream"],
        "types": ["track", "album"]
    }

# -------------------------------------------------------------------
# 2. Metadata Cleaning via MusicBrainz
# -------------------------------------------------------------------
async def correct_song_name(query: str):
    artist_hint = ""
    title_hint = query
    if " - " in query:
        parts = query.split(" - ", 1)
        artist_hint = parts[0].strip()
        title_hint = parts[1].strip()

    clean_title = re.sub(r'[^\w\s]', '', title_hint)
    mb_query = f'recording:"{clean_title}"'
    if artist_hint:
        clean_artist = re.sub(r'[^\w\s]', '', artist_hint)
        mb_query += f' AND artist:"{clean_artist}"'

    url = "https://musicbrainz.org/ws/2/recording"
    mb_headers = {"User-Agent": "EclipseAddonBridge/1.0.0 (contact@yourdomain.com)"}
    params = {"query": mb_query, "fmt": "json", "limit": 5}
    
    try:
        async with httpx.AsyncClient(timeout=5.0, headers=mb_headers) as client:
            res = await client.get(url, params=params)
            if res.status_code == 200:
                recordings = res.json().get("recordings", [])
                if recordings:
                    best = recordings[0]
                    title = best.get("title", title_hint)
                    
                    artist_credits = best.get("artist-credit", [])
                    artist = artist_credits[0].get("name", "Various Artists") if artist_credits else "Various Artists"
                    
                    releases = best.get("releases", [])
                    album = releases[0].get("title", "") if releases else ""
                    
                    return f"{artist} - {title}", title, artist, album
    except Exception:
        pass

    if artist_hint:
        return f"{artist_hint} - {title_hint}", title_hint, artist_hint, ""
    return query, query.capitalize(), "Various Artists", ""

# -------------------------------------------------------------------
# 3. Search Endpoint
# -------------------------------------------------------------------
@app.get("/search")
async def search(q: str = Query(...)):
    corrected_query, title, artist, album = await correct_song_name(q)
    safe_id_string = re.sub(r'[:/?#\[\]@!$&\'()*+,;=]', '', corrected_query)
    
    return {
        "results": [
            {
                "id": f"track_{safe_id_string}",
                "title": title,
                "artist": artist,
                "album": album,
                "format": "flac"
            }
        ]
    }

# -------------------------------------------------------------------
# 4. Stream Endpoint
# -------------------------------------------------------------------
@app.get("/stream/{track_id:path}")
async def get_stream(track_id: str):
    try:
        decoded_id = urllib.parse.unquote(track_id)
        raw_query = decoded_id.replace("track_", "")
        
        jackett_query = re.sub(r'[:/\\?#]', ' ', raw_query)
        jackett_query = ' '.join(jackett_query.split())
        
        # A. Query Jackett API
        jackett_endpoint = f"{JACKETT_URL.rstrip('/')}/api/v2.0/indexers/all/results"
        params = {
            "apikey": JACKETT_API_KEY,
            "Query": f"{jackett_query} FLAC"
        }
        
        async with httpx.AsyncClient(timeout=25.0, headers=DEFAULT_HEADERS) as client:
            try:
                res = await client.get(jackett_endpoint, params=params)
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Failed to connect to Jackett at {JACKETT_URL}: {str(e)}")

            if res.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Jackett returned HTTP {res.status_code}. Check API key and indexers.")
            
            results = res.json().get("Results", [])
            if not results:
                raise HTTPException(status_code=404, detail=f"No FLAC torrents found for query: {jackett_query}")
            
            # B. Prioritize 24-bit/Hi-Res FLAC over 16-bit
            selected_link = None
            
            for item in results:
                title = item.get("Title", "").lower()
                if any(k in title for k in ["24bit", "24-bit", "24 bit", "hires", "hi-res"]):
                    selected_link = item.get("MagnetUri") or item.get("Link")
                    if selected_link:
                        break
            
            if not selected_link:
                for item in results:
                    if "flac" in item.get("Title", "").lower():
                        selected_link = item.get("MagnetUri") or item.get("Link")
                        if selected_link:
                            break

            if not selected_link:
                raise HTTPException(status_code=404, detail="No suitable FLAC releases found in search results.")

            # C. Send Link to TorBox
            torbox_headers = {
                "Authorization": f"Bearer {TORBOX_API_KEY}",
                "User-Agent": DEFAULT_HEADERS["User-Agent"]
            }
            
            if selected_link.startswith("magnet:?"):
                payload = {"magnet": selected_link, "seed": "1", "allow_zip": "false"}
                add_torrent_res = await client.post(
                    "https://api.torbox.app/v1/api/torrents/createtorrent",
                    headers=torbox_headers,
                    data=payload
                )
            else:
                torrent_file_res = await client.get(selected_link, follow_redirects=True)
                if torrent_file_res.status_code != 200:
                    raise HTTPException(status_code=502, detail="Failed to fetch .torrent file from indexer link.")
                
                files = {
                    "file": ("download.torrent", torrent_file_res.content, "application/x-bittorrent")
                }
                add_torrent_res = await client.post(
                    "https://api.torbox.app/v1/api/torrents/createtorrent",
                    headers=torbox_headers,
                    files=files,
                    data={"seed": "1", "allow_zip": "false"}
                )
            
            torbox_data = add_torrent_res.json()
            if not torbox_data.get("success"):
                err_msg = torbox_data.get("detail") or torbox_data.get("message") or "Unknown TorBox Error"
                raise HTTPException(status_code=500, detail=f"TorBox torrent creation failed: {err_msg}")
                
            torrent_id = torbox_data.get("data", {}).get("torrent_id")
            if not torrent_id:
                raise HTTPException(status_code=500, detail="TorBox API response missing torrent_id.")

            # D. Fetch File Info from TorBox
            info_res = await client.get(
                f"https://api.torbox.app/v1/api/torrents/mylist?id={torrent_id}",
                headers=torbox_headers
            )
            info_data = info_res.json().get("data", {})
            
            if isinstance(info_data, list) and len(info_data) > 0:
                info_data = info_data[0]
                
            files = info_data.get("files", [])
            if not files:
                raise HTTPException(status_code=500, detail="TorBox torrent file list is empty or processing.")
            
            # Select audio file
            audio_file_id = None
            for f in files:
                if f.get("name", "").lower().endswith(".flac"):
                    audio_file_id = f.get("id")
                    break
                    
            if audio_file_id is None:
                audio_file_id = files[0].get("id")

            # E. Request Direct Stream Link
            link_res = await client.get(
                "https://api.torbox.app/v1/api/torrents/requestdl",
                headers=torbox_headers,
                params={
                    "torrent_id": torrent_id,
                    "file_id": audio_file_id,
                    "zip": "false"
                }
            )
            
            stream_url = link_res.json().get("data")
            if not stream_url:
                raise HTTPException(status_code=500, detail="Could not retrieve stream URL from TorBox response.")

            return {
                "url": stream_url,
                "format": "flac",
                "container": "flac"
            }

    except HTTPException:
        raise
    except Exception as e:
        print("EXPLICIT ERROR TRACEBACK:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")
