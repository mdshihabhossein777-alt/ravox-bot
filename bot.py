import os
import re
import json
import time
import asyncio
import gc
from collections import defaultdict

from dotenv import load_dotenv
load_dotenv()

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web
import yt_dlp

try:
    from welcome import generate_welcome_card, THEMES
    WELCOME_CARD_AVAILABLE = True
except ImportError:
    WELCOME_CARD_AVAILABLE = False
    THEMES = {}
    print("⚠️ welcome.py not found — image card disabled.")

# ================= ENV =================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("❌ DISCORD_TOKEN env variable is missing.")

# ================= JSON HELPERS =================
def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Save failed {path}: {e}")

RESPONSES = load_json("responses.json", {"exact": {}, "contains": {}, "regex": {}})
WELCOME = load_json("welcome.json", {})
VCGREET = load_json("vcgreet.json", {})
ANTILOUD = load_json("antiloud.json", {})
SECURITY = load_json("security.json", {})

COMPILED_REGEX = {re.compile(k, re.IGNORECASE): v for k, v in RESPONSES.get("regex", {}).items()}

# ================= YT-DLP =================
YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}

# ================= DEFAULTS =================
def default_security():
    return {
        "enabled": False,
        "log_channel_id": None,
        "bot_threshold": 1,
        "user_threshold": 3,
        "spam_window": 5,
        "spam_limit": 5,
        "punishment": "ban",
        "whitelist_bots": [],
        "whitelist_users": [],
        "whitelist_roles": [],
    }

def default_welcome():
    return {
        "welcome_channel_id": None,
        "leave_channel_id": None,
        "welcome_enabled": True,
        "leave_enabled": True,
        "theme": "galaxy",
        "type": "both",
        "image_enabled": True,
        "welcome_dm": False,
        "welcome_message": "🎉 Welcome {user} to **{server}**!",
        "leave_message": "👋 Goodbye {user}. We are now **{count}** members.",
        "auto_role_id": None,
    }

def default_vcgreet():
    return {
        "enabled": False,
        "channel_id": None,
        "cooldown": 30,
        "ignore_bots": True,
        "ignore_afk": True,
        "mention": True,
        "join_message": "🎙️ {user} joined voice. Assalamu Alaikum! 🌙",
        "leave_message": "👋 {user} left voice. Fi Amanillah.",
    }

def default_antiloud():
    return {
        "enabled": False,
        "log_channel_id": None,
        "threshold": 3,
        "time_window": 10,
        "action": "mute",
        "mute_duration": 60,
    }

def now():
    return int(time.time())

def get_cfg(store, gid, default_fn):
    gid = str(gid)
    if gid not in store:
        store[gid] = default_fn()
    else:
        base = default_fn()
        base.update(store[gid])
        store[gid] = base
    return store[gid]

