
# Telegram Control Plane v2

Bootstrap Mode:
- Set TG_BOOTSTRAP=1
- Send any DM to bot
- Check server logs for USER_ID and CHAT_ID
- Then set TG_BOOTSTRAP=0

Commands:
/status
/pause <PIN>
/resume <PIN>

Security:
- Allowlist enforced
- PBKDF2 PIN verification
- Long polling only
