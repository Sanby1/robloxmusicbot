import discord
from discord.ext import commands
from flask import Flask, jsonify, request
import threading
import time
import asyncio
import aiohttp
import os
import json
from io import BytesIO
from PIL import Image

app = Flask(__name__)

DB_FILE = "verified_accounts.json"
GRID_RES = 24  # 24x24 = 576 piksel (Tıpkı örnek oyundaki gibi yüksek netlikli albüm kapağı)

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
cover_cache = {} # URL -> {"pixels": [...], "color": [r, g, b]}

SIGN_KANAL_ID = 123456789012345678 # Put your Discord channel ID here
VERIFIED_ROLE_ID = 1540891610739507331 # Put your Verified role ID here

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

# Converts Spotify Album Cover URL ( or iTunes fallback) into a 24x24 (576 cells) RGB Hex Matrix
async def fetch_album_cover_pixels(url, artist="", title=""):
    target_url = url
    # If Discord didn't provide a cover URL, search iTunes API as a backup
    if not target_url and artist and title:
        try:
            search_q = f"{artist} {title}"
            async with aiohttp.ClientSession() as session:
                async with session.get("https://itunes.apple.com/search", params={"term": search_q, "entity": "song", "limit": 1}, timeout=3) as r:
                    if r.status == 200:
                        js = await r.json(content_type=None)
                        results = js.get("results", [])
                        if results:
                            target_url = results[0].get("artworkUrl100", "")
        except Exception:
            pass

    if not target_url:
        return [], [24, 24, 24]

    if target_url in cover_cache:
        return cover_cache[target_url]["pixels"], cover_cache[target_url]["color"]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(target_url, timeout=4) as resp:
                if resp.status == 200:
                    img_bytes = await resp.read()
                    img = Image.open(BytesIO(img_bytes)).convert("RGB")
                    resample_filter = getattr(Image, "Resampling", Image).LANCZOS

                    dom = img.resize((1, 1), resample_filter).getpixel((0, 0))
                    dom_color = [int(dom[0]), int(dom[1]), int(dom[2])]

                    small = img.resize((GRID_RES, GRID_RES), resample_filter)
                    pixels = []
                    for y in range(GRID_RES):
                        for x in range(GRID_RES):
                            r, g, b = small.getpixel((x, y))
                            pixels.append(f"{r:02x}{g:02x}{b:02x}")

                    if len(cover_cache) > 150:
                        cover_cache.clear()
                    cover_cache[target_url] = {"pixels": pixels, "color": dom_color}
                    return pixels, dom_color
    except Exception as e:
        print("Cover image error:", e)
    return [], [24, 24, 24]

@app.route('/api/all_users')
def get_all_users():
    sonuc = {}
    for roblox_ismi, discord_id in kayitli_hesaplar.items():
        veri = veri_tabani.get(discord_id)
        if not veri:
            sonuc[roblox_ismi] = {
                "oyun": "None",
                "sarki": "None",
                "oyun_sure": 0,
                "sarki_sure": 0,
                "sarki_toplam": 210,
                "sarki_sozu": "♪ No lyrics",
                "album_kapak": "",
                "kapak_pikseller": [],
                "bant_renk": [24, 24, 24]
            }
            continue

        o_bas = veri.get("oyun_baslangic", 0)
        s_bas = veri.get("sarki_baslangic", 0)

        oyun_sure = int(time.time() - o_bas) if o_bas > 0 else 0
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
            "oyun": veri.get("oyun", "None"),
            "oyun_sure": oyun_sure,
            "sarki": veri.get("sarki", "None"),
            "sarki_sure": sarki_sure,
            "sarki_toplam": veri.get("sarki_toplam", 210),
            "sarki_sozu": aktif_soz[:60],
            "album_kapak": veri.get("album_kapak", ""),
            "kapak_pikseller": veri.get("kapak_pikseller", []),
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
                    description="Click the button below to open a verification ticket and display your live Spotify & gaming status in Roblox!",
                    color=discord.Color.green()
                )
                await kanal.send(embed=embed, view=TicketView())

bot = MyBot()

async def extract_member_presence(member: discord.Member):
    oyun, sarki = "None", "None"
    oyun_baslangic, sarki_baslangic, sarki_toplam = 0, 0, 210
    synced, plain = None, "♪ No lyrics"
    album_kapak = ""
    kapak_pikseller = []
    bant_renk = [24, 24, 24]

    for act in member.activities:
        if act.type == discord.ActivityType.playing:
            oyun = act.name
            if hasattr(act, 'start') and act.start:
                oyun_baslangic = act.start.timestamp()
        elif isinstance(act, discord.Spotify):
            sarki = f"{act.artist} - {act.title}"
            if hasattr(act, 'start') and act.start:
                sarki_baslangic = act.start.timestamp()
            if hasattr(act, 'duration') and act.duration:
                sarki_toplam = int(act.duration.total_seconds())
            if hasattr(act, 'album_cover_url') and act.album_cover_url:
                album_kapak = str(act.album_cover_url)

            kapak_pikseller, bant_renk = await fetch_album_cover_pixels(album_kapak, act.artist, act.title)

            lyrics_text = await get_lyrics(act.artist, act.title, sarki_toplam)
            if lyrics_text != "No lyrics available":
                if "\n[" in lyrics_text or lyrics_text.startswith("["):
                    synced = lyrics_text
                else:
                    plain = lyrics_text

    return {
        "oyun": oyun,
        "oyun_baslangic": oyun_baslangic,
        "sarki": sarki,
        "sarki_baslangic": sarki_baslangic,
        "sarki_toplam": sarki_toplam,
        "synced_lyrics": synced,
        "plain_lyrics": plain,
        "album_kapak": album_kapak,
        "kapak_pikseller": kapak_pikseller,
        "bant_renk": bant_renk
    }

@bot.command()
async def setup_ticket(ctx):
    if ctx.author.guild_permissions.administrator:
        embed = discord.Embed(
            title="Link Your Roblox & Discord Accounts",
            description="Click the button below to open a verification ticket and display your live Spotify & gaming status in Roblox!",
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
    app.run(host='0.0.0.0', port=5000)

if __name__ == "__main__":
    threading.Thread(target=run_api, daemon=True).start()
    bot.run(os.environ.get('DISCORD_TOKEN'))