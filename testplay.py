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

@app.route('/api/set_code', methods=['GET'])
def set_code():
    user = request.args.get('user')
    code = request.args.get('code')
    if user and code:
        roblox_onay_kodlari[user.strip().lower()] = code.strip()
        return "OK", 200
    return "Error", 400

async def get_lyrics(artist, title, duration):
    try:
        url = "https://lrclib.net/api/get"
        params = {"artist_name": artist, "track_name": title, "duration": duration}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=4) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("syncedLyrics") or data.get("plainLyrics") or "No lyrics available"
    except Exception:
        pass
    return "No lyrics available"

@app.route('/api/all_users')
def get_all_users():
    sonuc = {}
    for roblox_ismi, discord_id in kayitli_hesaplar.items():
        veri = veri_tabani.get(discord_id)
        if not veri:
            sonuc[roblox_ismi] = {
                "sarki": "None",
                "sarki_sure": 0,
                "sarki_toplam": 210,
                "sarki_sozu": "♪ No lyrics",
                "album_kapak": "",
                "kapak_pikseller": [],
                "kapak_sansurlu": False,
                "bant_renk": [24, 24, 24]
            }
            continue

        s_bas = veri.get("sarki_baslangic", 0)
        sarki_sure = int(time.time() - s_bas) if s_bas > 0 else 0

        aktif_soz = "♪ No lyrics"
        synced = veri.get("synced_lyrics")
        if synced:
            lines = synced.split("\n")
            current_line = ""
            gecikme_suresi = 3

            for line in lines:
                if line.startswith("[") and "]" in line:
                    try:
                        time_part = line[1:line.find("]")]
                        m, s = time_part.split(":")
                        line_sec = int(m) * 60 + float(s)

                        if (sarki_sure + gecikme_suresi) >= line_sec:
                            current_line = line[line.find("]") + 1:].strip()
                    except Exception:
                        pass
            if current_line:
                aktif_soz = "♪ " + current_line
        else:
            aktif_soz = veri.get("plain_lyrics", "♪ No lyrics")

        sonuc[roblox_ismi] = {
            "sarki": veri.get("sarki", "None"),
            "sarki_sure": sarki_sure,
            "sarki_toplam": veri.get("sarki_toplam", 210),
            "sarki_sozu": aktif_soz[:60],
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

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Closing ticket in 3 seconds...", ephemeral=False)
        await asyncio.sleep(3)
        try:
            for uid, cid in list(active_tickets.items()):
                if cid == interaction.channel.id:
                    del active_tickets[uid]
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
                try:
                    await old_channel.delete(reason="User opened a new ticket")
                except Exception:
                    pass

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        ticket_channel = await guild.create_text_channel(f"verify-{interaction.user.name}", overwrites=overwrites)
        active_tickets[user_id] = ticket_channel.id

        await interaction.response.send_message(f"Your ticket has been opened: {ticket_channel.mention}", ephemeral=True)

        embed = discord.Embed(
            title="Roblox & Discord Account Verification",
            description=(
                f"Welcome {interaction.user.mention}!\n\n"
                "To link your Roblox account, look at the **6-digit code** on your Overhead UI in the game and type:\n"
                "```!verify YourRobloxName 123456```\n"
                "*(This ticket will automatically close once verified or after 24 hours.)*"
            ),
            color=discord.Color.blurple()
        )
        await ticket_channel.send(content=interaction.user.mention, embed=embed, view=TicketControlView())
        asyncio.create_task(auto_close_ticket(ticket_channel, user_id))

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
    synced, plain = None, "♪ No lyrics"
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
                    synced = lyrics_text
                else:
                    plain = lyrics_text

    return {
        "sarki": sarki,
        "sarki_baslangic": sarki_baslangic,
        "sarki_toplam": sarki_toplam,
        "synced_lyrics": synced,
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

        veri_tabani[ctx.author.id] = await extract_member_presence(ctx.author)

        embed = discord.Embed(
            title="Verification Complete!",
            description=(
                f"**{roblox_ismi}** has been linked to {ctx.author.mention}.\n"
                "Your in-game panel will update in a few seconds. This ticket will automatically close in **4 seconds**."
            ),
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)
        del roblox_onay_kodlari[r_isim_lower]

        await asyncio.sleep(4)
        try:
            if ctx.author.id in active_tickets and active_tickets[ctx.author.id] == ctx.channel.id:
                del active_tickets[ctx.author.id]
            if ctx.channel.name.startswith("verify-"):
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