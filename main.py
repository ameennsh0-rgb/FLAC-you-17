import os
import re
import urllib.parse
from fastapi import FastAPI, HTTPException, Query
import httpx

app = FastAPI(title="Eclipse TorBox Bridge")

# Environment Variables (Configured on Render)
JACKETT_URL = os.getenv("JACKETT_URL", "http://localhost:9117")
JACKETT_API_KEY = os.getenv("JACKETT_API_KEY", "")
TORBOX_API_KEY = os.getenv("TORBOX_API_KEY", "")

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
# 2. Metadata Cleaning via MusicBrainz (Spelling Resolution)
# -------------------------------------------------------------------
async def correct_song_name(query: str):
    # Sanitize query for MusicBrainz lookup
    clean_query = re.sub(r'[^\w\s-]', ' ', query)
    url = "https://musicbrainz.org/ws/2/recording"
    headers = {"User-Agent": "EclipseAddonBridge/1.0.0 ( contact@yourdomain.com )"}
    params = {"query": clean_query, "fmt": "json", "limit": 3}
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(url, params=params, headers=headers)
            if res.status_code == 200:
                data = res.json()
                recordings = data.get("recordings", [])
                if recordings:
                    first = recordings[0]
                    title = first.get("title", query)
                    artist = first.get("artist-credit", [{}])[0].get("name", "")
                    album = first.get("releases", [{}])[0].get("title", "") if first.get("releases") else ""
                    return f"{artist} - {title}", title, artist, album
    except Exception:
        pass

    return query, query, "Unknown Artist", ""

# -------------------------------------------------------------------
# 3. Search Endpoint
# -------------------------------------------------------------------
@app.get("/search")
async def search(q: str = Query(...)):
    corrected_query, title, artist, album = await correct_song_name(q)
    
    # Replace colons or special chars in the generated track ID to avoid URL routing issues
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
# 4. Stream Endpoint (Sanitized Query Resolution)
# -------------------------------------------------------------------
@app.get("/stream/{track_id:path}")
async def get_stream(track_id: str):
    # Decode URL entities (e.g., %20 to space)
    decoded_id = urllib.parse.unquote(track_id)
    raw_query = decoded_id.replace("track_", "")
    
    # Strip special punctuation (like colons) that break Jackett/indexer queries
    jackett_query = re.sub(r'[:/\\?#]', ' ', raw_query)
    jackett_query = ' '.join(jackett_query.split())  # Normalize multiple spaces
    
    # A. Search Jackett API
    jackett_endpoint = f"{JACKETT_URL.rstrip('/')}/api/v2.0/indexers/all/results"
    params = {
        "apikey": JACKETT_API_KEY,
        "Query": f"{jackett_query} FLAC"
    }
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            res = await client.get(jackett_endpoint, params=params)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to connect to Jackett: {str(e)}")

        if res.status_code != 200:
            raise HTTPException(status_code=502, detail="Jackett lookup failed")
        
        results = res.json().get("Results", [])
        if not results:
            raise HTTPException(status_code=404, detail=f"No FLAC torrents found for query: {jackett_query}")
        
        # B. Prioritize 24-bit FLAC over 16-bit FLAC
        selected_magnet = None
        
        # Tier 1 Search: 24-bit / Hi-Res
        for item in results:
            title = item.get("Title", "").lower()
            if any(k in title for k in ["24bit", "24-bit", "24 bit", "hires", "hi-res"]):
                selected_magnet = item.get("MagnetUri") or item.get("Link")
                if selected_magnet:
                    break
        
        # Tier 2 Fallback: General FLAC (16-bit)
        if not selected_magnet:
            for item in results:
                if "flac" in item.get("Title", "").lower():
                    selected_magnet = item.get("MagnetUri") or item.get("Link")
                    if selected_magnet:
                        break

        if not selected_magnet:
            raise HTTPException(status_code=404, detail="Suitable FLAC releases not found")

        # C. Send Magnet Link to TorBox
        torbox_headers = {"Authorization": f"Bearer {TORBOX_API_KEY}"}
        add_torrent_res = await client.post(
            "https://api.torbox.app/v1/api/torrents/createtorrent",
            headers=torbox_headers,
            data={"magnet": selected_magnet, "seed": "1", "allow_zip": "false"}
        )
        
        torbox_data = add_torrent_res.json()
        if not torbox_data.get("success"):
            raise HTTPException(status_code=500, detail=f"TorBox error: {torbox_data.get('detail', 'Unknown error')}")
            
        torrent_id = torbox_data["data"]["torrent_id"]

        # D. Fetch Direct Download / Stream Link from TorBox
        info_res = await client.get(
            f"https://api.torbox.app/v1/api/torrents/mylist?id={torrent_id}",
            headers=torbox_headers
        )
        info_data = info_res.json().get("data", {})
        
        if isinstance(info_data, list) and len(info_data) > 0:
            info_data = info_data[0]
            
        files = info_data.get("files", [])
        
        # Find the primary FLAC audio file inside the torrent
        audio_file_id = None
        for f in files:
            if f.get("name", "").lower().endswith(".flac"):
                audio_file_id = f.get("id")
                break
                
        if audio_file_id is None and files:
            audio_file_id = files[0].get("id")

        # E. Generate streamable link
        link_res = await client.get(
            f"https://api.torbox.app/v1/api/torrents/requestdl?token={TORBOX_API_KEY}&torrent_id={torrent_id}&file_id={audio_file_id}&zip=false"
        )
        
        stream_url = link_res.json().get("data")
        if not stream_url:
            raise HTTPException(status_code=500, detail="Could not retrieve stream URL from TorBox")

        # F. Return playable HTTP URL to Eclipse
        return {
            "url": stream_url,
            "format": "flac",
            "container": "flac"
        }