# ================= BOT =================
class RavoxBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True
        intents.guilds = True
        intents.moderation = True
        intents.webhooks = True

        super().__init__(command_prefix="!", intents=intents, max_messages=None)
        self.queues = {}
        self.vc_join_tracker = defaultdict(dict)
        self.vc_leave_tracker = defaultdict(dict)
        self.loud_tracker = defaultdict(list)
        self.nuke_tracker = defaultdict(list)
        self.spam_tracker = defaultdict(list)

    async def setup_hook(self):
        # Sync slash commands globally
        try:
            synced = await self.tree.sync()
            print(f"[Slash] Synced {len(synced)} commands.")
        except Exception as e:
            print(f"[Slash] Sync error: {e}")

    async def on_ready(self):
        print(f"[Ravox] ✅ Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name="/play 🎵")
        )

    # ================= AUTO-RESPONSE + SPAM =================
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.content:
            return

        # -------- SPAM DETECTION --------
        if message.guild:
            gid = str(message.guild.id)
            cfg = SECURITY.get(gid)
            if cfg and cfg.get("enabled"):
                if message.author.id not in cfg.get("whitelist_users", []):
                    has_wl_role = any(
                        r.id in cfg.get("whitelist_roles", [])
                        for r in getattr(message.author, "roles", [])
                    )
                    if not has_wl_role and message.author.id != message.guild.owner_id:
                        key = (message.guild.id, message.author.id)
                        self.spam_tracker[key].append(now())
                        window = cfg.get("spam_window", 5)
                        limit = cfg.get("spam_limit", 5)
                        self.spam_tracker[key] = [
                            t for t in self.spam_tracker[key] if now() - t <= window
                        ]
                        if len(self.spam_tracker[key]) > limit:
                            self.spam_tracker[key] = []
                            await self._spam_punish(message, cfg)
                            return

        # -------- AUTO RESPONSE --------
        content = message.content.strip()
        lower = content.lower()

        if lower in RESPONSES.get("exact", {}):
            try:
                await message.reply(RESPONSES["exact"][lower], mention_author=False)
            except Exception:
                pass

        for kw, reply in RESPONSES.get("contains", {}).items():
            if kw.lower() in lower:
                try:
                    await message.reply(reply, mention_author=False)
                except Exception:
                    pass
                break

        for pattern, reply in COMPILED_REGEX.items():
            if pattern.search(content):
                try:
                    await message.reply(reply, mention_author=False)
                except Exception:
                    pass
                break

    # ================= SPAM PUNISH =================
    async def _spam_punish(self, message, cfg):
        guild = message.guild
        user = message.author
        try:
            try:
                await message.channel.purge(
                    limit=25,
                    check=lambda m: m.author.id == user.id,
                    reason="Anti-Spam purge",
                )
            except Exception:
                pass

            await guild.ban(user, reason="Anti-Spam: message flooding", delete_message_days=1)

            log_id = cfg.get("log_channel_id")
            if log_id:
                ch = guild.get_channel(log_id)
                if ch:
                    await ch.send(
                        f"🚨 **SPAM BLOCKED** 🚨\n"
                        f"**User:** {user.mention} (`{user.id}`)\n"
                        f"**Channel:** {message.channel.mention}\n"
                        f"**Punishment:** `ban`"
                    )
        except Exception as e:
            print(f"[AntiSpam] Error: {e}")

    # ================= WELCOME =================
    async def on_member_join(self, member: discord.Member):
        gid = str(member.guild.id)
        cfg = WELCOME.get(gid)
        if not cfg:
            return

        if cfg.get("welcome_enabled") and cfg.get("welcome_channel_id"):
            await self._send_welcome(member, cfg, join=True)

        auto_role_id = cfg.get("auto_role_id")
        if auto_role_id:
            role = member.guild.get_role(auto_role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Auto-role on join")
                except Exception as e:
                    print(f"[AutoRole] Error: {e}")

        if cfg.get("welcome_dm"):
            try:
                await member.send(f"🎉 Welcome to **{member.guild.name}**, {member.mention}!")
            except Exception:
                pass

    async def on_member_remove(self, member: discord.Member):
        gid = str(member.guild.id)
        cfg = WELCOME.get(gid)
        if not cfg:
            return
        if cfg.get("leave_enabled") and cfg.get("leave_channel_id"):
            await self._send_welcome(member, cfg, join=False)

    async def _send_welcome(self, member, cfg, join: bool):
        gid = str(member.guild.id)
        ch_id = cfg.get("welcome_channel_id") if join else cfg.get("leave_channel_id")
        if not ch_id:
            return
        ch = member.guild.get_channel(ch_id)
        if not ch:
            return

        theme = cfg.get("theme", "galaxy")
        mode = cfg.get("type", "both")
        image_enabled = cfg.get("image_enabled", True) and WELCOME_CARD_AVAILABLE

        template = cfg.get("welcome_message") if join else cfg.get("leave_message")
        msg = (template or "")
        msg = (msg
               .replace("{user}", member.mention if join else str(member))
               .replace("{user.name}", member.name)
               .replace("{server}", member.guild.name)
               .replace("{count}", str(member.guild.member_count)))

        file = None
        if image_enabled and mode in ("card", "both"):
            try:
                file = await generate_welcome_card(
                    username=member.name,
                    discriminator=member.discriminator,
                    avatar_url=member.display_avatar.url,
                    member_count=member.guild.member_count,
                    server_name=member.guild.name,
                    theme=theme,
                    action="welcome" if join else "leave",
                )
            except Exception as e:
                print(f"[Welcome] Image error: {e}")

        try:
            if mode in ("embed", "both"):
                embed = discord.Embed(
                    description=msg,
                    color=0x7C3AED if join else 0xEF4444,
                )
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.set_footer(text=f"Member #{member.guild.member_count}")
                if file:
                    embed.set_image(url=f"attachment://{'welcome' if join else 'leave'}.png")
                    await ch.send(embed=embed, file=file)
                else:
                    await ch.send(embed=embed)
            else:
                if file:
                    await ch.send(content=msg, file=file)
                else:
                    await ch.send(content=msg)
        except Exception as e:
            print(f"[Welcome] Send error: {e}")

    # ================= VOICE SALAM + ANTI-LOUD =================
    async def on_voice_state_update(self, member, before, after):
        gid = str(member.guild.id)

        # VC Salam
        cfg_vc = VCGREET.get(gid)
        if cfg_vc and cfg_vc.get("enabled"):
            try:
                if not (cfg_vc.get("ignore_bots") and member.bot):
                    if before.channel is None and after.channel is not None:
                        await self._vc_greet(member, cfg_vc, after.channel, join=True)
                    elif before.channel is not None and after.channel is None:
                        await self._vc_greet(member, cfg_vc, before.channel, join=False)
            except Exception as e:
                print(f"[VCGreet] Error: {e}")

        # Anti-Loud
        cfg_al = ANTILOUD.get(gid)
        if cfg_al and cfg_al.get("enabled") and not member.bot:
            try:
                is_speaking = (not after.self_mute) and (not after.self_deaf)
                if is_speaking and after.channel is not None:
                    uid = member.id
                    self.loud_tracker[uid].append(now())
                    window = cfg_al.get("time_window", 10)
                    self.loud_tracker[uid] = [
                        t for t in self.loud_tracker[uid] if now() - t <= window
                    ]
                    if len(self.loud_tracker[uid]) >= cfg_al.get("threshold", 3):
                        await self._anti_loud_punish(member, cfg_al)
                        self.loud_tracker[uid] = []
            except Exception as e:
                print(f"[AntiLoud] Error: {e}")

    async def _vc_greet(self, member, cfg, channel, join: bool):
        if cfg.get("ignore_afk") and channel and "afk" in channel.name.lower():
            return
        last = self.vc_join_tracker if join else self.vc_leave_tracker
        cd = cfg.get("cooldown", 30)
        if member.id in last and now() - last[member.id] < cd:
            return
        last[member.id] = now()

        target_id = cfg.get("channel_id")
        if not target_id:
            return
        target = member.guild.get_channel(target_id)
        if not target:
            return

        template = cfg.get("join_message") if join else cfg.get("leave_message")
        if not template:
            return

        mention = member.mention if cfg.get("mention", True) else member.name
        text = (template
                .replace("{user}", mention)
                .replace("{user.name}", member.name)
                .replace("{channel}", channel.name if channel else ""))
        try:
            await target.send(text)
        except Exception as e:
            print(f"[VCGreet] Send error: {e}")

    async def _anti_loud_punish(self, member: discord.Member, cfg: dict):
        action = cfg.get("action", "mute")
        duration = cfg.get("mute_duration", 60)
        try:
            if action == "mute":
                await member.edit(mute=True, reason="Anti-Loud: mic spam")
                await asyncio.sleep(duration)
                try:
                    await member.edit(mute=False, reason="Anti-Loud: mute expired")
                except Exception:
                    pass
            elif action == "deafen":
                await member.edit(deafen=True, reason="Anti-Loud: mic spam")
            elif action == "kick":
                await member.move_to(None, reason="Anti-Loud: mic spam")

            log_id = cfg.get("log_channel_id")
            if log_id:
                log_ch = member.guild.get_channel(log_id)
                if log_ch:
                    await log_ch.send(
                        f"🔊 **Anti-Loud** | {member.mention} punished | Action: `{action}`"
                    )
        except Exception as e:
            print(f"[AntiLoud] Punish error: {e}")

    # ================= ANTI-NUKE =================
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        guild = entry.guild
        if not guild:
            return
        gid = str(guild.id)
        cfg = SECURITY.get(gid)
        if not cfg or not cfg.get("enabled"):
            return

        user = entry.user
        if not user:
            return
        if user.id == self.user.id:
            return
        if user.id == guild.owner_id:
            return
        if user.id in cfg.get("whitelist_users", []):
            return

        member = guild.get_member(user.id)
        if member and any(r.id in cfg.get("whitelist_roles", []) for r in member.roles):
            return

        is_bot = user.bot
        if is_bot and user.id in cfg.get("whitelist_bots", []):
            return

        action_map = {
            discord.AuditLogAction.channel_delete: "channel_delete",
            discord.AuditLogAction.channel_create: "channel_create",
            discord.AuditLogAction.role_delete: "role_delete",
            discord.AuditLogAction.role_create: "role_create",
            discord.AuditLogAction.ban: "member_ban",
            discord.AuditLogAction.kick: "member_kick",
            discord.AuditLogAction.webhook_create: "webhook_create",
            discord.AuditLogAction.bot_add: "bot_add",
            discord.AuditLogAction.guild_update: "guild_update",
            discord.AuditLogAction.emoji_delete: "emoji_delete",
        }
        key = action_map.get(entry.action)
        if not key:
            return

        limit = cfg.get("bot_threshold", 1) if is_bot else cfg.get("user_threshold", 3)
        window = 60
        tracker_key = (guild.id, user.id, key)
        self.nuke_tracker[tracker_key].append(now())
        self.nuke_tracker[tracker_key] = [
            t for t in self.nuke_tracker[tracker_key] if now() - t <= window
        ]
        count = len(self.nuke_tracker[tracker_key])

        log_id = cfg.get("log_channel_id")
        if log_id:
            log_ch = guild.get_channel(log_id)
            if log_ch:
                try:
                    await log_ch.send(
                        f"🛡️ **Anti-Nuke** | {user.mention} (`{user.id}`) | "
                        f"Bot: `{is_bot}` | Action: `{key}` | Count: **{count}/{limit}**"
                    )
                except Exception:
                    pass

        if count > limit:
            self.nuke_tracker[tracker_key] = []
            await self._anti_nuke_punish(guild, member, user, key, is_bot, cfg)

    async def _anti_nuke_punish(self, guild, member, user, key, is_bot, cfg):
        punishment = "ban" if is_bot else cfg.get("punishment", "ban")
        try:
            if punishment == "ban":
                await guild.ban(user, reason=f"Anti-Nuke: {key}", delete_message_days=1)
            elif punishment == "kick":
                await guild.kick(user, reason=f"Anti-Nuke: {key}")
            elif punishment == "strip" and member:
                for role in member.roles[1:]:
                    try:
                        await member.remove_roles(role, reason=f"Anti-Nuke: {key}")
                    except Exception:
                        pass
        except Exception as e:
            print(f"[AntiNuke] Punish error: {e}")

        log_id = cfg.get("log_channel_id")
        if log_id:
            ch = guild.get_channel(log_id)
            if ch:
                try:
                    await ch.send(
                        f"🚨 **NUKE BLOCKED** 🚨\n"
                        f"**User:** {user.mention} (`{user.id}`)\n"
                        f"**Bot:** `{is_bot}`\n"
                        f"**Action:** `{key}`\n"
                        f"**Punishment:** `{punishment}`"
                    )
                except Exception:
                    pass

    # ================= MUSIC =================
    async def play_next(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        q = self.queues.get(gid, [])
        if not q:
            return

        title, url = q.pop(0)
        vc = interaction.guild.voice_client

        if not vc:
            if interaction.user.voice:
                vc = await interaction.user.voice.channel.connect()
            else:
                return

        try:
            with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
                info = ydl.extract_info(url, download=False)
                if "entries" in info:
                    info = info["entries"][0]
                audio_url = info["url"]
                title = info.get("title", title)

            source = discord.FFmpegPCMAudio(
                audio_url,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn",
            )

            def after(err):
                if err:
                    print(f"[Music] Player error: {err}")
                fut = asyncio.run_coroutine_threadsafe(self.play_next(interaction), self.loop)
                try:
                    fut.result()
                except Exception as e:
                    print(f"[Music] after error: {e}")

            vc.play(source, after=after)
            try:
                await interaction.channel.send(f"🎵 Now playing: **{title}**")
            except Exception:
                pass
        except Exception as e:
            print(f"[Music] Error: {e}")
            try:
                await interaction.channel.send(f"❌ Error: `{e}`")
            except Exception:
                pass


# ================= CLIENT =================
bot = RavoxBot()
tree = bot.tree


# ================= SLASH COMMANDS =================

# ---- Music ----
@tree.command(name="play", description="Play a Suno/YouTube track")
@app_commands.describe(query="Suno/YouTube URL or search")
async def play_cmd(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        return await interaction.response.send_message("❌ Join a voice channel first.", ephemeral=True)
    await interaction.response.defer()
    bot.queues.setdefault(interaction.guild.id, []).append((query, query))
    if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
        await bot.play_next(interaction)
    else:
        await interaction.followup.send("📥 Added to queue.")


@tree.command(name="skip", description="Skip the current track")
async def skip_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped.")
    else:
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)


@tree.command(name="stop", description="Stop and disconnect")
async def stop_cmd(interaction: discord.Interaction):
    bot.queues[interaction.guild.id] = []
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
    await interaction.response.send_message("⏹️ Stopped and disconnected.")


@tree.command(name="queue", description="Show the queue")
async def queue_cmd(interaction: discord.Interaction):
    q = bot.queues.get(interaction.guild.id, [])
    if not q:
        return await interaction.response.send_message("📭 Queue is empty.", ephemeral=True)
    lines = [f"`{i+1}.` {t}" for i, (t, _) in enumerate(q[:20])]
    await interaction.response.send_message("📋 **Queue:**\n" + "\n".join(lines))


@tree.command(name="ping", description="Check bot latency")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! `{round(bot.latency*1000)}ms`")


# ---- Welcome ----
@tree.command(name="setwelcome", description="Set welcome channel")
@app_commands.describe(channel="Text channel for welcome messages")
@app_commands.default_permissions(administrator=True)
async def setwelcome_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_cfg(WELCOME, interaction.guild.id, default_welcome)
    cfg["welcome_channel_id"] = channel.id
    save_json("welcome.json", WELCOME)
    await interaction.response.send_message(f"✅ Welcome channel set to {channel.mention}", ephemeral=True)


@tree.command(name="setleave", description="Set leave channel")
@app_commands.default_permissions(administrator=True)
async def setleave_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_cfg(WELCOME, interaction.guild.id, default_welcome)
    cfg["leave_channel_id"] = channel.id
    save_json("welcome.json", WELCOME)
    await interaction.response.send_message(f"✅ Leave channel set to {channel.mention}", ephemeral=True)


@tree.command(name="setautorole", description="Set auto-role for new members")
@app_commands.default_permissions(administrator=True)
async def setautorole_cmd(interaction: discord.Interaction, role: discord.Role):
    cfg = get_cfg(WELCOME, interaction.guild.id, default_welcome)
    cfg["auto_role_id"] = role.id
    save_json("welcome.json", WELCOME)
    await interaction.response.send_message(f"✅ Auto-role set to {role.mention}", ephemeral=True)


@tree.command(name="welcometheme", description="Choose a welcome card theme")
@app_commands.describe(theme="Theme name")
@app_commands.choices(theme=[
    app_commands.Choice(name="Galaxy 🌌", value="galaxy"),
    app_commands.Choice(name="Neon 💡", value="neon"),
    app_commands.Choice(name="Dark 🖤", value="dark"),
    app_commands.Choice(name="Fire 🔥", value="fire"),
    app_commands.Choice(name="Ocean 🌊", value="ocean"),
    app_commands.Choice(name="Anime 🌸", value="anime"),
])
@app_commands.default_permissions(administrator=True)
async def welcometheme_cmd(interaction: discord.Interaction, theme: app_commands.Choice[str]):
    cfg = get_cfg(WELCOME, interaction.guild.id, default_welcome)
    cfg["theme"] = theme.value
    save_json("welcome.json", WELCOME)
    await interaction.response.send_message(f"✅ Theme set to **{theme.name}**", ephemeral=True)


@tree.command(name="welcometype", description="Choose welcome message type")
@app_commands.choices(mode=[
    app_commands.Choice(name="Card only", value="card"),
    app_commands.Choice(name="Embed only", value="embed"),
    app_commands.Choice(name="Card + Embed", value="both"),
])
@app_commands.default_permissions(administrator=True)
async def welcometype_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    cfg = get_cfg(WELCOME, interaction.guild.id, default_welcome)
    cfg["type"] = mode.value
    save_json("welcome.json", WELCOME)
    await interaction.response.send_message(f"✅ Welcome type: **{mode.name}**", ephemeral=True)


@tree.command(name="welcomedm", description="Toggle DM welcome")
@app_commands.choices(mode=[
    app_commands.Choice(name="On", value="on"),
    app_commands.Choice(name="Off", value="off"),
])
@app_commands.default_permissions(administrator=True)
async def welcomedm_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    cfg = get_cfg(WELCOME, interaction.guild.id, default_welcome)
    cfg["welcome_dm"] = (mode.value == "on")
    save_json("welcome.json", WELCOME)
    await interaction.response.send_message(f"✅ DM Welcome: **{mode.name}**", ephemeral=True)


@tree.command(name="testwelcome", description="Test welcome message")
@app_commands.default_permissions(administrator=True)
async def testwelcome_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await bot.on_member_join(interaction.user)
    await interaction.followup.send("✅ Test welcome sent!", ephemeral=True)


# ---- VC Greet ----
@tree.command(name="setvcgreet", description="Set VC greet channel")
@app_commands.default_permissions(administrator=True)
async def setvcgreet_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_cfg(VCGREET, interaction.guild.id, default_vcgreet)
    cfg["channel_id"] = channel.id
    save_json("vcgreet.json", VCGREET)
    await interaction.response.send_message(f"✅ VC Greet channel: {channel.mention}", ephemeral=True)


@tree.command(name="vcgreet", description="Toggle VC greet")
@app_commands.choices(mode=[
    app_commands.Choice(name="On", value="on"),
    app_commands.Choice(name="Off", value="off"),
    app_commands.Choice(name="Status", value="status"),
])
@app_commands.default_permissions(administrator=True)
async def vcgreet_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    cfg = get_cfg(VCGREET, interaction.guild.id, default_vcgreet)
    if mode.value == "status":
        s = "ON" if cfg.get("enabled") else "OFF"
        return await interaction.response.send_message(f"📊 VC Greet: `{s}`", ephemeral=True)
    cfg["enabled"] = (mode.value == "on")
    save_json("vcgreet.json", VCGREET)
    await interaction.response.send_message(f"✅ VC Greet: **{mode.name}**", ephemeral=True)


# ---- Anti-Loud ----
@tree.command(name="antiloud", description="Toggle anti-loud")
@app_commands.choices(mode=[
    app_commands.Choice(name="On", value="on"),
    app_commands.Choice(name="Off", value="off"),
    app_commands.Choice(name="Status", value="status"),
])
@app_commands.default_permissions(administrator=True)
async def antiloud_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    cfg = get_cfg(ANTILOUD, interaction.guild.id, default_antiloud)
    if mode.value == "status":
        s = "ON" if cfg.get("enabled") else "OFF"
        return await interaction.response.send_message(
            f"📊 Anti-Loud: `{s}` | Threshold: `{cfg.get('threshold')}` | Action: `{cfg.get('action')}`",
            ephemeral=True,
        )
    cfg["enabled"] = (mode.value == "on")
    save_json("antiloud.json", ANTILOUD)
    await interaction.response.send_message(f"✅ Anti-Loud: **{mode.name}**", ephemeral=True)


@tree.command(name="antiloudlog", description="Set Anti-Loud log channel")
@app_commands.default_permissions(administrator=True)
async def antiloudlog_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_cfg(ANTILOUD, interaction.guild.id, default_antiloud)
    cfg["log_channel_id"] = channel.id
    save_json("antiloud.json", ANTILOUD)
    await interaction.response.send_message(f"✅ Anti-Loud log: {channel.mention}", ephemeral=True)


# ---- Anti-Nuke ----
@tree.command(name="antinuke", description="Toggle Anti-Nuke")
@app_commands.choices(mode=[
    app_commands.Choice(name="On", value="on"),
    app_commands.Choice(name="Off", value="off"),
    app_commands.Choice(name="Status", value="status"),
])
@app_commands.default_permissions(administrator=True)
async def antinuke_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    cfg = get_cfg(SECURITY, interaction.guild.id, default_security)
    if mode.value == "status":
        s = "ON" if cfg.get("enabled") else "OFF"
        return await interaction.response.send_message(
            f"🛡️ **Anti-Nuke:** `{s}`\n"
            f"Bot Threshold: `{cfg.get('bot_threshold')}`\n"
            f"User Threshold: `{cfg.get('user_threshold')}`\n"
            f"Spam Limit: `{cfg.get('spam_limit')}/{cfg.get('spam_window')}s`\n"
            f"Punishment: `{cfg.get('punishment')}`",
            ephemeral=True,
        )
    cfg["enabled"] = (mode.value == "on")
    save_json("security.json", SECURITY)
    await interaction.response.send_message(f"✅ Anti-Nuke: **{mode.name}**", ephemeral=True)


@tree.command(name="antinukelog", description="Set Anti-Nuke log channel")
@app_commands.default_permissions(administrator=True)
async def antinukelog_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_cfg(SECURITY, interaction.guild.id, default_security)
    cfg["log_channel_id"] = channel.id
    save_json("security.json", SECURITY)
    await interaction.response.send_message(f"✅ Anti-Nuke log: {channel.mention}", ephemeral=True)


@tree.command(name="trust", description="Whitelist a user from Anti-Nuke")
@app_commands.choices(action=[
    app_commands.Choice(name="Add", value="add"),
    app_commands.Choice(name="Remove", value="remove"),
])
@app_commands.describe(user="User to trust/untrust")
@app_commands.default_permissions(administrator=True)
async def trust_cmd(interaction: discord.Interaction, action: app_commands.Choice[str], user: discord.Member):
    cfg = get_cfg(SECURITY, interaction.guild.id, default_security)
    wl = cfg.get("whitelist_users", [])
    if action.value == "add":
        if user.id not in wl:
            wl.append(user.id)
        await interaction.response.send_message(f"✅ {user.mention} trusted.", ephemeral=True)
    else:
        if user.id in wl:
            wl.remove(user.id)
        await interaction.response.send_message(f"✅ {user.mention} untrusted.", ephemeral=True)
    cfg["whitelist_users"] = wl
    save_json("security.json", SECURITY)


@tree.command(name="trustbot", description="Whitelist a bot from Anti-Nuke")
@app_commands.choices(action=[
    app_commands.Choice(name="Add", value="add"),
    app_commands.Choice(name="Remove", value="remove"),
])
@app_commands.describe(bot_user="Bot user to trust/untrust")
@app_commands.default_permissions(administrator=True)
async def trustbot_cmd(interaction: discord.Interaction, action: app_commands.Choice[str], bot_user: discord.Member):
    cfg = get_cfg(SECURITY, interaction.guild.id, default_security)
    wl = cfg.get("whitelist_bots", [])
    if action.value == "add":
        if bot_user.id not in wl:
            wl.append(bot_user.id)
        await interaction.response.send_message(f"✅ Bot {bot_user.mention} trusted.", ephemeral=True)
    else:
        if bot_user.id in wl:
            wl.remove(bot_user.id)
        await interaction.response.send_message(f"✅ Bot {bot_user.mention} untrusted.", ephemeral=True)
    cfg["whitelist_bots"] = wl
    save_json("security.json", SECURITY)


# ================= HEALTH SERVER =================
async def health(request):
    return web.Response(text="Ravox is alive 🎵")


async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[Web] Health server on port {port}")


# ================= GC =================
async def periodic_gc():
    while True:
        await asyncio.sleep(300)
        gc.collect()
        print("[GC] Memory collected.")


# ================= MAIN =================
async def main():
    await start_web()
    asyncio.create_task(periodic_gc())
    try:
        await bot.start(TOKEN)
    except Exception as e:
        print(f"[Bot] Crash: {e}")


if __name__ == "__main__":
    asyncio.run(main())