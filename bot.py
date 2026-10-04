import os
import sqlite3
import asyncio
import discord
from discord import app_commands
from discord.ext import commands


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "house_storage.db"
)

# Your Discord server ID
GUILD_ID = 1550035184756195328

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable is missing."
    )


# ============================================================
# DATABASE
# ============================================================

database = sqlite3.connect(DATABASE_PATH)
cursor = database.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS storage (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    amount INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, item)
)
""")

# Add the new access column to existing databases without deleting old deposits.
cursor.execute("PRAGMA table_info(storage)")
storage_columns = {row[1] for row in cursor.fetchall()}

if "access_type" not in storage_columns:
    cursor.execute(
        "ALTER TABLE storage "
        "ADD COLUMN access_type TEXT NOT NULL DEFAULT 'personal'"
    )

# Rebuild the table once so the same item can exist in both PERSONAL and SHARED storage.
cursor.execute("""
CREATE TABLE IF NOT EXISTS storage_v2 (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    amount INTEGER NOT NULL,
    access_type TEXT NOT NULL DEFAULT 'personal',
    PRIMARY KEY (guild_id, user_id, item, access_type)
)
""")

cursor.execute("""
INSERT OR IGNORE INTO storage_v2
(guild_id, user_id, item, amount, access_type)
SELECT guild_id, user_id, item, amount, access_type
FROM storage
""")

cursor.execute("DROP TABLE storage")
cursor.execute("ALTER TABLE storage_v2 RENAME TO storage")

cursor.execute("""
CREATE TABLE IF NOT EXISTS panels (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
)
""")

database.commit()

database_lock = asyncio.Lock()


# ============================================================
# BOT SETUP
# ============================================================

intents = discord.Intents.default()
intents.members = True


class StorageBot(commands.Bot):

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)

        try:
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)

            print("")
            print("=" * 55)
            print(f"Synced {len(synced)} commands to server {GUILD_ID}")

            for command in synced:
                option_names = [option.name for option in command.options]
                print(f" - /{command.name} options: {option_names}")

            print("=" * 55)
            print("")

        except Exception as error:
            print(f"Command sync failed: {repr(error)}")


bot = StorageBot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# READY
# ============================================================

@bot.event
async def on_ready():

    print("")
    print("=" * 55)
    print(f"Logged in as: {bot.user}")
    print(f"Bot ID: {bot.user.id}")
    print("House Storage Bot is ONLINE!")
    print("=" * 55)
    print("")


# ============================================================
# HELPERS
# ============================================================

def clean_item_name(item: str) -> str:

    return " ".join(
        item.strip().split()
    ).title()


async def get_member_name(
    guild: discord.Guild,
    user_id: int
) -> str:

    member = guild.get_member(user_id)

    if member:
        return member.display_name

    try:
        member = await guild.fetch_member(user_id)
        return member.display_name

    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException
    ):
        return f"Unknown User ({user_id})"


# ============================================================
# UPDATE PUBLIC STORAGE PANEL
# ============================================================

async def update_storage_panel(
    guild: discord.Guild
):

    # Find panel information.

    async with database_lock:

        cursor.execute(
            """
            SELECT channel_id, message_id
            FROM panels
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        panel_data = cursor.fetchone()


    if not panel_data:
        return


    channel_id, message_id = panel_data


    # Find channel.

    channel = guild.get_channel(channel_id)

    if channel is None:

        try:
            channel = await bot.fetch_channel(
                channel_id
            )

        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException
        ):
            return


    # Find panel message.

    try:

        message = await channel.fetch_message(
            message_id
        )

    except discord.NotFound:

        async with database_lock:

            cursor.execute(
                """
                DELETE FROM panels
                WHERE guild_id = ?
                """,
                (guild.id,)
            )

            database.commit()

        return

    except (
        discord.Forbidden,
        discord.HTTPException
    ):
        return


    # Get all storage for this server.

    async with database_lock:

        cursor.execute(
            """
            SELECT user_id, item, amount, access_type
            FROM storage
            WHERE guild_id = ?
            ORDER BY user_id ASC, access_type ASC, item ASC
            """,
            (guild.id,)
        )

        all_items = cursor.fetchall()


    # Build panel.

    embed = discord.Embed(
        title="🏠 HOUSE STORAGE",
        color=discord.Color.blue()
    )


    # Empty storage.

    if not all_items:

        embed.description = (
            "📭 **House storage is currently empty.**\n\n"
            "Use `/deposit` to add items."
        )

        embed.set_footer(
            text=(
                "Use /deposit to add items • "
                "/withdraw to remove items"
            )
        )

        try:
            await message.edit(embed=embed)

        except discord.HTTPException:
            pass

        return


    # Group storage by user.

    users = {}

    for user_id, item, amount, access_type in all_items:

        if user_id not in users:
            users[user_id] = []

        users[user_id].append(
            (item, amount, access_type)
        )


    # Add users to panel.

    for user_id, items in users.items():

        # Discord embeds allow 25 fields.
        if len(embed.fields) >= 25:
            break

        username = await get_member_name(
            guild,
            user_id
        )

        lines = []

        for item, amount, access_type in items:

            icon = "🔒" if access_type == "personal" else "🌐"
            label = "Personal" if access_type == "personal" else "Everyone"

            lines.append(
                f"{icon} **{item}** — `{amount:,}` • {label}"
            )

        item_text = "\n".join(lines)

        if len(item_text) > 1000:

            item_text = (
                item_text[:950]
                + "\n*More items not shown...*"
            )

        embed.add_field(
            name=f"👤 {username}",
            value=item_text,
            inline=False
        )


    embed.set_footer(
        text=(
            "Use /deposit to add items • "
            "/withdraw to remove items"
        )
    )


    try:
        await message.edit(embed=embed)

    except discord.HTTPException:
        pass


