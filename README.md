# Ravox Bot 🎵🛡️

A multi-purpose Discord bot with Music, Auto-Response, Welcome/Leave, VC Salam, Anti-Loud, and High-Level Anti-Nuke.

## Features
- 🎵 Music: `!play`, `!skip`, `!stop`, `!queue`
- 💬 Auto-Response: exact, contains, regex
- 🎉 Welcome / Leave messages
- 🎙️ VC Salam (Bangla voice greeting)
- 🔊 Anti-Loud system
- 🛡️ High-Level Anti-Nuke
- ⚙️ Live reload, health endpoint, GC loop

## Setup
1. Create a Discord bot and enable **MESSAGE CONTENT**, **SERVER MEMBERS**, **PRESENCE** intents.
2. Deploy to Render using Docker.
3. Set `DISCORD_TOKEN` env variable.
4. Invite bot with `Administrator` permission.

## Commands
| Command | Description |
|---|---|
| `!play <url>` | Play audio |
| `!skip` | Skip current track |
| `!stop` | Stop and disconnect |
| `!queue` | Show queue |
| `!ping` | Latency |
| `!setwelcome #ch` | Set welcome channel |
| `!setleave #ch` | Set leave channel |
| `!setautorole @role` | Set auto-role |
| `!setvcgreet #ch` | Set VC greet channel |
| `!vcgreet on/off/status` | Toggle VC greet |
| `!antiloud on/off/status` | Toggle anti-loud |
| `!antiloudlog #ch` | Set anti-loud log |
| `!antinuke on/off/status` | Toggle anti-nuke |
| `!antinukelog #ch` | Set anti-nuke log |
| `!trust add/remove @user` | Whitelist user |

## Notes
- Suno has no official API. Use direct MP3 links.
- Free Render tier sleeps after 15 min. Use UptimeRobot or Starter plan ($7/mo).