
"""Admin-only commands for Cowai bot (Maicé)."""
import discord
from core.mood import trust

def is_admin(message: discord.Message) -> bool:
	try:
		if not message.guild:
			return False
		perms = getattr(message.author, "guild_permissions", None)
		return bool(perms and getattr(perms, "administrator", False))
	except Exception:
		return False

async def handle_trust_admin(message: discord.Message, content: str) -> bool:
	if not is_admin(message):
		await message.channel.send("You don't have permission to manage trust.")
		return True
	is_set = content.lower().startswith("!trustset")
	raw = content.split(maxsplit=2)
	if len(raw) < 2:
		await message.channel.send(
			"Usage: `!trustset <0.0-1.0> [reason]` or `!trustadd <-1.0..+1.0> [reason]`"
		)
		return True
	try:
		value = float(raw[1])
	except Exception:
		await message.channel.send("Invalid number.")
		return True
	reason = raw[2].strip() if len(raw) >= 3 else "admin"
	uid = message.author.id
	if is_set:
		new_score = trust.set_score(uid, value, reason=f"trustset: {reason}")
		await message.channel.send(f"Trust set to **{new_score:.2f}**")
	else:
		new_score = trust.add(uid, value, reason=f"trustadd: {reason}")
		await message.channel.send(f"Trust updated to **{new_score:.2f}**")
	return True
