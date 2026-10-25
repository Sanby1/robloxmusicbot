import discord
from discord.ext import commands
from flask import Flask, jsonify, request
import threading
import time
import asyncio
import aiohttp
import os
import json
import struct
import zlib
import re
import urllib.request
import urllib.parse

app = Flask(__name__)

DB_FILE = "verified_accounts.json"
HIGH_RES = 40  # 40x40 = 1,600 pixels (Optimal crisp HD resolution with zero Roblox lag!)
CENSOR_RES = 7 # 7x7 heavy mosaic blocks when NSFW / Explicit / Slang is detected!

SLANG_NSFW_REGEX = re.compile(
    r"\b(sex|sexy|porn|porno|nsfw|nude|nudes|naked|hentai|xxx|erotic|onlyfans|stripper|fuck|fucking|motherfucker|shit|bitch|dick|pussy|ass|asshole|slut|whore|cock|boobs|tits|cum|bdsm|fetish|amk|aq|sik|sikiş|sikis|sikerim|yarrak|yarak|orospu|piç|pic|göt|got|meme|çıplak|ciplak|kahpe|pezevenk|amcık|amcik|sakso)\b",
    re.IGNORECASE
)

def load_accounts():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {k: int(v) for k, v in data.items()}
        except Exception:
            pass
    return {}

