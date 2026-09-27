import asyncio
import io
import random
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ============================================================
# PRIME CLOUD Official Bot
# discord.py 2.6+
# ============================================================

TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"
PREFIX = "!"
DB_FILE = "primecloud.db"
REVIEW_CHANNEL_ID = 1553695179666366565

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.messages = True

YELLOW = discord.Color.from_rgb(255, 193, 7)
GREEN = discord.Color.green()
RED = discord.Color.red()
BLUE = discord.Color.blurple()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def unix_now() -> int:
    return int(utc_now().timestamp())


def parse_duration(value: str) -> Optional[int]:
    value = value.lower().strip()
    if len(value) < 2:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        number = float(value[:-1])
        unit = value[-1]
        if number <= 0 or unit not in units:
            return None
        return int(number * units[unit])
    except ValueError:
        return None


def embed(title: str, description: str = "", color: discord.Color = YELLOW) -> discord.Embed:
    e = discord.Embed(
        title=f"💛 {title}",
        description=description,
        color=color,
        timestamp=utc_now(),
    )
    e.set_footer(text="PRIME CLOUD • |prime.gg")
    return e


def reason_text(reason: Optional[str]) -> str:
    return reason.strip() if reason and reason.strip() else "No reason provided."


class Database:
    """Small SQLite persistence layer. The database is created automatically."""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                ticket_category_id INTEGER,
                staff_role_id INTEGER,
                ticket_panel_channel_id INTEGER,
                welcome_channel_id INTEGER,
                welcome_message TEXT DEFAULT 'Welcome {user} to {server}! 💛',
                goodbye_channel_id INTEGER,
                goodbye_message TEXT DEFAULT '{user} has left {server}. 👋',
                autorole_id INTEGER,
                log_channel_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS triggers (
                guild_id INTEGER NOT NULL,
                trigger TEXT NOT NULL,
                response TEXT NOT NULL,
                PRIMARY KEY (guild_id, trigger)
            );

            CREATE TABLE IF NOT EXISTS tickets (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                ticket_type TEXT NOT NULL,
                claimed_by INTEGER,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS giveaways (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                prize TEXT NOT NULL,
                winners INTEGER NOT NULL,
                ends_at INTEGER NOT NULL,
                ended INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS warns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        self.conn.commit()

    def settings(self, guild_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,)
        ).fetchone()
        if row:
            return row
        self.conn.execute(
            "INSERT INTO guild_settings(guild_id) VALUES(?)", (guild_id,)
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,)
        ).fetchone()

    def set_setting(self, guild_id: int, key: str, value):
        allowed = {
            "ticket_category_id",
            "staff_role_id",
            "ticket_panel_channel_id",
            "welcome_channel_id",
            "welcome_message",
            "goodbye_channel_id",
            "goodbye_message",
            "autorole_id",
            "log_channel_id",
        }
        if key not in allowed:
            raise ValueError("Invalid setting.")
        self.settings(guild_id)
        self.conn.execute(
            f"UPDATE guild_settings SET {key}=? WHERE guild_id=?",
            (value, guild_id),
        )
        self.conn.commit()

    def trigger_add(self, guild_id: int, trigger: str, response: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO triggers(guild_id,trigger,response) VALUES(?,?,?)",
            (guild_id, trigger.lower(), response),
        )
        self.conn.commit()

    def trigger_remove(self, guild_id: int, trigger: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM triggers WHERE guild_id=? AND trigger=?",
            (guild_id, trigger.lower()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def trigger_get(self, guild_id: int, trigger: str):
        return self.conn.execute(
            "SELECT response FROM triggers WHERE guild_id=? AND trigger=?",
            (guild_id, trigger.lower()),
        ).fetchone()

    def trigger_list(self, guild_id: int):
        return self.conn.execute(
            "SELECT trigger,response FROM triggers WHERE guild_id=? ORDER BY trigger",
            (guild_id,),
        ).fetchall()

    def ticket_get(self, channel_id: int):
        return self.conn.execute(
            "SELECT * FROM tickets WHERE channel_id=?", (channel_id,)
        ).fetchone()

    def ticket_for_user(self, guild_id: int, user_id: int):
        return self.conn.execute(
            "SELECT * FROM tickets WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()

    def ticket_add(self, channel_id: int, guild_id: int, user_id: int, ticket_type: str):
        self.conn.execute(
            """
            INSERT INTO tickets(channel_id,guild_id,user_id,ticket_type,created_at)
            VALUES(?,?,?,?,?)
            """,
            (channel_id, guild_id, user_id, ticket_type, unix_now()),
        )
        self.conn.commit()

    def ticket_claim(self, channel_id: int, user_id: int):
        self.conn.execute(
            "UPDATE tickets SET claimed_by=? WHERE channel_id=?",
            (user_id, channel_id),
        )
        self.conn.commit()

    def ticket_delete(self, channel_id: int):
        self.conn.execute(
            "DELETE FROM tickets WHERE channel_id=?", (channel_id,)
        )
        self.conn.commit()

    def warn_add(self, guild_id: int, user_id: int, moderator_id: int, reason: str):
        self.conn.execute(
            """
            INSERT INTO warns(guild_id,user_id,moderator_id,reason,created_at)
            VALUES(?,?,?,?,?)
            """,
            (guild_id, user_id, moderator_id, reason, unix_now()),
        )
        self.conn.commit()

    def giveaway_add(
        self, message_id: int, guild_id: int, channel_id: int,
        prize: str, winners: int, ends_at: int
    ):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO giveaways
            (message_id,guild_id,channel_id,prize,winners,ends_at,ended)
            VALUES(?,?,?,?,?,?,0)
            """,
            (message_id, guild_id, channel_id, prize, winners, ends_at),
        )
        self.conn.commit()

    def giveaway_active(self):
        return self.conn.execute(
            "SELECT * FROM giveaways WHERE ended=0"
        ).fetchall()

    def giveaway_end(self, message_id: int):
        self.conn.execute(
            "UPDATE giveaways SET ended=1 WHERE message_id=?", (message_id,)
        )
        self.conn.commit()


db = Database(DB_FILE)


# ============================================================
# Permission helpers
# ============================================================

def member_has(member: discord.Member, permission: str) -> bool:
    return member.guild_permissions.administrator or getattr(
        member.guild_permissions, permission, False
    )


def ticket_staff(member: discord.Member, guild: discord.Guild) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_channels:
        return True
    settings = db.settings(guild.id)
    role_id = settings["staff_role_id"]
    return bool(role_id and any(r.id == role_id for r in member.roles))


def bot_has(guild: discord.Guild, permission: str) -> bool:
    me = guild.me
    return bool(me and getattr(me.guild_permissions, permission, False))


async def get_channel(guild: discord.Guild, channel_id: Optional[int]):
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel:
        return channel
    try:
        return await guild.fetch_channel(channel_id)
    except discord.DiscordException:
        return None


async def log_event(
    guild: discord.Guild, title: str, description: str,
    color: discord.Color = YELLOW
):
    settings = db.settings(guild.id)
    channel = await get_channel(guild, settings["log_channel_id"])
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed(title, description, color))
        except discord.DiscordException:
            pass


# ============================================================
# Ticket system
# ============================================================

TICKET_TYPES = {
    "purchase": ("🛒", "Purchase / Sales"),
    "technical": ("🛠️", "Technical Support"),
    "payment": ("💳", "Payment Support"),
    "partnership": ("🤝", "Partnership"),
    "report": ("🚨", "Report"),
    "general": ("❓", "General Support"),
}


async def make_transcript(channel: discord.TextChannel) -> Optional[bytes]:
    try:
        lines = [
            "PRIME CLOUD TICKET TRANSCRIPT",
            f"Server: {channel.guild.name}",
            f"Channel: #{channel.name}",
            f"Generated: {utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "",
        ]
        async for msg in channel.history(limit=None, oldest_first=True):
            attachments = " ".join(a.url for a in msg.attachments)
            content = msg.content.replace("\n", "\\n")
            lines.append(
                f"[{msg.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}] "
                f"{msg.author} ({msg.author.id}): {content} {attachments}".strip()
            )
        return "\n".join(lines).encode("utf-8")
    except discord.DiscordException:
        return None


async def create_ticket(interaction: discord.Interaction, ticket_type: str):
    guild = interaction.guild
    user = interaction.user
    if not guild or not isinstance(user, discord.Member):
        return await interaction.response.send_message(
            "This can only be used inside a server.", ephemeral=True
        )

    existing = db.ticket_for_user(guild.id, user.id)
    if existing:
        existing_channel = guild.get_channel(existing["channel_id"])
        if existing_channel:
            return await interaction.response.send_message(
                f"You already have an open ticket: {existing_channel.mention}",
                ephemeral=True,
            )
        db.ticket_delete(existing["channel_id"])

    settings = db.settings(guild.id)
    category = guild.get_channel(settings["ticket_category_id"]) if settings["ticket_category_id"] else None
    if category and not isinstance(category, discord.CategoryChannel):
        return await interaction.response.send_message(
            "The configured ticket category is invalid. Ask staff to run `/ticket setup` again.",
            ephemeral=True,
        )
    if not bot_has(guild, "manage_channels"):
        return await interaction.response.send_message(
            "I need Manage Channels permission to create tickets.", ephemeral=True
        )

    emoji, label = TICKET_TYPES[ticket_type]
    name = f"{emoji}-{user.name}"[:95]
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
    }

    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True,
        )

    staff_role = guild.get_role(settings["staff_role_id"]) if settings["staff_role_id"] else None
    if staff_role:
        if guild.me and staff_role >= guild.me.top_role:
            return await interaction.response.send_message(
                "The configured staff role is above my highest role. Move my bot role above it.",
                ephemeral=True,
            )
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True,
        )

    try:
        channel = await guild.create_text_channel(
            name=name,
            category=category,
            overwrites=overwrites,
            reason=f"PrimeCloud ticket: {label}",
        )
        db.ticket_add(channel.id, guild.id, user.id, ticket_type)
        await log_event(
            guild,
            "Ticket Created",
            f"👤 {user.mention}\n🎫 {channel.mention}\n📂 {label}",
        )
        e = embed(
            "Ticket Created",
            f"Welcome {user.mention}!\n\n"
            f"**Category:** {emoji} {label}\n"
            "Please explain your request clearly. Authorized staff will assist you.",
        )
        await channel.send(
            content=user.mention,
            embed=e,
            view=TicketControlView(),
        )
        await interaction.response.send_message(
            f"Ticket created: {channel.mention}", ephemeral=True
        )
    except discord.DiscordException as exc:
        await interaction.response.send_message(
            f"Ticket creation failed: `{exc}`", ephemeral=True
        )


class TicketSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="▼ Select Ticket Type",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=key, emoji=emoji)
                for key, (emoji, label) in TICKET_TYPES.items()
            ],
            custom_id="primecloud:ticket_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.values[0])


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


async def staff_button(interaction: discord.Interaction) -> bool:
    if (
        not interaction.guild
        or not isinstance(interaction.user, discord.Member)
        or not ticket_staff(interaction.user, interaction.guild)
    ):
        await interaction.response.send_message(
            "You need authorized ticket staff permissions.", ephemeral=True
        )
        return False
    return True


class AddUserModal(discord.ui.Modal, title="Add User to Ticket"):
    user_id = discord.ui.TextInput(
        label="User ID",
        placeholder="Enter Discord user ID",
        required=True,
        max_length=25,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            member = await interaction.guild.fetch_member(int(self.user_id.value.strip()))
            await interaction.channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )
            await interaction.response.send_message(
                f"👥 Added {member.mention} to this ticket."
            )
        except (ValueError, discord.NotFound):
            await interaction.response.send_message(
                "Invalid member ID.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot change that user's ticket permissions.", ephemeral=True
            )


class RemoveUserModal(discord.ui.Modal, title="Remove User from Ticket"):
    user_id = discord.ui.TextInput(
        label="User ID",
        placeholder="Enter Discord user ID",
        required=True,
        max_length=25,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            member = await interaction.guild.fetch_member(int(self.user_id.value.strip()))
            await interaction.channel.set_permissions(member, overwrite=None)
            await interaction.response.send_message(
                f"👤 Removed {member.mention} from this ticket."
            )
        except (ValueError, discord.NotFound):
            await interaction.response.send_message(
                "Invalid member ID.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot change that user's ticket permissions.", ephemeral=True
            )


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close", emoji="🔒", style=discord.ButtonStyle.secondary,
        custom_id="primecloud:ticket_close"
    )
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        guild = interaction.guild
        if not guild or not isinstance(interaction.user, discord.Member):
            return
        ticket = db.ticket_get(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message(
                "This is not an active ticket.", ephemeral=True
            )

        owner = ticket["user_id"] == interaction.user.id
        if not (owner or ticket_staff(interaction.user, guild)):
            return await interaction.response.send_message(
                "Only the ticket owner or authorized staff can close this ticket.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "🔒 Closing ticket and archiving its record..."
        )
        channel = interaction.channel
        transcript = await make_transcript(channel) if isinstance(channel, discord.TextChannel) else None

        # Hide the ticket after closure. It remains available to staff for transcript/delete.
        owner_member = guild.get_member(ticket["user_id"])
        if owner_member:
            await channel.set_permissions(
                owner_member, view_channel=False, send_messages=False
            )

        settings = db.settings(guild.id)
        staff_role = guild.get_role(settings["staff_role_id"]) if settings["staff_role_id"] else None
        if staff_role:
            await channel.set_permissions(
                staff_role,
                view_channel=True,
                send_messages=False,
                read_message_history=True,
            )

        db.ticket_delete(interaction.channel_id)
        if transcript:
            try:
                await channel.send(
                    embed=embed(
                        "Ticket Closed",
                        f"🔒 Closed by {interaction.user.mention}\n"
                        "The ticket is now hidden from the creator.",
                    ),
                    file=discord.File(
                        io.BytesIO(transcript),
                        filename=f"{channel.name}-transcript.txt",
                    ),
                )
            except discord.DiscordException:
                pass

        await log_event(
            guild,
            "Ticket Closed",
            f"🔒 `{channel.name}` closed by {interaction.user.mention}",
        )

    @discord.ui.button(
        label="Claim", emoji="🙋", style=discord.ButtonStyle.primary,
        custom_id="primecloud:ticket_claim"
    )
    async def claim(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await staff_button(interaction):
            return
        ticket = db.ticket_get(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message(
                "This is not an active ticket.", ephemeral=True
            )
        db.ticket_claim(interaction.channel_id, interaction.user.id)
        await interaction.response.send_message(
            f"🙋 {interaction.user.mention} claimed this ticket."
        )
        await log_event(
            interaction.guild,
            "Ticket Claimed",
            f"{interaction.channel.mention} claimed by {interaction.user.mention}",
        )

    @discord.ui.button(
        label="Add User", emoji="👥", style=discord.ButtonStyle.success,
        custom_id="primecloud:ticket_add"
    )
    async def add_user(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await staff_button(interaction):
            await interaction.response.send_modal(AddUserModal())

    @discord.ui.button(
        label="Remove User", emoji="👤", style=discord.ButtonStyle.secondary,
        custom_id="primecloud:ticket_remove"
    )
    async def remove_user(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await staff_button(interaction):
            await interaction.response.send_modal(RemoveUserModal())

    @discord.ui.button(
        label="Transcript", emoji="📄", style=discord.ButtonStyle.primary,
        custom_id="primecloud:ticket_transcript"
    )
    async def transcript(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await staff_button(interaction):
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message(
                "This is not a text ticket.", ephemeral=True
            )
        data = await make_transcript(interaction.channel)
        if data is None:
            return await interaction.response.send_message(
                "Could not generate transcript.", ephemeral=True
            )
        await interaction.response.send_message(
            "📄 Transcript generated.",
            file=discord.File(
                io.BytesIO(data),
                filename=f"{interaction.channel.name}-transcript.txt",
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger,
        custom_id="primecloud:ticket_delete"
    )
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await staff_button(interaction):
            return
        ticket = db.ticket_get(interaction.channel_id)
        if ticket:
            db.ticket_delete(interaction.channel_id)
        await interaction.response.send_message("🗑️ Deleting ticket...", ephemeral=True)
        await log_event(
            interaction.guild,
            "Ticket Deleted",
            f"🗑️ `{interaction.channel.name}` deleted by {interaction.user.mention}",
        )
        await asyncio.sleep(1)
        try:
            await interaction.channel.delete(reason=f"Ticket deleted by {interaction.user}")
        except discord.DiscordException:
            pass


# ============================================================
# Reviews
# ============================================================

class ReviewSelect(discord.ui.Select):
    def __init__(self, review_text: str, screenshot_url: Optional[str]):
        self.review_text = review_text
        self.screenshot_url = screenshot_url
        super().__init__(
            placeholder="Select your rating (1–5)",
            options=[
                discord.SelectOption(
                    label=f"{n} Star" + ("s" if n != 1 else ""),
                    value=str(n),
                    emoji="⭐",
                )
                for n in range(1, 6)
            ],
            custom_id="primecloud:review_rating",
        )

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(REVIEW_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                f"Review channel `{REVIEW_CHANNEL_ID}` was not found.",
                ephemeral=True,
            )

        rating = int(self.values[0])
        e = discord.Embed(
            title="💛 PRIME CLOUD",
            description="⭐ **Customer Review**",
            color=YELLOW,
            timestamp=utc_now(),
        )
        e.set_thumbnail(url=interaction.user.display_avatar.url)
        e.add_field(name="👤 Customer", value=interaction.user.mention, inline=False)
        e.add_field(
            name="⭐ Rating",
            value=f"{'⭐' * rating} {rating}/5",
            inline=False,
        )
        e.add_field(
            name="💬 Review",
            value=self.review_text[:1024],
            inline=False,
        )
        if self.screenshot_url:
            e.add_field(name="📸 Screenshot", value="[Screenshot attached]", inline=False)
            e.set_image(url=self.screenshot_url)
        e.set_footer(text="PRIME CLOUD • Customer Review")

        try:
            await channel.send(embed=e)
            await interaction.response.send_message(
                "💛 Thank you! Your review has been submitted.",
                ephemeral=True,
            )
            await interaction.message.edit(view=None)
        except discord.DiscordException as exc:
            await interaction.response.send_message(
                f"Could not submit review: `{exc}`", ephemeral=True
            )


class ReviewView(discord.ui.View):
    def __init__(self, review_text: str, screenshot_url: Optional[str]):
        super().__init__(timeout=300)
        self.add_item(ReviewSelect(review_text, screenshot_url))


# ============================================================
# Giveaways
# ============================================================

class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Join Giveaway", emoji="🎉", style=discord.ButtonStyle.success,
        custom_id="primecloud:giveaway_join"
    )
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await interaction.message.add_reaction("🎉")
            await interaction.response.send_message(
                "🎉 You are entered into the giveaway!", ephemeral=True
            )
        except discord.DiscordException:
            await interaction.response.send_message(
                "Could not register your entry.", ephemeral=True
            )


# ============================================================
# Bot
# ============================================================

class PrimeCloudBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=PREFIX,
            intents=INTENTS,
            case_insensitive=True,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=True, everyone=False
            ),
        )
        self.synced = False

    async def setup_hook(self):
        # Persistent component views: they survive bot restarts.
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        self.add_view(GiveawayView())

        # Sync slash commands.
        await self.tree.sync()
        self.tree.on_error = self._on_tree_error
        self.synced = True

        if not self.giveaway_worker.is_running():
            self.giveaway_worker.start()

    async def on_ready(self):
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Game(name="🎮 /help |prime.gg"),
        )
        print("💛 PRIME CLOUD BOT IS ONLINE")
        print(f"Logged in as: {self.user} ({self.user.id})")
        print("Prefix: !")
        print("Slash commands synced")
        print("Status: 🎮 Playing /help |prime.gg")

    async def on_member_join(self, member: discord.Member):
        settings = db.settings(member.guild.id)

        channel = await get_channel(member.guild, settings["welcome_channel_id"])
        if isinstance(channel, discord.TextChannel):
            message = (
                settings["welcome_message"]
                or "Welcome {user} to {server}! 💛"
            )
            message = (
                message.replace("{user}", member.mention)
                .replace("{server}", member.guild.name)
            )
            try:
                await channel.send(message)
            except discord.DiscordException:
                pass

        role = member.guild.get_role(settings["autorole_id"]) if settings["autorole_id"] else None
        if role and bot_has(member.guild, "manage_roles"):
            me = member.guild.me
            if me and role < me.top_role:
                try:
                    await member.add_roles(role, reason="PrimeCloud autorole")
                except discord.DiscordException:
                    pass

        await log_event(
            member.guild,
            "Member Joined",
            f"👋 {member.mention} joined the server.",
            GREEN,
        )

    async def on_member_remove(self, member: discord.Member):
        settings = db.settings(member.guild.id)
        channel = await get_channel(member.guild, settings["goodbye_channel_id"])
        if isinstance(channel, discord.TextChannel):
            message = (
                settings["goodbye_message"]
                or "{user} has left {server}. 👋"
            )
            message = (
                message.replace("{user}", str(member))
                .replace("{server}", member.guild.name)
            )
            try:
                await channel.send(message)
            except discord.DiscordException:
                pass

        await log_event(
            member.guild,
            "Member Left",
            f"👋 {member} left the server.",
            RED,
        )

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if message.guild:
            row = db.trigger_get(message.guild.id, message.content.strip())
            if row:
                try:
                    await message.channel.send(row["response"])
                except discord.DiscordException:
                    pass

        await self.process_commands(message)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            return await ctx.send(
                embed=embed(
                    "Permission Denied",
                    "You do not have the required Discord permission for this command.",
                    RED,
                )
            )
        if isinstance(error, commands.NoPrivateMessage):
            return await ctx.send("This command can only be used in a server.")
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(f"Missing argument: `{error.param.name}`.")
        print(f"[Prefix Error] {ctx.command}: {error}")
        traceback.print_exc()
        try:
            await ctx.send(
                embed=embed("Command Error", "An unexpected error occurred.", RED)
            )
        except discord.DiscordException:
            pass

    async def _on_tree_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        print(f"[Slash Error] {interaction.command}: {error}")
        traceback.print_exception(type(error), error, error.__traceback__)

        message = "An unexpected error occurred."
        if isinstance(error, app_commands.MissingPermissions):
            message = "You do not have the required Discord permission."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You are not allowed to use this command."
        elif isinstance(error, app_commands.TransformerError):
            message = "One of the supplied values is invalid."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=embed("Command Error", message, RED), ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    embed=embed("Command Error", message, RED), ephemeral=True
                )
        except discord.DiscordException:
            pass

    @tasks.loop(seconds=10)
    async def giveaway_worker(self):
        await self.wait_until_ready()

        for row in db.giveaway_active():
            if row["ends_at"] > unix_now():
                continue

            db.giveaway_end(row["message_id"])
            guild = self.get_guild(row["guild_id"])
            if not guild:
                continue

            channel = guild.get_channel(row["channel_id"])
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                message = await channel.fetch_message(row["message_id"])
                reaction = next(
                    (r for r in message.reactions if str(r.emoji) == "🎉"), None
                )
                entrants = []
                if reaction:
                    entrants = [
                        u async for u in reaction.users() if not u.bot
                    ]

                winner_count = min(row["winners"], len(entrants))
                winners = random.sample(entrants, winner_count) if winner_count else []

                if winners:
                    mentions = ", ".join(user.mention for user in winners)
                    await channel.send(
                        embed=embed(
                            "Giveaway Ended",
                            f"🎁 **Prize:** {row['prize']}\n\n"
                            f"🏆 **Winner(s):** {mentions}",
                            GREEN,
                        )
                    )
                else:
                    await channel.send(
                        embed=embed(
                            "Giveaway Ended",
                            f"🎁 **Prize:** {row['prize']}\n\nNo valid entrants.",
                            RED,
                        )
                    )

                await log_event(
                    guild,
                    "Giveaway Ended",
                    f"🎁 Prize: **{row['prize']}**\n"
                    f"🏆 Winners: {', '.join(u.mention for u in winners) if winners else 'None'}",
                )
            except discord.DiscordException as exc:
                print(f"[Giveaway Error] {exc}")

    @giveaway_worker.before_loop
    async def giveaway_before(self):
        await self.wait_until_ready()


bot = PrimeCloudBot()


# ============================================================
# /help + !help
# ============================================================

class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        select = discord.ui.Select(
            placeholder="Choose a help category",
            options=[
                discord.SelectOption(label="Tickets", emoji="🎫", value="tickets"),
                discord.SelectOption(label="Management", emoji="📢", value="management"),
                discord.SelectOption(label="Welcome & Goodbye", emoji="👋", value="welcome"),
                discord.SelectOption(label="Triggers", emoji="⚡", value="triggers"),
                discord.SelectOption(label="Moderation", emoji="🛡️", value="moderation"),
                discord.SelectOption(label="Server", emoji="🎭", value="server"),
                discord.SelectOption(label="Reviews", emoji="⭐", value="reviews"),
                discord.SelectOption(label="Giveaways", emoji="🎁", value="giveaways"),
                discord.SelectOption(label="PrimeCloud", emoji="💛", value="prime"),
                discord.SelectOption(label="Setup", emoji="⚙️", value="setup"),
            ],
        )
        select.callback = self.select_category
        self.add_item(select)

    async def select_category(self, interaction: discord.Interaction):
        key = interaction.data["values"][0]
        descriptions = {
            "tickets": "`/ticket setup` • `/ticket default`",
            "management": "`/announce` • `/setup logs`",
            "welcome": "`/setup welcome` • `/setup goodbye` • `/autorole`",
            "triggers": "`/trigger add` • `/trigger remove` • `/trigger list`",
            "moderation": "`/kick` • `/ban` • `/timeout` • `/warn` • `/purge`",
            "server": "`/serverinfo` • `/userinfo`",
            "reviews": "`/review`",
            "giveaways": "`/giveaway start`",
            "prime": "`/plans` • `/links`",
            "setup": "`/ticket setup` • `/setup welcome` • `/setup goodbye` • `/setup logs`",
        }
        await interaction.response.send_message(
            embed=embed(f"{key.title()} Commands", descriptions[key]),
            ephemeral=True,
        )


@bot.tree.command(name="help", description="Show the PrimeCloud help menu.")
async def help_slash(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=embed(
            "PRIME CLOUD • Help",
            "Select a category below to view the available commands.",
        ),
        view=HelpView(),
    )


@bot.command(name="help")
async def help_prefix(ctx: commands.Context):
    await ctx.send(
        embed=embed(
            "PRIME CLOUD • Help",
            "Select a category below to view the available commands.",
        ),
        view=HelpView(),
    )


# ============================================================
# Ticket command group
# ============================================================

ticket_group = app_commands.Group(
    name="ticket", description="PrimeCloud ticket system."
)


@ticket_group.command(name="setup", description="Configure the ticket system.")
@app_commands.describe(
    category="Ticket category",
    staff_role="Staff role",
    panel_channel="Panel channel",
)
@app_commands.default_permissions(manage_channels=True)
async def ticket_setup(
    interaction: discord.Interaction,
    category: discord.CategoryChannel,
    staff_role: discord.Role,
    panel_channel: discord.TextChannel,
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "manage_channels"
    ):
        return await interaction.response.send_message(
            "You need Manage Channels or Administrator.", ephemeral=True
        )

    if interaction.guild.me and staff_role >= interaction.guild.me.top_role:
        return await interaction.response.send_message(
            "The staff role must be below my highest role.", ephemeral=True
        )

    if not bot_has(interaction.guild, "manage_channels"):
        return await interaction.response.send_message(
            "I need Manage Channels permission.", ephemeral=True
        )

    db.set_setting(interaction.guild.id, "ticket_category_id", category.id)
    db.set_setting(interaction.guild.id, "staff_role_id", staff_role.id)
    db.set_setting(interaction.guild.id, "ticket_panel_channel_id", panel_channel.id)

    await interaction.response.send_message(
        embed=embed(
            "Ticket Setup Saved",
            f"📂 Category: {category.mention}\n"
            f"👥 Staff: {staff_role.mention}\n"
            f"📌 Panel: {panel_channel.mention}",
        )
    )


@ticket_group.command(name="default", description="Send the default PrimeCloud ticket panel.")
@app_commands.default_permissions(manage_channels=True)
async def ticket_default(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "manage_channels"
    ):
        return await interaction.response.send_message(
            "You need Manage Channels or Administrator.", ephemeral=True
        )

    settings = db.settings(interaction.guild.id)
    channel = await get_channel(
        interaction.guild, settings["ticket_panel_channel_id"]
    )
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message(
            "Run `/ticket setup` first.", ephemeral=True
        )

    e = embed(
        "Create a Ticket",
        "Select a category...\n\n"
        "▼ **Select Ticket Type**\n\n"
        "🛒 Purchase / Sales\n"
        "🛠️ Technical Support\n"
        "💳 Payment Support\n"
        "🤝 Partnership\n"
        "🚨 Report\n"
        "❓ General Support",
    )
    await channel.send(embed=e, view=TicketPanelView())
    await interaction.response.send_message(
        f"Ticket panel sent to {channel.mention}.", ephemeral=True
    )


bot.tree.add_command(ticket_group)


# ============================================================
# Setup group
# ============================================================

setup_group = app_commands.Group(
    name="setup", description="Configure PrimeCloud server systems."
)


@setup_group.command(name="welcome", description="Configure welcome messages.")
@app_commands.describe(
    channel="Welcome channel",
    message="Message; supports {user} and {server}",
)
@app_commands.default_permissions(manage_guild=True)
async def setup_welcome(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message: str = "Welcome {user} to {server}! 💛",
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "manage_guild"
    ):
        return await interaction.response.send_message(
            "You need Manage Server or Administrator.", ephemeral=True
        )
    db.set_setting(interaction.guild.id, "welcome_channel_id", channel.id)
    db.set_setting(interaction.guild.id, "welcome_message", message)
    await interaction.response.send_message(
        embed=embed(
            "Welcome Setup Saved",
            f"📌 {channel.mention}\n💬 {message}",
        )
    )


@setup_group.command(name="goodbye", description="Configure goodbye messages.")
@app_commands.describe(
    channel="Goodbye channel",
    message="Message; supports {user} and {server}",
)
@app_commands.default_permissions(manage_guild=True)
async def setup_goodbye(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message: str = "{user} has left {server}. 👋",
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "manage_guild"
    ):
        return await interaction.response.send_message(
            "You need Manage Server or Administrator.", ephemeral=True
        )
    db.set_setting(interaction.guild.id, "goodbye_channel_id", channel.id)
    db.set_setting(interaction.guild.id, "goodbye_message", message)
    await interaction.response.send_message(
        embed=embed(
            "Goodbye Setup Saved",
            f"📌 {channel.mention}\n💬 {message}",
        )
    )


@setup_group.command(name="logs", description="Configure the event log channel.")
@app_commands.describe(channel="Log channel")
@app_commands.default_permissions(manage_guild=True)
async def setup_logs(
    interaction: discord.Interaction, channel: discord.TextChannel
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "manage_guild"
    ):
        return await interaction.response.send_message(
            "You need Manage Server or Administrator.", ephemeral=True
        )
    db.set_setting(interaction.guild.id, "log_channel_id", channel.id)
    await interaction.response.send_message(
        embed=embed("Log Setup Saved", f"Logs will be sent to {channel.mention}.")
    )


bot.tree.add_command(setup_group)


# ============================================================
# Triggers
# ============================================================

trigger_group = app_commands.Group(
    name="trigger", description="Manage exact-message triggers."
)


@trigger_group.command(name="add", description="Add or replace a trigger.")
@app_commands.describe(trigger="Exact message", response="Response")
@app_commands.default_permissions(manage_guild=True)
async def trigger_add(
    interaction: discord.Interaction, trigger: str, response: str
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "manage_guild"
    ):
        return await interaction.response.send_message(
            "You need Manage Server or Administrator.", ephemeral=True
        )

    trigger = trigger.strip()
    if not trigger or len(trigger) > 100:
        return await interaction.response.send_message(
            "Trigger must be 1–100 characters.", ephemeral=True
        )

    db.trigger_add(interaction.guild.id, trigger, response)
    await interaction.response.send_message(
        embed=embed(
            "Trigger Saved",
            f"**Trigger:** `{trigger}`\n**Response:** {response}",
        )
    )


@trigger_group.command(name="remove", description="Remove a trigger.")
@app_commands.describe(trigger="Exact trigger")
@app_commands.default_permissions(manage_guild=True)
async def trigger_remove(
    interaction: discord.Interaction, trigger: str
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "manage_guild"
    ):
        return await interaction.response.send_message(
            "You need Manage Server or Administrator.", ephemeral=True
        )

    removed = db.trigger_remove(interaction.guild.id, trigger)
    await interaction.response.send_message(
        "Trigger removed." if removed else "That trigger does not exist.",
        ephemeral=True,
    )


@trigger_group.command(name="list", description="List all triggers.")
@app_commands.default_permissions(manage_guild=True)
async def trigger_list(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "manage_guild"
    ):
        return await interaction.response.send_message(
            "You need Manage Server or Administrator.", ephemeral=True
        )

    rows = db.trigger_list(interaction.guild.id)
    if not rows:
        return await interaction.response.send_message(
            "No triggers configured.", ephemeral=True
        )

    text = "\n".join(
        f"• `{r['trigger']}` → {r['response'][:120]}" for r in rows
    )
    await interaction.response.send_message(
        embed=embed("Custom Triggers", text), ephemeral=True
    )


bot.tree.add_command(trigger_group)


# ============================================================
# Review
# ============================================================

@bot.tree.command(name="review", description="Submit a customer review.")
@app_commands.describe(
    message="Your review",
    screenshot="Optional screenshot attachment",
)
async def review(
    interaction: discord.Interaction,
    message: app_commands.Range[str, 1, 1000],
    screenshot: Optional[discord.Attachment] = None,
):
    if screenshot and not (screenshot.content_type or "").startswith("image/"):
        return await interaction.response.send_message(
            "The screenshot must be an image attachment.", ephemeral=True
        )

    await interaction.response.send_message(
        embed=embed(
            "Customer Review",
            "Thank you for supporting PRIME CLOUD 💛\n\n"
            "Please select your rating from 1 to 5 stars.",
        ),
        view=ReviewView(message, screenshot.url if screenshot else None),
        ephemeral=True,
    )


# ============================================================
# Announcement
# ============================================================

@bot.tree.command(name="announce", description="Send a PrimeCloud announcement.")
@app_commands.describe(
    channel="Announcement channel",
    title="Announcement title",
    message="Announcement message",
)
@app_commands.default_permissions(manage_messages=True)
async def announce(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    message: str,
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "manage_messages"
    ):
        return await interaction.response.send_message(
            "You need Manage Messages or Administrator.", ephemeral=True
        )

    if not channel.permissions_for(interaction.guild.me).send_messages:
        return await interaction.response.send_message(
            "I cannot send messages in that channel.", ephemeral=True
        )

    e = embed(title[:256], message)
    e.set_author(
        name=f"{interaction.user.display_name}",
        icon_url=interaction.user.display_avatar.url,
    )

    try:
        await channel.send(embed=e)
        await log_event(
            interaction.guild,
            "Announcement Sent",
            f"📢 {interaction.user.mention} → {channel.mention}\n**{title}**",
        )
        await interaction.response.send_message(
            f"Announcement sent to {channel.mention}.", ephemeral=True
        )
    except discord.DiscordException as exc:
        await interaction.response.send_message(
            f"Could not send announcement: `{exc}`", ephemeral=True
        )


# ============================================================
# Autorole
# ============================================================

@bot.tree.command(name="autorole", description="Set the role new members receive.")
@app_commands.describe(role="Role to assign to new members")
@app_commands.default_permissions(manage_roles=True)
async def autorole(
    interaction: discord.Interaction, role: discord.Role
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "manage_roles"
    ):
        return await interaction.response.send_message(
            "You need Manage Roles or Administrator.", ephemeral=True
        )

    me = interaction.guild.me
    if me and role >= me.top_role:
        return await interaction.response.send_message(
            "That role is above or equal to my highest role. Move my bot role higher.",
            ephemeral=True,
        )

    db.set_setting(interaction.guild.id, "autorole_id", role.id)
    await interaction.response.send_message(
        embed=embed("Autorole Saved", f"New members will receive {role.mention}.")
    )


# ============================================================
# Moderation
# ============================================================

def valid_target(actor: discord.Member, target: discord.Member, bot_member: discord.Member):
    if target.id == actor.id:
        return False, "You cannot target yourself."
    if target.id == bot_member.id:
        return False, "You cannot target the bot."
    if target.top_role >= actor.top_role and not actor.guild_permissions.administrator:
        return False, "You cannot target a member with an equal or higher role."
    if target.top_role >= bot_member.top_role:
        return False, "My highest role must be above the target."
    return True, ""


@bot.tree.command(name="kick", description="Kick a member.")
@app_commands.describe(member="Member to kick", reason="Reason")
@app_commands.default_permissions(kick_members=True)
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided.",
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "kick_members"
    ):
        return await interaction.response.send_message(
            "You need Kick Members or Administrator.", ephemeral=True
        )

    ok, why = valid_target(interaction.user, member, interaction.guild.me)
    if not ok:
        return await interaction.response.send_message(why, ephemeral=True)

    try:
        await member.kick(reason=reason_text(reason))
        await interaction.response.send_message(
            embed=embed(
                "Member Kicked",
                f"👤 {member.mention}\n📝 {reason_text(reason)}",
                GREEN,
            )
        )
        await log_event(
            interaction.guild,
            "Moderation • Kick",
            f"👤 {member} kicked by {interaction.user.mention}\n"
            f"📝 {reason_text(reason)}",
            RED,
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Discord denied the kick. Check role hierarchy and permissions.",
            ephemeral=True,
        )


@bot.tree.command(name="ban", description="Ban a member.")
@app_commands.describe(member="Member to ban", reason="Reason")
@app_commands.default_permissions(ban_members=True)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided.",
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "ban_members"
    ):
        return await interaction.response.send_message(
            "You need Ban Members or Administrator.", ephemeral=True
        )

    ok, why = valid_target(interaction.user, member, interaction.guild.me)
    if not ok:
        return await interaction.response.send_message(why, ephemeral=True)

    try:
        await member.ban(reason=reason_text(reason))
        await interaction.response.send_message(
            embed=embed(
                "Member Banned",
                f"👤 {member.mention}\n📝 {reason_text(reason)}",
                GREEN,
            )
        )
        await log_event(
            interaction.guild,
            "Moderation • Ban",
            f"👤 {member} banned by {interaction.user.mention}\n"
            f"📝 {reason_text(reason)}",
            RED,
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Discord denied the ban. Check role hierarchy and permissions.",
            ephemeral=True,
        )


@bot.tree.command(name="timeout", description="Timeout a member.")
@app_commands.describe(
    member="Member to timeout",
    minutes="Timeout length in minutes",
    reason="Reason",
)
@app_commands.default_permissions(moderate_members=True)
async def timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: str = "No reason provided.",
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "moderate_members"
    ):
        return await interaction.response.send_message(
            "You need Moderate Members or Administrator.", ephemeral=True
        )

    ok, why = valid_target(interaction.user, member, interaction.guild.me)
    if not ok:
        return await interaction.response.send_message(why, ephemeral=True)

    try:
        await member.timeout(
            timedelta(minutes=minutes),
            reason=reason_text(reason),
        )
        await interaction.response.send_message(
            embed=embed(
                "Member Timed Out",
                f"👤 {member.mention}\n⏱️ {minutes} minutes\n"
                f"📝 {reason_text(reason)}",
                GREEN,
            )
        )
        await log_event(
            interaction.guild,
            "Moderation • Timeout",
            f"👤 {member} timed out by {interaction.user.mention}\n"
            f"⏱️ {minutes} minutes\n📝 {reason_text(reason)}",
            RED,
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Discord denied the timeout. Check role hierarchy and permissions.",
            ephemeral=True,
        )


@bot.tree.command(name="warn", description="Warn a member.")
@app_commands.describe(member="Member to warn", reason="Reason")
@app_commands.default_permissions(moderate_members=True)
async def warn(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided.",
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "moderate_members"
    ):
        return await interaction.response.send_message(
            "You need Moderate Members or Administrator.", ephemeral=True
        )

    if member.id == interaction.user.id:
        return await interaction.response.send_message(
            "You cannot warn yourself.", ephemeral=True
        )

    db.warn_add(
        interaction.guild.id,
        member.id,
        interaction.user.id,
        reason_text(reason),
    )

    try:
        await member.send(
            embed=embed(
                "You Have Been Warned",
                f"Server: **{interaction.guild.name}**\n"
                f"📝 {reason_text(reason)}",
                RED,
            )
        )
    except discord.DiscordException:
        pass

    await interaction.response.send_message(
        embed=embed(
            "Member Warned",
            f"👤 {member.mention}\n📝 {reason_text(reason)}",
            GREEN,
        )
    )
    await log_event(
        interaction.guild,
        "Moderation • Warn",
        f"👤 {member.mention} warned by {interaction.user.mention}\n"
        f"📝 {reason_text(reason)}",
        RED,
    )


@bot.tree.command(name="purge", description="Delete recent messages.")
@app_commands.describe(amount="Number of messages to delete")
@app_commands.default_permissions(manage_messages=True)
async def purge(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 100],
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "manage_messages"
    ):
        return await interaction.response.send_message(
            "You need Manage Messages or Administrator.", ephemeral=True
        )

    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message(
            "This command requires a text channel.", ephemeral=True
        )

    if not bot_has(interaction.guild, "manage_messages"):
        return await interaction.response.send_message(
            "I need Manage Messages permission.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(
            limit=amount,
            reason=f"Purged by {interaction.user}",
        )
        await interaction.followup.send(
            embed=embed(
                "Messages Purged",
                f"🗑️ Deleted **{len(deleted)}** messages.",
                GREEN,
            ),
            ephemeral=True,
        )
        await log_event(
            interaction.guild,
            "Moderation • Purge",
            f"🗑️ {len(deleted)} messages deleted by {interaction.user.mention}\n"
            f"📌 {interaction.channel.mention}",
            RED,
        )
    except discord.DiscordException as exc:
        await interaction.followup.send(
            f"Could not purge messages: `{exc}`", ephemeral=True
        )


# ============================================================
# Server information
# ============================================================

@bot.tree.command(name="serverinfo", description="Show server information.")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        return await interaction.response.send_message(
            "Server only.", ephemeral=True
        )

    e = embed("Server Information")
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    e.add_field(name="🏠 Name", value=guild.name, inline=True)
    e.add_field(name="🆔 ID", value=str(guild.id), inline=True)
    e.add_field(name="👥 Members", value=str(guild.member_count), inline=True)
    e.add_field(name="💬 Channels", value=str(len(guild.channels)), inline=True)
    e.add_field(name="🎭 Roles", value=str(len(guild.roles)), inline=True)
    e.add_field(name="👑 Owner", value=f"<@{guild.owner_id}>", inline=True)
    e.add_field(
        name="📅 Created",
        value=discord.utils.format_dt(guild.created_at, "F"),
        inline=False,
    )
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="userinfo", description="Show user information.")
@app_commands.describe(member="Member to inspect")
async def userinfo(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
):
    member = member or interaction.user
    e = embed("User Information")
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="👤 User", value=member.mention, inline=True)
    e.add_field(name="🆔 ID", value=str(member.id), inline=True)
    e.add_field(name="🎭 Top Role", value=member.top_role.mention, inline=True)
    e.add_field(
        name="📅 Joined",
        value=discord.utils.format_dt(member.joined_at, "F") if member.joined_at else "Unknown",
        inline=False,
    )
    e.add_field(
        name="📅 Account Created",
        value=discord.utils.format_dt(member.created_at, "F"),
        inline=False,
    )
    await interaction.response.send_message(embed=e)


# ============================================================
# Giveaways
# ============================================================

giveaway_group = app_commands.Group(
    name="giveaway", description="PrimeCloud giveaways."
)


@giveaway_group.command(name="start", description="Start a giveaway.")
@app_commands.describe(
    duration="Duration: 30s, 10m, 2h, 1d",
    prize="Giveaway prize",
    winners="Number of winners",
)
@app_commands.default_permissions(manage_messages=True)
async def giveaway_start(
    interaction: discord.Interaction,
    duration: str,
    prize: str,
    winners: app_commands.Range[int, 1, 20],
):
    if not isinstance(interaction.user, discord.Member) or not member_has(
        interaction.user, "manage_messages"
    ):
        return await interaction.response.send_message(
            "You need Manage Messages or Administrator.", ephemeral=True
        )

    seconds = parse_duration(duration)
    if not seconds or seconds > 30 * 86400:
        return await interaction.response.send_message(
            "Invalid duration. Use `30s`, `10m`, `2h`, `1d`, etc. Maximum is 30 days.",
            ephemeral=True,
        )

    ends_at = unix_now() + seconds
    e = embed(
        "Giveaway",
        f"🎁 **Prize:** {prize}\n"
        f"🏆 **Winners:** {winners}\n"
        f"⏰ **Ends:** <t:{ends_at}:R>\n\n"
        "Click **Join Giveaway** to enter!",
    )
    e.set_author(
        name=f"Hosted by {interaction.user.display_name}",
        icon_url=interaction.user.display_avatar.url,
    )

    await interaction.response.send_message(
        embed=e,
        view=GiveawayView(),
    )
    message = await interaction.original_response()
    db.giveaway_add(
        message.id,
        interaction.guild.id,
        interaction.channel.id,
        prize,
        winners,
        ends_at,
    )
    await log_event(
        interaction.guild,
        "Giveaway Started",
        f"🎁 Prize: **{prize}**\n"
        f"🏆 Winners: {winners}\n"
        f"⏰ Ends: <t:{ends_at}:F>",
    )


bot.tree.add_command(giveaway_group)


# ============================================================
# PrimeCloud plans + links
# ============================================================

PLANS = [
    ("DIRT", "₹49/mo", "2GB RAM • 1 CPU • 10GB NVMe • 1 Backup"),
    ("WOOD", "₹89/mo", "4GB RAM • 2 CPU • 20GB NVMe • 2 Backups"),
    ("COAL", "₹129/mo", "6GB RAM • 2.5 CPU • 30GB NVMe • 2 Backups"),
    ("IRON", "₹169/mo", "8GB RAM • 3 CPU • 40GB NVMe • 3 Backups"),
    ("GOLD", "₹249/mo", "12GB RAM • 4 CPU • 60GB NVMe • 3 Backups"),
    ("DIAMOND", "₹329/mo", "16GB RAM • 5 CPU • 80GB NVMe • 4 Backups"),
    ("EMERALD", "₹499/mo", "24GB RAM • 6 CPU • 120GB NVMe • 4 Backups"),
]


@bot.tree.command(name="plans", description="Show PrimeCloud Minecraft hosting plans.")
async def plans(interaction: discord.Interaction):
    e = embed("PrimeCloud Minecraft Hosting", "Choose the plan that fits your server.")
    for name, price, specs in PLANS:
        e.add_field(
            name=f"💛 {name} — {price}",
            value=specs,
            inline=False,
        )
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="links", description="Show official PrimeCloud links.")
async def links(interaction: discord.Interaction):
    e = embed("PrimeCloud Links")
    e.add_field(
        name="🖥️ Paid Panel",
        value="https://panel.primescloud.dpdns.org/",
        inline=False,
    )
    e.add_field(
        name="🆓 Free Panel",
        value="https://gp.asknodesxprimecloud.dpdns.org/",
        inline=False,
    )
    e.add_field(
        name="💳 Billing",
        value="https://billing.primescloud.dpdns.org/",
        inline=False,
    )
    e.add_field(
        name="🌐 Website",
        value="https://www.primescloud.dpdns.org",
        inline=False,
    )
    await interaction.response.send_message(embed=e)


# ============================================================
# Prefix commands for key public commands
# ============================================================

@bot.command(name="plans")
async def plans_prefix(ctx: commands.Context):
    e = embed("PrimeCloud Minecraft Hosting", "Current plans:")
    for name, price, specs in PLANS:
        e.add_field(name=f"💛 {name} — {price}", value=specs, inline=False)
    await ctx.send(embed=e)


@bot.command(name="links")
async def links_prefix(ctx: commands.Context):
    e = embed(
        "PrimeCloud Links",
        "🖥️ Paid Panel: https://panel.primescloud.dpdns.org/\n"
        "🆓 Free Panel: https://gp.asknodesxprimecloud.dpdns.org/\n"
        "💳 Billing: https://billing.primescloud.dpdns.org/\n"
        "🌐 Website: https://www.primescloud.dpdns.org",
    )
    await ctx.send(embed=e)


# ============================================================
# Prefix moderation/config compatibility commands
# ============================================================

@bot.command(name="purge")
@commands.guild_only()
@commands.bot_has_permissions(manage_messages=True)
async def purge_prefix(ctx: commands.Context, amount: int):
    if not isinstance(ctx.author, discord.Member) or not member_has(
        ctx.author, "manage_messages"
    ):
        return await ctx.send("You need Manage Messages or Administrator.")
    if not isinstance(ctx.channel, discord.TextChannel):
        return
    if amount < 1 or amount > 100:
        return await ctx.send("Amount must be between 1 and 100.")
    deleted = await ctx.channel.purge(
        limit=amount, reason=f"Purged by {ctx.author}"
    )
    await ctx.send(f"🗑️ Deleted {len(deleted)} messages.", delete_after=5)


# ============================================================
# Global error logging
# ============================================================

async def global_error_log(message: str):
    print(message)
    traceback.print_exc()


# ============================================================
# Start
# ============================================================

if __name__ == "__main__":
    if not TOKEN or TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        print("ERROR: Paste your Discord bot token into TOKEN in bot.py first.")
    else:
        try:
            bot.run(TOKEN, log_handler=None)
        except KeyboardInterrupt:
            print("PRIME CLOUD BOT stopped.")
        except Exception:
            print("PRIME CLOUD BOT stopped because of an unexpected error:")
            traceback.print_exc()
