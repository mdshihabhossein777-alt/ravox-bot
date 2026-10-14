import os
import re
import json
import time
import asyncio
import gc
from collections import defaultdict
from datetime import timedelta

import discord
from discord.ext import commands
from aiohttp import web
import yt_dlp

# ---------------- ENV ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("❌ DISCORD_TOKEN env variable is missing.")

# ---------------- LOAD JSON ----------------
def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Failed to load {path}: {e}")
        return default if default is not None else {}

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Failed to save {path}: {e}")

RESPONSES = load_json("responses.json", {"exact": {}, "contains": {}, "regex": {}})
WELCOME = load_json("welcome.json", {})
VCGREET = load_json("vcgreet.json", {})
ANTILOUD = load_json("antiloud.json", {})
SECURITY = load_json("security.json", {})

COMPILED_REGEX = {re.compile(k, re.IGNORECASE): v for k, v in RESPONSES.get("regex", {}).items()}

# ---------------- YT-DLP ----------------
YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}

# ---------------- HELPERS ----------------
def is_whitelisted(member: discord.Member, config: dict) -> bool:
    if member.id == member.guild.owner_id:
        return True
    if member.id in config.get("whitelist_users", []):
        return True
    if any(r.id in config.get("whitelist_roles", []) for r in member.roles):
        return True
    return False

def now():
    return int(time.time())

