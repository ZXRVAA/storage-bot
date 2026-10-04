import os
import sqlite3
import asyncio
import discord
from discord import app_commands
from discord.ext import commands


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "house_storage.db"
)

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable is missing."
    )


# ============================================================
# DATABASE
# ============================================================

database = sqlite3.connect(DATABASE_PATH)

cursor = database.cursor()


# ------------------------------------------------------------
# STORAGE TABLE
#
# Every item belongs to:
#
# Discord Server
#     ↓
# Discord User
#     ↓
# Item
#     ↓
# Amount
# ------------------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS storage (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    amount INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, item)
)
""")


# ------------------------------------------------------------
# SHARED PANEL TABLE
#
# Remembers where each server's public panel is located.
# ------------------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS panels (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
)
""")


database.commit()


# Prevent multiple commands from changing
# the database simultaneously.
database_lock = asyncio.Lock()


# ============================================================
# BOT SETUP
# ============================================================

intents = discord.Intents.default()

# Required for resolving Discord members/display names.
intents.members = True


class StorageBot(commands.Bot):

    async def setup_hook(self):

        try:

            synced = await self.tree.sync()

            print(
                f"Synced {len(synced)} slash commands."
            )

        except Exception as error:

            print(
                f"Failed to sync commands: {error}"
            )


bot = StorageBot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# BOT READY
# ============================================================

@bot.event
async def on_ready():

    print("=" * 55)
    print(f"Logged in as: {bot.user}")
    print(f"Bot ID: {bot.user.id}")
    print("House Storage Bot is ONLINE!")
    print("=" * 55)


# ============================================================
# HELPER: CLEAN ITEM NAME
# ============================================================

def clean_item_name(item: str) -> str:

    # Example:
    #
    # hardwood boards
    # HARDWOOD BOARDS
    # Hardwood    Boards
    #
    # All become:
    #
    # Hardwood Boards

    return " ".join(
        item.strip().split()
    ).title()


# ============================================================
# HELPER: GET MEMBER NAME
# ============================================================

async def get_member_name(
    guild: discord.Guild,
    user_id: int
) -> str:

    member = guild.get_member(user_id)

    if member:

        return member.display_name


    try:

        member = await guild.fetch_member(
            user_id
        )

        return member.display_name

    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException
    ):

        return f"Unknown User ({user_id})"


# ============================================================
# SHARED STORAGE PANEL
# ============================================================

