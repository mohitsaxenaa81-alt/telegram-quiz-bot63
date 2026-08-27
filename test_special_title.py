import html

title = "#ᴀɴᴛɪᴍ_ᴘʀᴀʜᴀʀ 🔥#ᴍᴀʜɪ💗"
creator = "MAHI 💗"
quiz_id = "GGN1NZBG4"
q_count = 11
timer = 15

safe_name = html.escape(str(title))
safe_creator = html.escape(str(creator))
safe_quiz_id = html.escape(str(quiz_id))

msg_text = (
    f"Quiz Created! 💬\n\n"
    f"💳 Name: {safe_name}\n"
    f"#️⃣ Questions: {q_count}\n"
    f"⏰ Timer: {timer}s\n"
    f"🆔 ID: <code>{safe_quiz_id}</code>\n"
    f"💰 Type: free\n"
    f"☠️ -ve: 0.00\n"
    f"👧 Creator: {safe_creator}"
)

print("Escaped HTML output:")
print(msg_text)