# ============================================================
# /DEPOSIT
# ============================================================

@bot.tree.command(
    name="deposit",
    description="Deposit an item into house storage."
)
@app_commands.describe(
    amount="Amount you are depositing",
    item="Item you are depositing",
    access="Who is allowed to use this deposit?"
)
@app_commands.choices(
    access=[
        app_commands.Choice(name="🔒 Personal use", value="personal"),
        app_commands.Choice(name="🌐 Everyone can use it", value="shared"),
    ]
)
async def deposit(
    interaction: discord.Interaction,
    amount: int,
    item: str,
    access: app_commands.Choice[str]
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    if amount <= 0:
        await interaction.response.send_message(
            "❌ Amount must be greater than 0.",
            ephemeral=True
        )
        return

    item = clean_item_name(item)
    access_type = access.value

    if not item:
        await interaction.response.send_message(
            "❌ You need to enter an item.",
            ephemeral=True
        )
        return

    if len(item) > 100:
        await interaction.response.send_message(
            "❌ Item names must be 100 characters or less.",
            ephemeral=True
        )
        return

    async with database_lock:
        cursor.execute(
            """
            SELECT amount
            FROM storage
            WHERE guild_id = ?
            AND user_id = ?
            AND item = ?
            AND access_type = ?
            """,
            (
                interaction.guild.id,
                interaction.user.id,
                item,
                access_type
            )
        )

        result = cursor.fetchone()

        if result:
            new_amount = result[0] + amount

            cursor.execute(
                """
                UPDATE storage
                SET amount = ?
                WHERE guild_id = ?
                AND user_id = ?
                AND item = ?
                AND access_type = ?
                """,
                (
                    new_amount,
                    interaction.guild.id,
                    interaction.user.id,
                    item,
                    access_type
                )
            )
        else:
            new_amount = amount

            cursor.execute(
                """
                INSERT INTO storage
                (guild_id, user_id, item, amount, access_type)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    interaction.guild.id,
                    interaction.user.id,
                    item,
                    amount,
                    access_type
                )
            )

        database.commit()

    access_text = (
        "🔒 Personal use"
        if access_type == "personal"
        else "🌐 Everyone can use it"
    )

    embed = discord.Embed(
        title="📦 Deposit Successful",
        color=discord.Color.green()
    )

    embed.add_field(
        name="Deposited",
        value=f"**{amount:,} × {item}**",
        inline=False
    )
    embed.add_field(
        name="Access",
        value=access_text,
        inline=False
    )
    embed.add_field(
        name="Total in this category",
        value=f"**{new_amount:,} × {item}**",
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

    await update_storage_panel(interaction.guild)


# ============================================================
# /WITHDRAW
# ============================================================

@bot.tree.command(
    name="withdraw",
    description="Withdraw an item you are allowed to use."
)
@app_commands.describe(
    amount="Amount you are withdrawing",
    item="Item you are withdrawing"
)
async def withdraw(
    interaction: discord.Interaction,
    amount: int,
    item: str
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    if amount <= 0:
        await interaction.response.send_message(
            "❌ Amount must be greater than 0.",
            ephemeral=True
        )
        return

    item = clean_item_name(item)

    if not item:
        await interaction.response.send_message(
            "❌ You need to enter an item.",
            ephemeral=True
        )
        return

    async with database_lock:
        cursor.execute(
            """
            SELECT amount
            FROM storage
            WHERE guild_id = ?
            AND user_id = ?
            AND item = ?
            AND access_type = 'personal'
            """,
            (interaction.guild.id, interaction.user.id, item)
        )
        personal_row = cursor.fetchone()
        personal_available = personal_row[0] if personal_row else 0

        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM storage
            WHERE guild_id = ?
            AND item = ?
            AND access_type = 'shared'
            """,
            (interaction.guild.id, item)
        )
        shared_available = cursor.fetchone()[0]
        total_available = personal_available + shared_available

        if total_available < amount:
            await interaction.response.send_message(
                (
                    f"❌ You can only access **{total_available:,} × {item}**.\n"
                    f"🔒 Yours: **{personal_available:,}**\n"
                    f"🌐 Shared: **{shared_available:,}**"
                ),
                ephemeral=True
            )
            return

        personal_used = min(amount, personal_available)
        shared_used = amount - personal_used

        if personal_used:
            personal_remaining = personal_available - personal_used

            if personal_remaining == 0:
                cursor.execute(
                    """
                    DELETE FROM storage
                    WHERE guild_id = ?
                    AND user_id = ?
                    AND item = ?
                    AND access_type = 'personal'
                    """,
                    (interaction.guild.id, interaction.user.id, item)
                )
            else:
                cursor.execute(
                    """
                    UPDATE storage
                    SET amount = ?
                    WHERE guild_id = ?
                    AND user_id = ?
                    AND item = ?
                    AND access_type = 'personal'
                    """,
                    (
                        personal_remaining,
                        interaction.guild.id,
                        interaction.user.id,
                        item
                    )
                )

        if shared_used:
            cursor.execute(
                """
                SELECT user_id, amount
                FROM storage
                WHERE guild_id = ?
                AND item = ?
                AND access_type = 'shared'
                ORDER BY rowid ASC
                """,
                (interaction.guild.id, item)
            )

            remaining_to_take = shared_used

            for owner_id, owner_amount in cursor.fetchall():
                if remaining_to_take <= 0:
                    break

                take = min(owner_amount, remaining_to_take)
                owner_remaining = owner_amount - take
                remaining_to_take -= take

                if owner_remaining == 0:
                    cursor.execute(
                        """
                        DELETE FROM storage
                        WHERE guild_id = ?
                        AND user_id = ?
                        AND item = ?
                        AND access_type = 'shared'
                        """,
                        (interaction.guild.id, owner_id, item)
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE storage
                        SET amount = ?
                        WHERE guild_id = ?
                        AND user_id = ?
                        AND item = ?
                        AND access_type = 'shared'
                        """,
                        (
                            owner_remaining,
                            interaction.guild.id,
                            owner_id,
                            item
                        )
                    )

        database.commit()

    embed = discord.Embed(
        title="📤 Withdrawal Successful",
        color=discord.Color.red()
    )
    embed.add_field(
        name="Withdrawn",
        value=f"**{amount:,} × {item}**",
        inline=False
    )

    if personal_used:
        embed.add_field(
            name="🔒 From Your Personal Storage",
            value=f"**{personal_used:,}**",
            inline=True
        )

    if shared_used:
        embed.add_field(
            name="🌐 From Shared Storage",
            value=f"**{shared_used:,}**",
            inline=True
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)
    await update_storage_panel(interaction.guild)


# ============================================================
# /STORAGE
# ============================================================

@bot.tree.command(
    name="storage",
    description="View your personal items and shared house storage."
)
async def storage(
    interaction: discord.Interaction
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    async with database_lock:
        cursor.execute(
            """
            SELECT item, amount
            FROM storage
            WHERE guild_id = ?
            AND user_id = ?
            AND access_type = 'personal'
            ORDER BY item ASC
            """,
            (
                interaction.guild.id,
                interaction.user.id
            )
        )
        personal_items = cursor.fetchall()

        cursor.execute(
            """
            SELECT item, SUM(amount)
            FROM storage
            WHERE guild_id = ?
            AND access_type = 'shared'
            GROUP BY item
            ORDER BY item ASC
            """,
            (interaction.guild.id,)
        )
        shared_items = cursor.fetchall()

    embed = discord.Embed(
        title="🏠 House Storage",
        color=discord.Color.blue()
    )

    if personal_items:
        personal_text = "\n".join(
            f"• **{item}** — `{amount:,}`"
            for item, amount in personal_items
        )
    else:
        personal_text = "📭 No personal items."

    if shared_items:
        shared_text = "\n".join(
            f"• **{item}** — `{amount:,}`"
            for item, amount in shared_items
        )
    else:
        shared_text = "📭 No shared items."

    embed.add_field(
        name="🔒 Your Personal Storage",
        value=personal_text[:1024],
        inline=False
    )
    embed.add_field(
        name="🌐 Everyone Can Use",
        value=shared_text[:1024],
        inline=False
    )

    embed.set_footer(
        text=f"Viewing storage as {interaction.user.display_name}"
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# /SETUPPANEL
# ============================================================

@bot.tree.command(
    name="setuppanel",
    description="Create the shared House Storage panel."
)
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def setuppanel(
    interaction: discord.Interaction
):

    if interaction.guild is None:

        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return


    # Look for an old panel.

    async with database_lock:

        cursor.execute(
            """
            SELECT channel_id, message_id
            FROM panels
            WHERE guild_id = ?
            """,
            (interaction.guild.id,)
        )

        old_panel = cursor.fetchone()


    # Delete old panel if possible.

    if old_panel:

        old_channel_id, old_message_id = old_panel

        old_channel = interaction.guild.get_channel(
            old_channel_id
        )

        if old_channel:

            try:

                old_message = (
                    await old_channel.fetch_message(
                        old_message_id
                    )
                )

                await old_message.delete()

            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException
            ):
                pass


    # Create panel.

    embed = discord.Embed(
        title="🏠 HOUSE STORAGE",
        description=(
            "📭 **House storage is currently empty.**\n\n"
            "Use `/deposit` to add items."
        ),
        color=discord.Color.blue()
    )

    embed.set_footer(
        text=(
            "Use /deposit to add items • "
            "/withdraw to remove items"
        )
    )


    await interaction.response.send_message(
        embed=embed
    )


    panel_message = (
        await interaction.original_response()
    )


    # Save panel location.

    async with database_lock:

        cursor.execute(
            """
            INSERT OR REPLACE INTO panels
            (
                guild_id,
                channel_id,
                message_id
            )
            VALUES (?, ?, ?)
            """,
            (
                interaction.guild.id,
                interaction.channel.id,
                panel_message.id
            )
        )

        database.commit()


    await update_storage_panel(
        interaction.guild
    )


# ============================================================
# /ADMINREMOVE
# ============================================================

@bot.tree.command(
    name="adminremove",
    description="Admin: Remove a user's personal or shared deposit."
)
@app_commands.describe(
    user="User whose deposit you want to modify",
    amount="Amount you want to remove",
    item="Item you want to remove",
    access="Remove their personal or shared deposit?"
)
@app_commands.choices(
    access=[
        app_commands.Choice(name="🔒 Personal", value="personal"),
        app_commands.Choice(name="🌐 Shared", value="shared"),
    ]
)
@app_commands.checks.has_permissions(administrator=True)
async def adminremove(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
    item: str,
    access: app_commands.Choice[str]
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    if amount <= 0:
        await interaction.response.send_message(
            "❌ Amount must be greater than 0.",
            ephemeral=True
        )
        return

    item = clean_item_name(item)
    access_type = access.value

    if not item:
        await interaction.response.send_message(
            "❌ You need to enter an item.",
            ephemeral=True
        )
        return

    async with database_lock:
        cursor.execute(
            """
            SELECT amount
            FROM storage
            WHERE guild_id = ?
            AND user_id = ?
            AND item = ?
            AND access_type = ?
            """,
            (
                interaction.guild.id,
                user.id,
                item,
                access_type
            )
        )

        result = cursor.fetchone()

        if not result:
            await interaction.response.send_message(
                f"❌ **{user.display_name}** doesn't have **{item}** "
                f"in that storage category.",
                ephemeral=True
            )
            return

        current_amount = result[0]

        if amount > current_amount:
            await interaction.response.send_message(
                f"❌ **{user.display_name}** only has "
                f"**{current_amount:,} × {item}** in that category.",
                ephemeral=True
            )
            return

        new_amount = current_amount - amount

        if new_amount == 0:
            cursor.execute(
                """
                DELETE FROM storage
                WHERE guild_id = ?
                AND user_id = ?
                AND item = ?
                AND access_type = ?
                """,
                (
                    interaction.guild.id,
                    user.id,
                    item,
                    access_type
                )
            )
        else:
            cursor.execute(
                """
                UPDATE storage
                SET amount = ?
                WHERE guild_id = ?
                AND user_id = ?
                AND item = ?
                AND access_type = ?
                """,
                (
                    new_amount,
                    interaction.guild.id,
                    user.id,
                    item,
                    access_type
                )
            )

        database.commit()

    category = "🔒 Personal" if access_type == "personal" else "🌐 Shared"

    embed = discord.Embed(
        title="🛡️ Admin Storage Adjustment",
        color=discord.Color.orange()
    )
    embed.add_field(name="User", value=user.mention, inline=False)
    embed.add_field(name="Category", value=category, inline=False)
    embed.add_field(
        name="Removed",
        value=f"**{amount:,} × {item}**",
        inline=False
    )
    embed.add_field(
        name="Remaining",
        value=f"**{new_amount:,} × {item}**",
        inline=False
    )
    embed.set_footer(
        text=f"Removed by {interaction.user.display_name}"
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

    await update_storage_panel(interaction.guild)


# ============================================================
# ERROR HANDLER
# ============================================================

@bot.tree.error
async def command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    if isinstance(
        error,
        app_commands.MissingPermissions
    ):

        message = (
            "❌ You don't have permission "
            "to use that command."
        )

    else:

        print(
            f"Slash command error: {repr(error)}"
        )

        message = (
            "❌ Something went wrong while "
            "running that command."
        )


    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                message,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                message,
                ephemeral=True
            )

    except discord.HTTPException:
        pass


# ============================================================
# START BOT
# ============================================================

print("Starting House Storage Bot...")

bot.run(TOKEN)