# ---------------- BOT ----------------
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
        self.vc_join_tracker = defaultdict(dict)   # user_id -> last join time
        self.vc_leave_tracker = defaultdict(dict)
        self.loud_tracker = defaultdict(list)      # user_id -> list of timestamps
        self.nuke_tracker = defaultdict(list)      # (guild, action) -> timestamps

    async def on_ready(self):
        print(f"[Ravox] ✅ Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name="🎵 !play")
        )

    # ================= AUTO-RESPONSE + VC + ANTI-LOUD HOOK =================
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.content:
            return
        content = message.content.strip()
        lower = content.lower()

        if lower in RESPONSES.get("exact", {}):
            await message.reply(RESPONSES["exact"][lower], mention_author=False)
            return

        for kw, reply in RESPONSES.get("contains", {}).items():
            if kw.lower() in lower:
                await message.reply(reply, mention_author=False)
                break

        for pattern, reply in COMPILED_REGEX.items():
            if pattern.search(content):
                await message.reply(reply, mention_author=False)
                break

        await self.process_commands(message)

    # ================= WELCOME / LEAVE =================
    async def on_member_join(self, member: discord.Member):
        if not WELCOME.get("welcome_enabled"):
            return
        ch_id = WELCOME.get("welcome_channel_id")
        if not ch_id:
            return
        ch = member.guild.get_channel(ch_id)
        if not ch:
            return

        msg = WELCOME.get("welcome_message", "🎉 Welcome {user}!")
        msg = (msg
               .replace("{user}", member.mention)
               .replace("{user.name}", member.name)
               .replace("{server}", member.guild.name)
               .replace("{count}", str(member.guild.member_count)))

        try:
            embed = discord.Embed(description=msg, color=0x7C3AED)
            embed.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=embed)
        except Exception as e:
            print(f"[Welcome] Error: {e}")

        auto_role_id = WELCOME.get("auto_role_id")
        if auto_role_id:
            role = member.guild.get_role(auto_role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Auto-role on join")
                except Exception as e:
                    print(f"[AutoRole] Error: {e}")

    async def on_member_remove(self, member: discord.Member):
        if not WELCOME.get("leave_enabled"):
            return
        ch_id = WELCOME.get("leave_channel_id")
        if not ch_id:
            return
        ch = member.guild.get_channel(ch_id)
        if not ch:
            return

        msg = WELCOME.get("leave_message", "👋 Goodbye {user}.")
        msg = (msg
               .replace("{user}", str(member))
               .replace("{user.name}", member.name)
               .replace("{server}", member.guild.name)
               .replace("{count}", str(member.guild.member_count)))

        try:
            embed = discord.Embed(description=msg, color=0xEF4444)
            embed.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=embed)
        except Exception as e:
            print(f"[Leave] Error: {e}")

    # ================= VOICE SALAM + ANTI-LOUD =================
    async def on_voice_state_update(self, member, before, after):
        # ---- VC Salam ----
        if VCGREET.get("enabled"):
            try:
                if VCGREET.get("ignore_bots") and member.bot:
                    pass
                else:
                    # Join
                    if before.channel is None and after.channel is not None:
                        await self._vc_greet(member, after.channel, join=True)
                    # Leave
                    elif before.channel is not None and after.channel is None:
                        await self._vc_greet(member, before.channel, join=False)
            except Exception as e:
                print(f"[VCGreet] Error: {e}")

        # ---- Anti-Loud ----
        if ANTILOUD.get("enabled") and not member.bot:
            try:
                # Track speak patterns: going from not speaking -> speaking
                was_speaking = before.self_mute is False and before.self_deaf is False
                is_speaking = after.self_mute is False and after.self_deaf is False
                if is_speaking and after.channel is not None:
                    self.loud_tracker[member.id].append(now())
                    # keep only last N seconds
                    window = ANTILOUD.get("time_window", 10)
                    self.loud_tracker[member.id] = [
                        t for t in self.loud_tracker[member.id] if now() - t <= window
                    ]
                    if len(self.loud_tracker[member.id]) >= ANTILOUD.get("threshold", 3):
                        await self._anti_loud_punish(member)
                        self.loud_tracker[member.id] = []
            except Exception as e:
                print(f"[AntiLoud] Error: {e}")

    async def _vc_greet(self, member, channel, join: bool):
        if VCGREET.get("ignore_afk") and channel and "afk" in channel.name.lower():
            return

        last = self.vc_join_tracker if join else self.vc_leave_tracker
        cd = VCGREET.get("cooldown", 30)
        if member.id in last and now() - last[member.id] < cd:
            return
        last[member.id] = now()

        target_id = VCGREET.get("channel_id")
        if not target_id:
            return
        target = member.guild.get_channel(target_id)
        if not target:
            return

        template = VCGREET.get("join_message") if join else VCGREET.get("leave_message")
        if not template:
            return

        mention = member.mention if VCGREET.get("mention", True) else member.name
        text = (template
                .replace("{user}", mention)
                .replace("{user.name}", member.name)
                .replace("{channel}", channel.name if channel else ""))

        try:
            await target.send(text)
        except Exception as e:
            print(f"[VCGreet] Send error: {e}")

    async def _anti_loud_punish(self, member: discord.Member):
        action = ANTILOUD.get("action", "mute")
        duration = ANTILOUD.get("mute_duration", 60)
        try:
            if action == "mute":
                await member.edit(mute=True, reason="Anti-Loud: mic spam")
                await asyncio.sleep(duration)
                await member.edit(mute=False, reason="Anti-Loud: mute expired")
            elif action == "deafen":
                await member.edit(deafen=True, reason="Anti-Loud: mic spam")
            elif action == "kick":
                await member.move_to(None, reason="Anti-Loud: mic spam")

            log_id = ANTILOUD.get("log_channel_id")
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
        if not SECURITY.get("enabled"):
            return
        guild = entry.guild
        user = entry.user
        if not user:
            return
        if user.id == self.user.id:
            return
        if user.id == guild.owner_id:
            return

        # Check whitelist
        member = guild.get_member(user.id)
        if member and is_whitelisted(member, SECURITY):
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

        window = SECURITY.get("time_window", 60)
        limit = SECURITY.get("thresholds", {}).get(key, 3)

        tracker_key = (guild.id, user.id, key)
        self.nuke_tracker[tracker_key].append(now())
        self.nuke_tracker[tracker_key] = [
            t for t in self.nuke_tracker[tracker_key] if now() - t <= window
        ]

        count = len(self.nuke_tracker[tracker_key])

        # Log every action
        log_id = SECURITY.get("log_channel_id")
        if log_id:
            log_ch = guild.get_channel(log_id)
            if log_ch:
                try:
                    await log_ch.send(
                        f"🛡️ **Anti-Nuke** | {user.mention} → `{key}` | Count: **{count}/{limit}**"
                    )
                except Exception:
                    pass

        if count >= limit:
            self.nuke_tracker[tracker_key] = []
            await self._anti_nuke_punish(guild, member, user, key)

    async def _anti_nuke_punish(self, guild, member, user, key):
        punishment = SECURITY.get("punishment", "ban")
        try:
            if punishment == "ban":
                await guild.ban(user, reason=f"Anti-Nuke: {key}", delete_message_days=0)
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

        log_id = SECURITY.get("log_channel_id")
        if log_id:
            ch = guild.get_channel(log_id)
            if ch:
                try:
                    await ch.send(
                        f"🚨 **NUKE DETECTED** | {user.mention} (`{user.id}`) | Action: `{key}` | Punishment: `{punishment}`"
                    )
                except Exception:
                    pass

    # ================= MUSIC =================
    async def play_next(self, ctx: commands.Context):
        gid = ctx.guild.id
        q = self.queues.get(gid, [])
        if not q:
            return
        title, url = q.pop(0)

        vc = ctx.voice_client
        if not vc:
            if ctx.author.voice:
                vc = await ctx.author.voice.channel.connect()
            else:
                return await ctx.send("❌ Join a voice channel first.")

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
                fut = asyncio.run_coroutine_threadsafe(self.play_next(ctx), self.loop)
                try:
                    fut.result()
                except Exception as e:
                    print(f"[Music] after error: {e}")

            vc.play(source, after=after)
            await ctx.send(f"🎵 Now playing: **{title}**")
        except Exception as e:
            await ctx.send(f"❌ Error: `{e}`")
            await self.play_next(ctx)