def save_accounts():
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(kayitli_hesaplar, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

veri_tabani = {}
kayitli_hesaplar = load_accounts()
roblox_onay_kodlari = {}
active_tickets = {}
cover_cache = {}
lyrics_cache = {}
ticket_cooldowns = {}

SIGN_KANAL_ID = 123456789012345678 # Put your Discord channel ID here
VERIFIED_ROLE_ID = 1540891610739507331 # Put your Verified role ID here

# =========================================================================
# STRICT NSFW SKIN-CLUSTER DETECTOR (AVOIDS FALSE POSITIVES ON NORMAL COVERS)
# =========================================================================
def is_nsfw_skin_heavy(hex_pixels, grid_size=HIGH_RES, is_itunes_explicit=False):
    if not hex_pixels or len(hex_pixels) != grid_size * grid_size:
        return False

    skin_total = 0
    center_skin = 0
    center_count = 0
    c_min = int(grid_size * 0.22)
    c_max = int(grid_size * 0.78)

    for idx, hx in enumerate(hex_pixels):
        r = int(hx[0:2], 16)
        g = int(hx[2:4], 16)
        b = int(hx[4:6], 16)

        if (r > 105 and g > 50 and b > 35 and
            r > g and g > b and
            (r - g) >= 15 and (r - g) <= 85 and
            (g - b) <= 55):
            cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
            cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
            if 85 <= cb <= 122 and 136 <= cr <= 168:
                skin_total += 1
                y = idx // grid_size
                x = idx % grid_size
                if c_min <= x < c_max and c_min <= y < c_max:
                    center_skin += 1

        y = idx // grid_size
        x = idx % grid_size
        if c_min <= x < c_max and c_min <= y < c_max:
            center_count += 1

    total_ratio = skin_total / len(hex_pixels)
    center_ratio = center_skin / max(center_count, 1)

    if is_itunes_explicit and center_ratio >= 0.60:
        return True
    return center_ratio >= 0.74 and total_ratio >= 0.64

def pixelate_hex_grid(hex_pixels, grid_size=HIGH_RES, coarse_size=CENSOR_RES):
    if len(hex_pixels) != grid_size * grid_size:
        return hex_pixels
    out = list(hex_pixels)
    block = max(1, grid_size // coarse_size)
    for by in range(0, grid_size, block):
        for bx in range(0, grid_size, block):
            r_sum = g_sum = b_sum = cnt = 0
            for y in range(by, min(by + block, grid_size)):
                for x in range(bx, min(bx + block, grid_size)):
                    hx = hex_pixels[y * grid_size + x]
                    r_sum += int(hx[0:2], 16)
                    g_sum += int(hx[2:4], 16)
                    b_sum += int(hx[4:6], 16)
                    cnt += 1
            if cnt > 0:
                avg_hex = f"{int(r_sum/cnt):02x}{int(g_sum/cnt):02x}{int(b_sum/cnt):02x}"
                for y in range(by, min(by + block, grid_size)):
                    for x in range(bx, min(bx + block, grid_size)):
                        out[y * grid_size + x] = avg_hex
    return out

# =========================================================================
# ZERO-DEPENDENCY PURE PYTHON PNG DECODER & RESIZER (40x40 = 1,600 PIXELS)
# =========================================================================
def parse_png_to_grid(png_bytes, grid_size=HIGH_RES):
    if len(png_bytes) < 33 or png_bytes[:8] != b'\x89PNG\r\n\x1a\n':
        return [], [24, 24, 24]
    pos = 8
    width = height = bit_depth = color_type = 0
    idat_chunks = []
    plte = []

    while pos + 8 <= len(png_bytes):
        length = struct.unpack(">I", png_bytes[pos:pos+4])[0]
        chunk_type = png_bytes[pos+4:pos+8]
        chunk_data = png_bytes[pos+8:pos+8+length]
        pos += 12 + length

        if chunk_type == b'IHDR':
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk_data[:10])
        elif chunk_type == b'PLTE':
            for i in range(0, len(chunk_data), 3):
                if i + 2 < len(chunk_data):
                    plte.append((chunk_data[i], chunk_data[i+1], chunk_data[i+2]))
        elif chunk_type == b'IDAT':
            idat_chunks.append(chunk_data)
        elif chunk_type == b'IEND':
            break

    if width == 0 or height == 0 or bit_depth != 8 or not idat_chunks:
        return [], [24, 24, 24]

    bpp_map = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if color_type not in bpp_map:
        return [], [24, 24, 24]
    bpp = bpp_map[color_type]

    raw = zlib.decompress(b"".join(idat_chunks))
    stride = width * bpp
    recon = bytearray(height * stride)

    idx = 0
    for y in range(height):
        f_type = raw[idx]
        idx += 1
        row_start = y * stride
        prev_start = (y - 1) * stride
        for x in range(stride):
            val = raw[idx]
            idx += 1
            a = recon[row_start + x - bpp] if x >= bpp else 0
            b = recon[prev_start + x] if y > 0 else 0
            c = recon[prev_start + x - bpp] if (y > 0 and x >= bpp) else 0
            if f_type == 1:
                val = (val + a) & 0xFF
            elif f_type == 2:
                val = (val + b) & 0xFF
            elif f_type == 3:
                val = (val + ((a + b) >> 1)) & 0xFF
            elif f_type == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                val = (val + pr) & 0xFF
            recon[row_start + x] = val

    pixels = []
    r_sum = g_sum = b_sum = 0
    for gy in range(grid_size):
        sy = min(int(gy * height / grid_size), height - 1)
        for gx in range(grid_size):
            sx = min(int(gx * width / grid_size), width - 1)
            p_idx = (sy * width + sx) * bpp
            if color_type == 2 or color_type == 6:
                r, g, b = recon[p_idx], recon[p_idx+1], recon[p_idx+2]
            elif color_type == 0 or color_type == 4:
                r = g = b = recon[p_idx]
            elif color_type == 3:
                pal_i = recon[p_idx]
                r, g, b = plte[pal_i] if pal_i < len(plte) else (24, 24, 24)
            else:
                r = g = b = 24
            r_sum += r
            g_sum += g
            b_sum += b
            pixels.append(f"{r:02x}{g:02x}{b:02x}")

    total = max(len(pixels), 1)
    dom_color = [int(r_sum / total), int(g_sum / total), int(b_sum / total)]
    return pixels, dom_color

async def fetch_album_cover_pixels(url, artist="", title=""):
    target_url = url
    has_slang_or_nsfw = False
    is_itunes_explicit = False

    if SLANG_NSFW_REGEX.search(f"{artist} {title}"):
        has_slang_or_nsfw = True

    if artist and title:
        try:
            search_q = f"{artist} {title}"
            async with aiohttp.ClientSession() as session:
                async with session.get("https://itunes.apple.com/search", params={"term": search_q, "entity": "song", "limit": 1}, timeout=4) as r:
                    if r.status == 200:
                        js = await r.json(content_type=None)
                        results = js.get("results", [])
                        if results:
                            item = results[0]
                            if not target_url:
                                target_url = item.get("artworkUrl100", "")
                            if item.get("collectionExplicitness") == "explicit" or item.get("trackExplicitness") == "explicit":
                                is_itunes_explicit = True
                            col_name = item.get("collectionName", "")
                            if SLANG_NSFW_REGEX.search(col_name):
                                has_slang_or_nsfw = True
        except Exception:
            pass

    if not target_url:
        return [], [24, 24, 24], False

    cache_key = f"{target_url}_{has_slang_or_nsfw}_{is_itunes_explicit}_{HIGH_RES}"
    if cache_key in cover_cache:
        c = cover_cache[cache_key]
        return c["pixels"], c["color"], c["censored"]

    pixels = []
    dom_color = [24, 24, 24]

    try:
        from PIL import Image, ImageEnhance
        from io import BytesIO
        async with aiohttp.ClientSession() as session:
            async with session.get(target_url, timeout=4) as resp:
                if resp.status == 200:
                    img_bytes = await resp.read()
                    img = Image.open(BytesIO(img_bytes)).convert("RGB")
                    resample_filter = getattr(Image, "Resampling", Image).LANCZOS
                    dom = img.resize((1, 1), resample_filter).getpixel((0, 0))
                    dom_color = [int(dom[0]), int(dom[1]), int(dom[2])]
                    small = img.resize((HIGH_RES, HIGH_RES), resample_filter)
                    small = ImageEnhance.Contrast(small).enhance(1.06)
                    for y in range(HIGH_RES):
                        for x in range(HIGH_RES):
                            r, g, b = small.getpixel((x, y))
                            pixels.append(f"{r:02x}{g:02x}{b:02x}")
    except Exception:
        pass

    if not pixels:
        try:
            proxy_url = f"https://wsrv.nl/?url={target_url}&w={HIGH_RES}&h={HIGH_RES}&fit=cover&output=png"
            async with aiohttp.ClientSession() as session:
                async with session.get(proxy_url, timeout=5) as resp:
                    if resp.status == 200:
                        png_bytes = await resp.read()
                        pixels, dom_color = parse_png_to_grid(png_bytes, HIGH_RES)
        except Exception as e:
            print("Fallback cover error:", e)

    if pixels:
        should_censor = has_slang_or_nsfw or is_nsfw_skin_heavy(pixels, HIGH_RES, is_itunes_explicit)
        if should_censor:
            pixels = pixelate_hex_grid(pixels, HIGH_RES, CENSOR_RES)

        if len(cover_cache) > 80:
            cover_cache.clear()
        cover_cache[cache_key] = {"pixels": pixels, "color": dom_color, "censored": should_censor}
        return pixels, dom_color, should_censor

    return [], [24, 24, 24], False

def parse_synced_lyrics_to_list(synced_str):
    if not synced_str:
        return []
    out = []
    for line in synced_str.split("\n"):
        line = line.strip()
        if line.startswith("[") and "]" in line:
            try:
                time_part = line[1:line.find("]")]
                m, s = time_part.split(":")
                line_sec = round(int(m) * 60 + float(s), 2)
                txt = line[line.find("]") + 1:].strip()
                if txt:
                    out.append({"t": line_sec, "text": txt})
            except Exception:
                pass
    return out

def build_duration_synced_list_from_plain(plain_str, duration):
    if not plain_str:
        return []
    raw_lines = [ln.strip() for ln in plain_str.split("\n") if ln.strip() and not ln.strip().startswith("[")]
    if not raw_lines:
        return []
    usable_dur = max(15.0, float(duration or 180) - 4.0)
    step = usable_dur / max(len(raw_lines), 1)
    out = []
    for i, ln in enumerate(raw_lines):
        out.append({"t": round(i * step, 2), "text": ln[:80]})
    return out

@app.route('/api/set_code', methods=['GET'])
def set_code():
    user = request.args.get('user')
    code = request.args.get('code')
    discord_id_str = request.args.get('discord_id')
    if user and code:
        u_low = user.strip().lower()
        roblox_onay_kodlari[u_low] = code.strip()
        if discord_id_str and discord_id_str.strip().isdigit() and u_low not in kayitli_hesaplar:
            d_id = int(discord_id_str.strip())
            kayitli_hesaplar[u_low] = d_id
            save_accounts()
            try:
                for g in bot.guilds:
                    member = g.get_member(d_id)
                    if member:
                        asyncio.run_coroutine_threadsafe(
                            _refresh_restored_member(member),
                            bot.loop
                        )
                        break
            except Exception:
                pass
        return "OK", 200
    return "Error", 400

async def _refresh_restored_member(member):
    try:
        veri_tabani[member.id] = await extract_member_presence(member)
    except Exception:
        pass

# =========================================================================
# PLAYLIST / ALBUM LOOKUP ENDPOINT FOR "liste" BOARD IN WORKSPACE
# Supports Spotify Playlist/Album Links OR Album/Artist Search Queries!
# =========================================================================
@app.route('/api/lookup_list', methods=['GET'])
def lookup_list():
    query = (request.args.get('q') or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "Empty query"}), 400

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # 1. Check if the user pasted a Spotify Playlist, Album, or Track URL
    sp_match = re.search(r"open\.spotify\.com/(?:intl-[a-zA-Z-]+/)?(playlist|album|track)/([a-zA-Z0-9]+)", query)
    if sp_match:
        kind, sp_id = sp_match.group(1), sp_match.group(2)
        embed_url = f"https://open.spotify.com/embed/{kind}/{sp_id}"
        try:
            req = urllib.request.Request(embed_url, headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
                nd_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
                if nd_match:
                    nd = json.loads(nd_match.group(1))
                    entity = nd.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                    title = entity.get("name") or entity.get("title") or f"Spotify {kind.title()}"
                    subtitle = entity.get("subtitle") or entity.get("description") or "Spotify"
                    if not subtitle or subtitle == "Spotify":
                        authors = entity.get("authors") or entity.get("artists") or []
                        if isinstance(authors, list) and len(authors) > 0 and isinstance(authors[0], dict):
                            subtitle = authors[0].get("name") or "Spotify"
                    track_list = entity.get("trackList", [])
                    if not track_list and kind == "track":
                        dur_ms = entity.get("duration") or 180000
                        track_list = [{"title": title, "subtitle": subtitle, "duration": dur_ms}]
                    tracks = []
                    total_sec = 0
                    for idx, t in enumerate(track_list[:50], 1):
                        dur_ms = t.get("duration") or 0
                        dur_s = int(dur_ms / 1000)
                        total_sec += dur_s
                        tracks.append({
                            "num": idx,
                            "title": t.get("title") or "Track",
                            "artist": t.get("subtitle") or subtitle,
                            "duration": f"{dur_s // 60}:{dur_s % 60:02d}"
                        })
                    if tracks:
                        return jsonify({"ok": True, "title": title, "subtitle": subtitle, "totalSec": total_sec, "tracks": tracks})
        except Exception as e:
            print("Spotify embed parse fallback:", e)
        return jsonify({"ok": False, "error": "Could not load Spotify link"}), 404

    # 2. Search iTunes Album / Songs API for Album or Artist name
    try:
        enc_q = urllib.parse.quote(query)
        alb_url = f"https://itunes.apple.com/search?term={enc_q}&entity=album&limit=1"
        req = urllib.request.Request(alb_url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            alb_data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            results = alb_data.get("results", [])
            if results:
                col_id = results[0].get("collectionId")
                col_name = results[0].get("collectionName") or query
                art_name = results[0].get("artistName") or ""
                if col_id:
                    lookup_url = f"https://itunes.apple.com/lookup?id={col_id}&entity=song"
                    with urllib.request.urlopen(urllib.request.Request(lookup_url, headers=headers), timeout=5) as l_resp:
                        l_data = json.loads(l_resp.read().decode("utf-8", errors="ignore"))
                        tracks = []
                        for item in l_data.get("results", []):
                            if item.get("wrapperType") == "track":
                                dur_s = int((item.get("trackTimeMillis") or 0) / 1000)
                                tracks.append({
                                    "num": len(tracks) + 1,
                                    "title": item.get("trackName") or "Track",
                                    "artist": item.get("artistName") or art_name,
                                    "duration": f"{dur_s // 60}:{dur_s % 60:02d}"
                                })
                        if tracks:
                            return jsonify({"ok": True, "title": col_name, "subtitle": art_name, "tracks": tracks[:50]})
    except Exception as e:
        print("Album lookup error:", e)

    return jsonify({"ok": False, "error": "Not found"}), 404

async def get_lyrics(artist, title, duration):
    clean_artist = re.split(r"[;,/]", str(artist or ""))[0].strip()
    clean_title = re.sub(r"\s*[\(\[].*?[\)\]]", "", str(title or "")).strip()
    cache_k = f"{clean_artist.lower()}_{clean_title.lower()}_{int(duration or 0)}"
    if cache_k in lyrics_cache:
        return lyrics_cache[cache_k]
    headers = {"User-Agent": "RobloxMusicStatusBot/2.0"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            for a_try, t_try in [(artist, title), (clean_artist, clean_title)]:
                if not a_try or not t_try:
                    continue
                try:
                    async with session.get("https://lrclib.net/api/get", params={"artist_name": a_try, "track_name": t_try, "duration": duration}, timeout=4) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            if data and (data.get("syncedLyrics") or data.get("plainLyrics")):
                                res_lyr = data.get("syncedLyrics") or data.get("plainLyrics")
                                if len(lyrics_cache) > 150:
                                    lyrics_cache.clear()
                                lyrics_cache[cache_k] = res_lyr
                                return res_lyr
                except Exception:
                    pass

            for q_str in [f"{clean_artist} {clean_title}", f"{artist} {title}", clean_title]:
                if not q_str.strip():
                    continue
                try:
                    async with session.get("https://lrclib.net/api/search", params={"q": q_str.strip()}, timeout=4) as s_resp:
                        if s_resp.status == 200:
                            items = await s_resp.json(content_type=None)
                            if isinstance(items, list) and len(items) > 0:
                                for it in items:
                                    if it.get("syncedLyrics"):
                                        if len(lyrics_cache) > 150:
                                            lyrics_cache.clear()
                                        lyrics_cache[cache_k] = it.get("syncedLyrics")
                                        return it.get("syncedLyrics")
                                for it in items:
                                    if it.get("plainLyrics"):
                                        if len(lyrics_cache) > 150:
                                            lyrics_cache.clear()
                                        lyrics_cache[cache_k] = it.get("plainLyrics")
                                        return it.get("plainLyrics")
                except Exception:
                    pass
    except Exception:
        pass
    return "No lyrics available" 

@app.route('/api/all_users')
def get_all_users():
    sonuc = {}
    now = time.time()
    for roblox_ismi, discord_id in kayitli_hesaplar.items():
        veri = veri_tabani.get(discord_id)
        if not veri:
            sonuc[roblox_ismi] = {
                "discord_id": str(discord_id),
                "sarki": "None",
                "sarki_sure": 0,
                "sarki_toplam": 210,
                "sarki_sozu": "♪ No lyrics",
                "synced_list": [],
                "album_kapak": "",
                "kapak_pikseller": [],
                "kapak_sansurlu": False,
                "bant_renk": [24, 24, 24]
            }
            continue

        s_bas = veri.get("sarki_baslangic", 0)
        sarki_sure_float = round(now - s_bas, 2) if s_bas > 0 else 0
        sarki_sure = int(sarki_sure_float)

        aktif_soz = "♪ No lyrics"
        synced_list = veri.get("synced_list") or []
        if synced_list:
            current_line = ""
            # Exact real-time match (with +0.35s lead to compensate for network/UI tween)
            for item in synced_list:
                if (sarki_sure_float + 0.35) >= item["t"]:
                    current_line = item["text"]
                else:
                    break
            if current_line:
                aktif_soz = "♪ " + current_line
        else:
            aktif_soz = veri.get("plain_lyrics", "♪ No lyrics")

        sonuc[roblox_ismi] = {
            "discord_id": str(discord_id),
            "sarki": veri.get("sarki", "None"),
            "sarki_sure": sarki_sure,
            "sarki_sure_exact": sarki_sure_float,
            "sarki_toplam": veri.get("sarki_toplam", 210),
            "sarki_sozu": aktif_soz[:80],
            "synced_list": synced_list,
            "album_kapak": veri.get("album_kapak", ""),
            "kapak_pikseller": veri.get("kapak_pikseller", []),
            "kapak_sansurlu": veri.get("kapak_sansurlu", False),
            "bant_renk": veri.get("bant_renk", [24, 24, 24])
        }
    return jsonify(sonuc)

async def auto_close_ticket(channel, user_id):
    await asyncio.sleep(86400)
    try:
        if user_id in active_tickets and active_tickets[user_id] == channel.id:
            del active_tickets[user_id]
        await channel.delete(reason="Auto-closed after 24 hours")
    except Exception:
        pass

def build_spotify_fix_embed():
    embed = discord.Embed(
        title="Why Is My Spotify Song Not Showing in Game?",
        description="If you linked your account but your song does not appear in Roblox, check these **3 quick settings** in Discord:",
        color=discord.Color.from_rgb(30, 215, 96)
    )
    embed.add_field(
        name="1. Enable Spotify Status in Connections",
        value="Go to **User Settings > Connections > Spotify** and turn ON **Display Spotify as your status**.",
        inline=False
    )
    embed.add_field(
        name="2. Enable Activity Privacy",
        value="Go to **User Settings > Activity Privacy** and turn ON **Share your detected activities with others** (and enable it for this server).",
        inline=False
    )
    embed.add_field(
        name="3. Do Not Use Invisible Mode",
        value="Discord hides all Spotify activity when your status is **Invisible / Offline**. Set your status to **Online**, **Idle**, or **Do Not Disturb**.",
        inline=False
    )
    return embed

async def run_spotify_status_test(interaction: discord.Interaction):
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not member:
        await interaction.response.send_message("Could not read your member profile right now.", ephemeral=True)
        return

    linked_roblox = None
    for r_name, d_id in kayitli_hesaplar.items():
        if d_id == member.id:
            linked_roblox = r_name
            break

    spotify_act = None
    for act in member.activities:
        if isinstance(act, discord.Spotify):
            spotify_act = act
            break

    status_str = str(member.status)
    if status_str == "offline" or status_str == "invisible":
        embed = discord.Embed(
            title="Spotify Test Result: You Are Invisible / Offline!",
            description="Your Discord status appears **Offline / Invisible**.\nDiscord does **not** broadcast Spotify activity while Invisible. Switch your Discord status to **Online**, **Idle**, or **Do Not Disturb** and test again!",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if spotify_act:
        veri_tabani[member.id] = await extract_member_presence(member)
        link_note = f"Linked Roblox Account: **{linked_roblox}**" if linked_roblox else "*(You have not linked a Roblox account yet — click Open Verify Ticket to link!)*"
        embed = discord.Embed(
            title="Spotify Test Result: Connected & Working!",
            description=f"We detected your live Spotify stream:\n**{spotify_act.artist} - {spotify_act.title}**\n\n{link_note}",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        embed = build_spotify_fix_embed()
        embed.title = "Spotify Test Result: No Active Song Detected"
        embed.color = discord.Color.orange()
        await interaction.response.send_message(
            content="We could not detect Spotify on your Discord profile right now. Make sure a song is playing and check these 3 steps:",
            embed=embed,
            ephemeral=True
        )

active_fix_rooms = {}

async def open_private_spotify_fix_room(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message(embed=build_spotify_fix_embed(), ephemeral=True)
        return

    user_id = interaction.user.id
    if user_id in active_fix_rooms:
        old_ch = guild.get_channel(active_fix_rooms[user_id])
        if old_ch:
            await interaction.response.send_message(
                f"You already have an active troubleshooting room open here: {old_ch.mention}",
                ephemeral=True
            )
            return

    now_ts = time.time()
    if now_ts - ticket_cooldowns.get(f"fix_{user_id}", 0) < 20:
        await interaction.response.send_message(
            "Please wait a few seconds before opening another room.",
            ephemeral=True
        )
        return
    ticket_cooldowns[f"fix_{user_id}"] = now_ts

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
    }

    fix_channel = await guild.create_text_channel(f"spotify-help-{interaction.user.name}", overwrites=overwrites)
    active_fix_rooms[user_id] = fix_channel.id

    await interaction.response.send_message(
        f"Your private Spotify troubleshooting room has been created: {fix_channel.mention}",
        ephemeral=True
    )

    member = guild.get_member(user_id)
    linked_roblox = None
    for r_name, d_id in kayitli_hesaplar.items():
        if d_id == user_id:
            linked_roblox = r_name
            break

    spotify_act = None
    if member:
        for act in member.activities:
            if isinstance(act, discord.Spotify):
                spotify_act = act
                break

    status_str = str(member.status) if member else "unknown"
    diag_lines = []
    if linked_roblox:
        diag_lines.append(f"• **Linked Roblox Account:** `{linked_roblox}` (Verified)")
    else:
        diag_lines.append("• **Linked Roblox Account:** `Not Linked Yet!` (Type `!verify YourRobloxName 123456` right here in this room to link!)")

    if status_str in ("offline", "invisible"):
        diag_lines.append("• **Discord Status:** `Offline / Invisible` — **PROBLEM FOUND:** Discord blocks Spotify activity while you are Invisible! Change your Discord status to **Online**, **Idle**, or **Do Not Disturb**.")
    else:
        diag_lines.append(f"• **Discord Status:** `{status_str.title()}` (Good)")

    if spotify_act:
        diag_lines.append(f"• **Live Spotify Stream:** `{spotify_act.artist} - {spotify_act.title}` (Detected & Working!)")
    else:
        diag_lines.append("• **Live Spotify Stream:** `Not Detected` — Follow the **3 Steps** below so Discord shares your song!")

    diag_embed = discord.Embed(
        title="Personal Spotify & Account Troubleshooter",
        description=(
            f"Hello {interaction.user.mention}! This private room was created just for you to solve any connection or Spotify display issues.\n\n"
            "**Live Account Diagnosis:**\n" + "\n".join(diag_lines)
        ),
        color=discord.Color.from_rgb(30, 215, 96)
    )
    diag_embed.add_field(
        name="Step 1 — Enable Spotify Status on Discord",
        value="Open **Discord User Settings > Connections > Spotify** and turn ON **Display Spotify as your status**.",
        inline=False
    )
    diag_embed.add_field(
        name="Step 2 — Enable Activity Privacy",
        value="Open **Discord User Settings > Activity Privacy** and turn ON **Share your detected activities with others** (and make sure Activity Status is enabled for this server).",
        inline=False
    )
    diag_embed.add_field(
        name="Step 3 — Stay Online & Refresh Song",
        value="Make sure your Discord status is **Online / Idle / DND** (never Invisible), then pause and play your song on Spotify and click **Test My Spotify Status** below!",
        inline=False
    )
    await fix_channel.send(content=interaction.user.mention, embed=diag_embed, view=TicketControlView())
    asyncio.create_task(auto_close_ticket(fix_channel, user_id))

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Test My Spotify Status", style=discord.ButtonStyle.success, custom_id="test_spotify_ticket_btn")
    async def test_spotify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_spotify_status_test(interaction)

    @discord.ui.button(label="Spotify Fix Checklist", style=discord.ButtonStyle.secondary, custom_id="fix_guide_ticket_btn")
    async def fix_guide(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_spotify_fix_embed(), ephemeral=True)

    @discord.ui.button(label="Close Room", style=discord.ButtonStyle.danger, custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Closing room in 3 seconds...", ephemeral=False)
        await asyncio.sleep(3)
        try:
            for uid, cid in list(active_tickets.items()):
                if cid == interaction.channel.id:
                    del active_tickets[uid]
            for uid, cid in list(active_fix_rooms.items()):
                if cid == interaction.channel.id:
                    del active_fix_rooms[uid]
            await interaction.channel.delete(reason=f"Closed by {interaction.user}")
        except Exception:
            pass

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Verify Ticket", style=discord.ButtonStyle.blurple, custom_id="kayit_ticket_btn")
    async def ticket_ac(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user_id = interaction.user.id

        if user_id in active_tickets:
            old_channel_id = active_tickets[user_id]
            old_channel = guild.get_channel(old_channel_id)
            if old_channel:
                await interaction.response.send_message(
                    f"You already have an open verification room here: {old_channel.mention}",
                    ephemeral=True
                )
                return

        now_ts = time.time()
        if now_ts - ticket_cooldowns.get(f"ver_{user_id}", 0) < 20:
            await interaction.response.send_message(
                "Please wait a few seconds before opening another ticket.",
                ephemeral=True
            )
            return
        ticket_cooldowns[f"ver_{user_id}"] = now_ts

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        ticket_channel = await guild.create_text_channel(f"verify-{interaction.user.name}", overwrites=overwrites)
        active_tickets[user_id] = ticket_channel.id

        await interaction.response.send_message(f"Your verification room has been opened: {ticket_channel.mention}", ephemeral=True)

        embed = discord.Embed(
            title="Roblox & Discord Account Verification",
            description=(
                f"Welcome {interaction.user.mention}!\n\n"
                "To link your Roblox account, look at the **6-digit code** on your Overhead UI (or onboarding card) in the game and type:\n"
                "```!verify YourRobloxName 123456```\n"
                "*(If your Spotify song is not showing in-game after linking, click **Test My Spotify Status** below!)*"
            ),
            color=discord.Color.blurple()
        )
        await ticket_channel.send(content=interaction.user.mention, embed=embed, view=TicketControlView())
        asyncio.create_task(auto_close_ticket(ticket_channel, user_id))

    @discord.ui.button(label="Spotify Not Showing? (Open Help Room)", style=discord.ButtonStyle.success, custom_id="main_fix_guide_btn")
    async def main_fix_guide(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_private_spotify_fix_room(interaction)

    @discord.ui.button(label="Quick Spotify Status Test", style=discord.ButtonStyle.secondary, custom_id="main_test_spotify_btn")
    async def main_test_spotify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_spotify_status_test(interaction)

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.presences = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(TicketView())
        self.add_view(TicketControlView())

    async def on_ready(self):
        print(f"Logged in as {self.user}!")
        kanal = self.get_channel(SIGN_KANAL_ID)
        if kanal:
            mesaj_var = False
            async for msg in kanal.history(limit=10):
                if msg.author == self.user:
                    mesaj_var = True
                    break
            if not mesaj_var:
                embed = discord.Embed(
                    title="Link Your Roblox & Discord Accounts",
                    description="Click the button below to open a verification ticket and display your live Spotify status in Roblox!",
                    color=discord.Color.green()
                )
                await kanal.send(embed=embed, view=TicketView())

        for g in self.guilds:
            for r_name, d_id in list(kayitli_hesaplar.items()):
                member = g.get_member(d_id)
                if member:
                    try:
                        veri_tabani[d_id] = await extract_member_presence(member)
                    except Exception:
                        pass

bot = MyBot()

async def extract_member_presence(member: discord.Member):
    sarki = "None"
    sarki_baslangic, sarki_toplam = 0, 210
    synced_list = []
    plain = "♪ No lyrics"
    album_kapak = ""
    kapak_pikseller = []
    kapak_sansurlu = False
    bant_renk = [24, 24, 24]

    for act in member.activities:
        if isinstance(act, discord.Spotify):
            sarki = f"{act.artist} - {act.title}"
            if hasattr(act, 'start') and act.start:
                sarki_baslangic = act.start.timestamp()
            if hasattr(act, 'duration') and act.duration:
                sarki_toplam = int(act.duration.total_seconds())
            if hasattr(act, 'album_cover_url') and act.album_cover_url:
                album_kapak = str(act.album_cover_url)

            kapak_pikseller, bant_renk, kapak_sansurlu = await fetch_album_cover_pixels(album_kapak, act.artist, act.title)

            lyrics_text = await get_lyrics(act.artist, act.title, sarki_toplam)
            if lyrics_text != "No lyrics available":
                if "\n[" in lyrics_text or lyrics_text.startswith("["):
                    synced_list = parse_synced_lyrics_to_list(lyrics_text)
                if not synced_list:
                    synced_list = build_duration_synced_list_from_plain(lyrics_text, sarki_toplam)
                plain = "♪ " + lyrics_text.split("\n")[0][:65]

    return {
        "sarki": sarki,
        "sarki_baslangic": sarki_baslangic,
        "sarki_toplam": sarki_toplam,
        "synced_list": synced_list,
        "plain_lyrics": plain,
        "album_kapak": album_kapak,
        "kapak_pikseller": kapak_pikseller,
        "kapak_sansurlu": kapak_sansurlu,
        "bant_renk": bant_renk
    }

@bot.command()
async def setup_ticket(ctx):
    if ctx.author.guild_permissions.administrator:
        embed = discord.Embed(
            title="Link Your Roblox & Discord Accounts",
            description="Click the button below to open a verification ticket and display your live Spotify status in Roblox!",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed, view=TicketView())

@bot.command()
async def verify(ctx, roblox_ismi: str = None, kod: str = None):
    if not roblox_ismi or not kod:
        await ctx.send("Missing information! Usage example: `!verify YourRobloxName 123456`")
        return

    r_isim_lower = roblox_ismi.strip().lower()
    girdi_kod = kod.strip()

    if r_isim_lower in roblox_onay_kodlari and roblox_onay_kodlari[r_isim_lower] == girdi_kod:
        kayitli_hesaplar[r_isim_lower] = ctx.author.id
        save_accounts()

        try:
            await ctx.author.edit(nick=roblox_ismi)
        except Exception:
            pass

        role = ctx.guild.get_role(VERIFIED_ROLE_ID)
        if role:
            try:
                await ctx.author.add_roles(role)
            except Exception:
                pass

        pres = await extract_member_presence(ctx.author)
        veri_tabani[ctx.author.id] = pres
        has_song = (pres.get("sarki") and pres.get("sarki") != "None")
        sp_status_line = (
            f"\n**Live Spotify Detected:** `{pres.get('sarki')}`"
            if has_song else
            "\n*(Tip: No Spotify song detected right now. Make sure **Display Spotify as your status** is ON in Discord Connections and you are not Invisible!)*"
        )

        embed = discord.Embed(
            title="Verification Complete!",
            description=(
                f"**{roblox_ismi}** has been linked to {ctx.author.mention}.{sp_status_line}\n\n"
                "Your in-game panel will update in a few seconds. This ticket will automatically close in **5 seconds**."
            ),
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)
        del roblox_onay_kodlari[r_isim_lower]

        await asyncio.sleep(4)
        try:
            if ctx.author.id in active_tickets and active_tickets[ctx.author.id] == ctx.channel.id:
                del active_tickets[ctx.author.id]
            if ctx.author.id in active_fix_rooms and active_fix_rooms[ctx.author.id] == ctx.channel.id:
                del active_fix_rooms[ctx.author.id]
            if ctx.channel.name.startswith("verify-") or ctx.channel.name.startswith("spotify-help-"):
                await ctx.channel.delete(reason="Verification Complete")
        except Exception:
            pass
    else:
        await ctx.send("**Error:** The code you entered is wrong or you are not in the game right now!")

@bot.event
async def on_presence_update(before, after):
    if after.id in kayitli_hesaplar.values():
        veri_tabani[after.id] = await extract_member_presence(after)

def run_api():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    threading.Thread(target=run_api, daemon=True).start()
    bot.run(os.environ.get('DISCORD_TOKEN'))