async def update_storage_panel(
    guild: discord.Guild
):

    # --------------------------------------------------------
    # Find panel information
    # --------------------------------------------------------

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


    # Server doesn't have a panel yet
    if not panel_data:
        return


    channel_id, message_id = panel_data


    # --------------------------------------------------------
    # Find channel
    # --------------------------------------------------------

    channel = guild.get_channel(
        channel_id
    )


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


    # --------------------------------------------------------
    # Find panel message
    # --------------------------------------------------------

    try:

        message = await channel.fetch_message(
            message_id
        )

    except discord.NotFound:

        # Panel message was deleted.
        # Forget the old panel.

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


    # --------------------------------------------------------
    # Get ALL storage for THIS SERVER
    # --------------------------------------------------------

    async with database_lock:

        cursor.execute(
            """
            SELECT user_id, item, amount
            FROM storage
            WHERE guild_id = ?
            ORDER BY user_id ASC, item ASC
            """,
            (guild.id,)
        )

        all_items = cursor.fetchall()


    # --------------------------------------------------------
    # Build panel
    # --------------------------------------------------------

    embed = discord.Embed(
        title="🏠 HOUSE STORAGE",
        color=discord.Color.blue()
    )


    # --------------------------------------------------------
    # Empty storage
    # --------------------------------------------------------

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

            await message.edit(
                embed=embed
            )

        except discord.HTTPException:
            pass

        return


    # --------------------------------------------------------
    # Group storage by user
    # --------------------------------------------------------

    users = {}


    for user_id, item, amount in all_items:

        if user_id not in users:

            users[user_id] = []


        users[user_id].append(
            (item, amount)
        )


    # --------------------------------------------------------
    # Add users to panel
    # --------------------------------------------------------

    for user_id, items in users.items():

        # Discord embeds have a maximum
        # of 25 fields.

        if len(embed.fields) >= 25:

            break


        username = await get_member_name(
            guild,
            user_id
        )


        item_lines = []


        for item, amount in items:

            item_lines.append(
                f"• **{item}** — `{amount:,}`"
            )


        item_text = "\n".join(
            item_lines
        )


        # Keep field below Discord limit.

        if len(item_text) > 1000:

            item_text = (
                item_text[:950]
                + "\n\n*More items not shown...*"
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


    # --------------------------------------------------------
    # Update Discord message
    # --------------------------------------------------------

    try:

        await message.edit(
            embed=embed
        )

    except discord.HTTPException:

        pass


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

    # --------------------------------------------------------
    # Must be inside server
    # --------------------------------------------------------

    if interaction.guild is None:

        await interaction.response.send_message(
            "❌ This command can only be used inside a server.",
            ephemeral=True
        )

        return


    # --------------------------------------------------------
    # Find existing panel
    # --------------------------------------------------------

    async with database_lock:

        cursor.execute(
            """
            SELECT channel_id, message_id
            FROM panels
            WHERE guild_id = ?
            """,
            (interaction.guild.id,)
        )

        existing_panel = cursor.fetchone()


    # --------------------------------------------------------
    # Delete old panel
    # --------------------------------------------------------

    if existing_panel:

        old_channel_id, old_message_id = (
            existing_panel
        )


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


    # --------------------------------------------------------
    # Create new panel
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # Save panel
    # --------------------------------------------------------

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


    # Populate existing storage if there is any.

    await update_storage_panel(
        interaction.guild
    )


# ============================================================
# /DEPOSIT
# ============================================================

@bot.tree.command(
    name="deposit",
    description="Deposit an item into your house storage."
)
@app_commands.describe(
    amount="Amount you are depositing",
    item="Item you are depositing"
)
async def deposit(
    interaction: discord.Interaction,
    amount: int,
    item: str
):

    # --------------------------------------------------------
    # Server check
    # --------------------------------------------------------

    if interaction.guild is None:

        await interaction.response.send_message(
            "❌ Deposits can only be made inside a server.",
            ephemeral=True
        )

        return


    # --------------------------------------------------------
    # Amount check
    # --------------------------------------------------------

    if amount <= 0:

        await interaction.response.send_message(
            "❌ Deposit amount must be greater than 0.",
            ephemeral=True
        )

        return


    # --------------------------------------------------------
    # Clean item
    # --------------------------------------------------------

    item = clean_item_name(
        item
    )


    if not item:

        await interaction.response.send_message(
            "❌ You need to enter an item name.",
            ephemeral=True
        )

        return


    if len(item) > 100:

        await interaction.response.send_message(
            "❌ Item names must be 100 characters or less.",
            ephemeral=True
        )

        return


    # --------------------------------------------------------
    # Database
    # --------------------------------------------------------

    async with database_lock:

        cursor.execute(
            """
            SELECT amount
            FROM storage
            WHERE guild_id = ?
            AND user_id = ?
            AND item = ?
            """,
            (
                interaction.guild.id,
                interaction.user.id,
                item
            )
        )


        result = cursor.fetchone()


        # ----------------------------------------------------
        # Existing item
        # ----------------------------------------------------

        if result:

            current_amount = result[0]

            new_amount = (
                current_amount + amount
            )


            cursor.execute(
                """
                UPDATE storage
                SET amount = ?
                WHERE guild_id = ?
                AND user_id = ?
                AND item = ?
                """,
                (
                    new_amount,
                    interaction.guild.id,
                    interaction.user.id,
                    item
                )
            )


        # ----------------------------------------------------
        # New item
        # ----------------------------------------------------

        else:

            new_amount = amount


            cursor.execute(
                """
                INSERT INTO storage
                (
                    guild_id,
                    user_id,
                    item,
                    amount
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    interaction.guild.id,
                    interaction.user.id,
                    item,
                    amount
                )
            )


        database.commit()


    # --------------------------------------------------------
    # Confirmation
    # --------------------------------------------------------

    embed = discord.Embed(
        title="📦 Deposit Successful",
        color=discord.Color.green()
    )


    embed.add_field(
        name="Deposited",
        value=(
            f"**{amount:,} × {item}**"
        ),
        inline=False
    )


    embed.add_field(
        name="Your Total",
        value=(
            f"**{new_amount:,} × {item}**"
        ),
        inline=False
    )


    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


    # --------------------------------------------------------
    # Update shared panel
    # --------------------------------------------------------

    await update_storage_panel(
        interaction.guild
    )


# ============================================================
# /WITHDRAW
# ============================================================

@bot.tree.command(
    name="withdraw",
    description="Withdraw an item from your house storage."
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

    # --------------------------------------------------------
    # Server check
    # --------------------------------------------------------

    if interaction.guild is None:

        await interaction.response.send_message(
            "❌ Withdrawals can only be made inside a server.",
            ephemeral=True
        )

        return


    # --------------------------------------------------------
    # Amount validation
    # --------------------------------------------------------

    if amount <= 0:

        await interaction.response.send_message(
            "❌ Withdrawal amount must be greater than 0.",
            ephemeral=True
        )

        return


    # --------------------------------------------------------
    # Clean item
    # --------------------------------------------------------

    item = clean_item_name(
        item
    )


    if not item:

        await interaction.response.send_message(
            "❌ You need to enter an item name.",
            ephemeral=True
        )

        return


    # --------------------------------------------------------
    # Find user's item
    # --------------------------------------------------------

    async with database_lock:

        cursor.execute(
            """
            SELECT amount
            FROM storage
            WHERE guild_id = ?
            AND user_id = ?
            AND item = ?
            """,
            (
                interaction.guild.id,
                interaction.user.id,
                item
            )
        )


        result = cursor.fetchone()


    # --------------------------------------------------------
    # Doesn't own item
    # --------------------------------------------------------

    if not result:

        await interaction.response.send_message(
            (
                f"❌ You don't have "
                f"**{item}** in storage."
            ),
            ephemeral=True
        )

        return


    current_amount = result[0]


    # --------------------------------------------------------
    # Not enough
    # --------------------------------------------------------

    if amount > current_amount:

        await interaction.response.send_message(
            (
                f"❌ You don't have enough "
                f"**{item}**.\n\n"
                f"You currently have "
                f"**{current_amount:,}**."
            ),
            ephemeral=True
        )

        return


    new_amount = (
        current_amount - amount
    )


    # --------------------------------------------------------
    # Update database
    # --------------------------------------------------------

    async with database_lock:

        # Remove item entirely at zero.

        if new_amount == 0:

            cursor.execute(
                """
                DELETE FROM storage
                WHERE guild_id = ?
                AND user_id = ?
                AND item = ?
                """,
                (
                    interaction.guild.id,
                    interaction.user.id,
                    item
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
                """,
                (
                    new_amount,
                    interaction.guild.id,
                    interaction.user.id,
                    item
                )
            )


        database.commit()


    # --------------------------------------------------------
    # Confirmation
    # --------------------------------------------------------

    embed = discord.Embed(
        title="📤 Withdrawal Successful",
        color=discord.Color.red()
    )


    embed.add_field(
        name="Withdrawn",
        value=(
            f"**{amount:,} × {item}**"
        ),
        inline=False
    )


    if new_amount == 0:

        embed.add_field(
            name="Remaining",
            value="**0**",
            inline=False
        )


    else:

        embed.add_field(
            name="Remaining",
            value=(
                f"**{new_amount:,} × {item}**"
            ),
            inline=False
        )


    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


    # --------------------------------------------------------
    # Update shared panel
    # --------------------------------------------------------

    await update_storage_panel(
        interaction.guild
    )


# ============================================================
# /STORAGE
# ============================================================

@bot.tree.command(
    name="storage",
    description="View your personal house storage."
)
async def storage(
    interaction: discord.Interaction
):

    # --------------------------------------------------------
    # Server check
    # --------------------------------------------------------

    if interaction.guild is None:

        await interaction.response.send_message(
            "❌ Storage can only be viewed inside a server.",
            ephemeral=True
        )

        return


    # --------------------------------------------------------
    # Get ONLY this user's storage
    # --------------------------------------------------------

    async with database_lock:

        cursor.execute(
            """
            SELECT item, amount
            FROM storage
            WHERE guild_id = ?
            AND user_id = ?
            ORDER BY item ASC
            """,
            (
                interaction.guild.id,
                interaction.user.id
            )
        )


        items = cursor.fetchall()


    # --------------------------------------------------------
    # Build personal inventory
    # --------------------------------------------------------

    embed = discord.Embed(
        title="🏠 Your House Storage",
        color=discord.Color.blue()
    )


    if not items:

        embed.description = (
            "📭 Your storage is currently empty."
        )


    else:

        item_lines = []


        for item, amount in items:

            item_lines.append(
                f"• **{item}** — `{amount:,}`"
            )


        storage_text = "\n".join(
            item_lines
        )


        # Protect against Discord embed limit.

        if len(storage_text) > 4000:

            storage_text = (
                storage_text[:3900]
                + "\n\n*More items not shown...*"
            )


        embed.description = storage_text


    embed.set_footer(
        text=(
            f"Storage belonging to "
            f"{interaction.user.display_name}"
        )
    )


    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# /ADMINREMOVE
# ============================================================

@bot.tree.command(
    name="adminremove",
    description="Admin: Remove items from another user's storage."
)
@app_commands.describe(
    user="User whose storage you want to modify",
    amount="Amount you want to remove",
    item="Item you want to remove"
)
@app_commands.checks.has_permissions(
    administrator=True
)
async def adminremove(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
    item: str
):

    # --------------------------------------------------------
    # Server check
    # --------------------------------------------------------

    if interaction.guild is None:

        await interaction.response.send_message(
            "❌ This command can only be used inside a server.",
            ephemeral=True
        )

        return


    # --------------------------------------------------------
    # Amount check
    # --------------------------------------------------------

    if amount <= 0:

        await interaction.response.send_message(
            "❌ Amount must be greater than 0.",
            ephemeral=True
        )

        return


    # --------------------------------------------------------
    # Clean item
    # --------------------------------------------------------

    item = clean_item_name(
        item
    )


    if not item:

        await interaction.response.send_message(
            "❌ You need to enter an item name.",
            ephemeral=True
        )

        return


    # --------------------------------------------------------
    # Find selected user's item
    # --------------------------------------------------------

    async with database_lock:

        cursor.execute(
            """
            SELECT amount
            FROM storage
            WHERE guild_id = ?
            AND user_id = ?
            AND item = ?
            """,
            (
                interaction.guild.id,
                user.id,
                item
            )
        )


        result = cursor.fetchone()


    # --------------------------------------------------------
    # Item not found
    # --------------------------------------------------------

    if not result:

        await interaction.response.send_message(
            (
                f"❌ **{user.display_name}** "
                f"doesn't have **{item}** "
                f"in storage."
            ),
            ephemeral=True
        )

        return


    current_amount = result[0]


    # --------------------------------------------------------
    # Trying to remove too much
    # --------------------------------------------------------

    if amount > current_amount:

        await interaction.response.send_message(
            (
                f"❌ **{user.display_name}** only has "
                f"**{current_amount:,} × {item}**.\n\n"
                f"You cannot remove **{amount:,}**."
            ),
            ephemeral=True
        )

        return


    new_amount = (
        current_amount - amount
    )


    # --------------------------------------------------------
    # Update database
    # --------------------------------------------------------

    async with database_lock:

        if new_amount == 0:

            cursor.execute(
                """
                DELETE FROM storage
                WHERE guild_id = ?
                AND user_id = ?
                AND item = ?
                """,
                (
                    interaction.guild.id,
                    user.id,
                    item
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
                """,
                (
                    new_amount,
                    interaction.guild.id,
                    user.id,
                    item
                )
            )


        database.commit()


    # --------------------------------------------------------
    # Confirmation
    # --------------------------------------------------------

    embed = discord.Embed(
        title="🛡️ Admin Storage Adjustment",
        color=discord.Color.orange()
    )


    embed.add_field(
        name="User",
        value=user.mention,
        inline=False
    )


    embed.add_field(
        name="Removed",
        value=(
            f"**{amount:,} × {item}**"
        ),
        inline=False
    )


    embed.add_field(
        name="Remaining",
        value=(
            f"**{new_amount:,} × {item}**"
        ),
        inline=False
    )


    embed.set_footer(
        text=(
            f"Removed by "
            f"{interaction.user.display_name}"
        )
    )


    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


    # --------------------------------------------------------
    # Refresh public panel
    # --------------------------------------------------------

    await update_storage_panel(
        interaction.guild
    )


# ============================================================
# /SETUPPANEL ERROR
# ============================================================

@setuppanel.error
async def setuppanel_error(
    interaction: discord.Interaction,
    error
):

    if isinstance(
        error,
        app_commands.MissingPermissions
    ):

        message = (
            "❌ You need the **Manage Server** "
            "permission to use `/setuppanel`."
        )


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


# ============================================================
# /ADMINREMOVE ERROR
# ============================================================

@adminremove.error
async def adminremove_error(
    interaction: discord.Interaction,
    error
):

    if isinstance(
        error,
        app_commands.MissingPermissions
    ):

        message = (
            "❌ Only Discord administrators "
            "can use `/adminremove`."
        )


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


# ============================================================
# START BOT
# ============================================================

print("Starting House Storage Bot...")

bot.run(TOKEN)