# ---------------- COMMANDS ----------------
bot = RavoxBot()

@bot.command(name="play", aliases=["p", "suno"])
async def play(ctx, *, query: str):
    if not ctx.author.voice:
        return await ctx.send("❌ Join a voice channel first.")
    bot.queues.setdefault(ctx.guild.id, []).append((query, query))
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await bot.play_next(ctx)
    else:
        await ctx.send("📥 Added to queue.")

@bot.command(name="skip")
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped.")
    else:
        await ctx.send("❌ Nothing is playing.")

@bot.command(name="stop")
async def stop(ctx):
    bot.queues[ctx.guild.id] = []
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
    await ctx.send("⏹️ Stopped and disconnected.")

@bot.command(name="queue")
async def queue_cmd(ctx):
    q = bot.queues.get(ctx.guild.id, [])
    if not q:
        return await ctx.send("📭 Queue is empty.")
    lines = [f"`{i+1}.` {t}" for i, (t, _) in enumerate(q)]
    await ctx.send("📋 **Queue:**\n" + "\n".join(lines[:20]))

@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"🏓 Pong! `{round(bot.latency*1000)}ms`")

@bot.command(name="reload")
@commands.has_permissions(administrator=True)
async def reload_cmd(ctx):
    global RESPONSES, COMPILED_REGEX
    RESPONSES = load_json("responses.json", {"exact": {}, "contains": {}, "regex": {}})
    COMPILED_REGEX = {re.compile(k, re.IGNORECASE): v for k, v in RESPONSES.get("regex", {}).items()}
    await ctx.send("🔄 Responses reloaded.")

# ---- Welcome / Leave ----
@bot.command(name="setwelcome")
@commands.has_permissions(administrator=True)
async def setwelcome(ctx, channel: discord.TextChannel):
    WELCOME["welcome_channel_id"] = channel.id
    save_json("welcome.json", WELCOME)
    await ctx.send(f"✅ Welcome channel set to {channel.mention}")

@bot.command(name="setleave")
@commands.has_permissions(administrator=True)
async def setleave(ctx, channel: discord.TextChannel):
    WELCOME["leave_channel_id"] = channel.id
    save_json("welcome.json", WELCOME)
    await ctx.send(f"✅ Leave channel set to {channel.mention}")

@bot.command(name="setautorole")
@commands.has_permissions(administrator=True)
async def setautorole(ctx, role: discord.Role):
    WELCOME["auto_role_id"] = role.id
    save_json("welcome.json", WELCOME)
    await ctx.send(f"✅ Auto-role set to {role.mention}")

# ---- VC Salam ----
@bot.command(name="setvcgreet")
@commands.has_permissions(administrator=True)
async def setvcgreet(ctx, channel: discord.TextChannel):
    VCGREET["channel_id"] = channel.id
    save_json("vcgreet.json", VCGREET)
    await ctx.send(f"✅ VC Greet channel set to {channel.mention}")

@bot.command(name="vcgreet")
@commands.has_permissions(administrator=True)
async def vcgreet_cmd(ctx, mode: str):
    mode = mode.lower()
    if mode in ("on", "off"):
        VCGREET["enabled"] = (mode == "on")
        save_json("vcgreet.json", VCGREET)
        await ctx.send(f"✅ VC Greet turned **{mode}**.")
    elif mode == "status":
        await ctx.send(f"📊 VC Greet: `{'ON' if VCGREET.get('enabled') else 'OFF'}`")
    else:
        await ctx.send("Usage: `!vcgreet on/off/status`")

# ---- Anti-Loud ----
@bot.command(name="antiloud")
@commands.has_permissions(administrator=True)
async def antiloud_cmd(ctx, mode: str):
    mode = mode.lower()
    if mode in ("on", "off"):
        ANTILOUD["enabled"] = (mode == "on")
        save_json("antiloud.json", ANTILOUD)
        await ctx.send(f"✅ Anti-Loud turned **{mode}**.")
    elif mode == "status":
        s = "ON" if ANTILOUD.get("enabled") else "OFF"
        await ctx.send(f"📊 Anti-Loud: `{s}` | Threshold: `{ANTILOUD.get('threshold')}` | Action: `{ANTILOUD.get('action')}`")
    else:
        await ctx.send("Usage: `!antiloud on/off/status`")

@bot.command(name="antiloudlog")
@commands.has_permissions(administrator=True)
async def antiloudlog(ctx, channel: discord.TextChannel):
    ANTILOUD["log_channel_id"] = channel.id
    save_json("antiloud.json", ANTILOUD)
    await ctx.send(f"✅ Anti-Loud log set to {channel.mention}")

# ---- Anti-Nuke ----
@bot.command(name="antinuke")
@commands.has_permissions(administrator=True)
async def antinuke_cmd(ctx, mode: str):
    mode = mode.lower()
    if mode in ("on", "off"):
        SECURITY["enabled"] = (mode == "on")
        save_json("security.json", SECURITY)
        await ctx.send(f"✅ Anti-Nuke turned **{mode}**.")
    elif mode == "status":
        s = "ON" if SECURITY.get("enabled") else "OFF"
        await ctx.send(f"📊 Anti-Nuke: `{s}` | Punishment: `{SECURITY.get('punishment')}`")
    else:
        await ctx.send("Usage: `!antinuke on/off/status`")

@bot.command(name="antinukelog")
@commands.has_permissions(administrator=True)
async def antinukelog(ctx, channel: discord.TextChannel):
    SECURITY["log_channel_id"] = channel.id
    save_json("security.json", SECURITY)
    await ctx.send(f"✅ Anti-Nuke log set to {channel.mention}")

@bot.command(name="trust")
@commands.has_permissions(administrator=True)
async def trust(ctx, action: str, user: discord.Member):
    action = action.lower()
    wl = SECURITY.get("whitelist_users", [])
    if action == "add":
        if user.id not in wl:
            wl.append(user.id)
        await ctx.send(f"✅ {user.mention} trusted.")
    elif action == "remove":
        if user.id in wl:
            wl.remove(user.id)
        await ctx.send(f"✅ {user.mention} removed from trust.")
    else:
        return await ctx.send("Usage: `!trust add/remove @user`")
    SECURITY["whitelist_users"] = wl
    save_json("security.json", SECURITY)


# ---------------- HEALTH SERVER ----------------
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
    print(f"[Web] Health server running on port {port}")

# ---------------- GC ----------------
async def periodic_gc():
    while True:
        await asyncio.sleep(300)
        gc.collect()
        print("[GC] Memory collected.")

# ---------------- MAIN ----------------
async def main():
    await start_web()
    asyncio.create_task(periodic_gc())
    try:
        await bot.start(TOKEN)
    except Exception as e:
        print(f"[Bot] Crash: {e}")

if __name__ == "__main__":
    asyncio.run(main())