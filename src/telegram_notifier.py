from __future__ import annotations

import logging
import json
import os
import ssl
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass
from datetime import datetime

# FIX: create SSL context that handles self-signed certificates in corporate proxies
_SSL_CTX = ssl.create_default_context()
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    pass  # fall back to system certs

@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""

class TelegramNotifier:
    """Enhanced Telegram notifier with comprehensive message templates"""
    
    def __init__(self, config: TelegramConfig):
        self.config = config
        self.logger = logging.getLogger("TelegramNotifier")
        self.trade_counter = 0
        self.last_alert_time = {}
        self.alert_cooldown = 300  # 5 minutes between same alert types
        self._bot_profile: Optional[Dict[str, Any]] = None
        self._last_chat_lookup_at: Optional[datetime] = None
        self._chat_lookup_cooldown_seconds = 60
        self._status_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self._last_missing_chat_warning_at: Optional[datetime] = None
        self._missing_chat_warning_cooldown_seconds = 300
        self._last_update_id = 0
        self._commands: Dict[str, Callable] = {}
        self._keyboard_handlers: Dict[str, Callable] = {}
    
    def send_trade_execution(self, trade_data: Dict[str, Any]) -> bool:
        """Send trade execution notification"""
        if not self.config.enabled:
            return True
        
        self.trade_counter += 1
        
        message = self._format_trade_message(trade_data)
        return self._send_message(message)

    def send(self, message: str) -> bool:
        """Send a plain text notification (compatibility with system logger)."""
        if not self.config.enabled:
            return True
        return self._send_message(str(message))
    
    def send_trade_exit(self, trade_data: Dict[str, Any]) -> bool:
        """Send trade exit notification"""
        if not self.config.enabled:
            return True
        
        message = self._format_exit_message(trade_data)
        return self._send_message(message)
    
    def send_alert(self, alert_type: str, message: str, severity: str = "WARNING") -> bool:
        """Send alert notification with cooldown"""
        if not self.config.enabled:
            return True
        
        # Check cooldown to prevent spam
        current_time = datetime.now()
        if alert_type in self.last_alert_time:
            time_diff = (current_time - self.last_alert_time[alert_type]).total_seconds()
            if time_diff < self.alert_cooldown:
                return True
        
        self.last_alert_time[alert_type] = current_time
        
        alert_message = self._format_alert_message(alert_type, message, severity)
        return self._send_message(alert_message)
    
    def send_milestone(self, milestone_type: str, progress: Dict[str, Any]) -> bool:
        """Send forward-test milestone notification"""
        if not self.config.enabled:
            return True
        
        message = self._format_milestone_message(milestone_type, progress)
        return self._send_message(message)

    def set_status_provider(self, provider: Optional[Callable[[], Dict[str, Any]]]) -> None:
        """Register a callback that supplies live status values for alerts."""
        self._status_provider = provider
    
    def _format_trade_message(self, trade_data: Dict[str, Any]) -> str:
        """Format trade execution message"""
        direction_emoji = "🟢" if trade_data.get("direction") == "BUY" else "🔴"
        confluence_score = trade_data.get("confluence_score", "N/A")
        confluence_signals = trade_data.get("confluence_signals", "")
        session = trade_data.get("session", "")

        message = f"""
{direction_emoji} [TRADE EXECUTED] - EURUSD H1 RSI_PULLBACK

Trade #{self.trade_counter} {trade_data.get('direction', 'UNKNOWN')}
Entry: {trade_data.get('entry_price', 'N/A')}
SL: {trade_data.get('stop_loss', 'N/A')}
TP: {trade_data.get('take_profit', 'N/A')}
RR: {trade_data.get('risk_reward', 'N/A')}:1
Volume: {trade_data.get('volume_multiplier', 'N/A')}x
RSI: {trade_data.get('rsi_value', 'N/A')} ({trade_data.get('rsi_pullback', 'N/A')})
SR Tolerance: {trade_data.get('sr_tolerance', 'N/A')}

� Confluence: {confluence_score}/10
{confluence_signals}

🕐 Session: {session}

�🔒 Safety Status:
Daily Loss: {trade_data.get('daily_loss_pct', '0.0')}% / 4%
Open Trades: {trade_data.get('open_trades', '0')} / 3
News Pause: {trade_data.get('news_pause_status', 'Active')}

⚡ Execution:
Slippage: {trade_data.get('slippage_pips', '0.0')} pips
Spread: {trade_data.get('spread_points', '0')} points
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

✅ Forward-Test Progress: {trade_data.get('trades_completed', '0')} / 20 trades
"""
        return message.strip()
    
    def _format_exit_message(self, trade_data: Dict[str, Any]) -> str:
        """Format trade exit message"""
        pnl = trade_data.get('profit_loss', 0)
        result_emoji = "💰" if pnl > 0 else "📉" if pnl < 0 else "➡️"
        win_loss = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BE"
        
        message = f"""
{result_emoji} [TRADE CLOSED] - EURUSD H1 RSI_PULLBACK

Trade #{self.trade_counter} {win_loss}
Entry: {trade_data.get('entry_price', 'N/A')}
Exit: {trade_data.get('exit_price', 'N/A')}
PnL: ${pnl:.2f}
R-Multiple: {trade_data.get('r_multiple', 'N/A')}
Exit Reason: {trade_data.get('exit_reason', 'N/A')}
Duration: {trade_data.get('duration_minutes', 'N/A')} min

⚡ Execution:
Slippage: {trade_data.get('slippage_pips', '0.0')} pips
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

✅ Forward-Test Progress: {trade_data.get('trades_completed', '0')} / 20 trades
"""
        return message.strip()
    
    def _format_alert_message(self, alert_type: str, message: str, severity: str) -> str:
        """Format alert message"""
        severity_emoji = {
            "INFO": "ℹ️",
            "WARNING": "⚠️",
            "ERROR": "🚨",
            "CRITICAL": "🔴"
        }.get(severity, "ℹ️")
        
        message = f"""
{severity_emoji} [{severity}] - EURUSD H1 RSI_PULLBACK

Alert Type: {alert_type}
Message: {message}
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

🔒 Current Safety Status:
Daily Loss: {self._get_current_daily_loss()}% / 1.5%
Open Trades: {self._get_current_open_trades()} / 1
Bot Status: {self._get_bot_status()}
"""
        return message.strip()
    
    def _format_milestone_message(self, milestone_type: str, progress: Dict[str, Any]) -> str:
        """Format milestone message"""
        milestone_emoji = "🎯" if "trade" in milestone_type.lower() else "📅"
        
        message = f"""
{milestone_emoji} [MILESTONE] - EURUSD H1 RSI_PULLBACK

{milestone_type}

📊 Current Performance:
Total Trades: {progress.get('total_trades', '0')}
Win Rate: {progress.get('win_rate', '0.0'):.1%}
Profit Factor: {progress.get('profit_factor', '0.0'):.2f}
Expectancy: ${progress.get('expectancy', '0.0'):.2f}
Total PnL: ${progress.get('total_pnl', '0.0'):.2f}

⚡ Execution Quality:
Avg Slippage: {progress.get('avg_slippage', '0.0')} pips
Max Drawdown: {progress.get('max_drawdown', '0.0'):.1%}

🎯 Forward-Test Progress: {progress.get('trades_completed', '0')} / 20 trades
Test Duration: {progress.get('test_days', '0')} days
"""
        return message.strip()

    def _get_live_status(self) -> Dict[str, Any]:
        """Return the latest status snapshot from the registered provider."""
        provider = self._status_provider
        if provider is None:
            return {}

        try:
            data = provider() or {}
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            self.logger.exception("Telegram status provider exception: %s", exc)
            return {}

    def get_bot_profile(self) -> Optional[Dict[str, Any]]:
        """Fetch and cache bot metadata from Telegram."""
        if self._bot_profile is not None:
            return self._bot_profile

        result = self._api_request("getMe")
        if isinstance(result, dict):
            self._bot_profile = result
            return result
        return None

    def discover_chat_id(self, force: bool = False) -> str:
        """Try to resolve the target chat from bot updates."""
        if self.config.chat_id:
            return str(self.config.chat_id)

        now = datetime.now()
        if not force and self._last_chat_lookup_at is not None:
            age = (now - self._last_chat_lookup_at).total_seconds()
            if age < self._chat_lookup_cooldown_seconds:
                return ""
        self._last_chat_lookup_at = now

        updates = self._api_request("getUpdates")
        if not isinstance(updates, list) or not updates:
            return ""

        chat_ids: list[str] = []
        for update in reversed(updates):
            chat = self._extract_chat_from_update(update)
            if not isinstance(chat, dict):
                continue

            chat_id = str(chat.get("id", "") or "").strip()
            if chat_id and chat_id not in chat_ids:
                chat_ids.append(chat_id)

        if len(chat_ids) == 1:
            self.config.chat_id = chat_ids[0]
            self.logger.info("Telegram chat_id auto-discovered from bot updates.")
            return self.config.chat_id

        if len(chat_ids) > 1:
            self.logger.warning("Multiple Telegram chats found in updates. Set TELEGRAM_CHAT_ID explicitly.")

        return ""

    def _extract_chat_from_update(self, update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract chat payload from the common Telegram update shapes."""
        direct_keys = (
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "my_chat_member",
            "chat_member",
        )
        for key in direct_keys:
            payload = update.get(key)
            if isinstance(payload, dict):
                chat = payload.get("chat")
                if isinstance(chat, dict):
                    return chat

        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            message = callback_query.get("message")
            if isinstance(message, dict):
                chat = message.get("chat")
                if isinstance(chat, dict):
                    return chat

        return None

    def _api_request(self, method: str, data: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        """Call the Telegram Bot API and return the result payload."""
        if not self.config.bot_token:
            self.logger.warning("Telegram bot token missing")
            return None

        url = f"https://api.telegram.org/bot{self.config.bot_token}/{method}"

        try:
            encoded_data = None
            headers = {}
            if data is not None:
                # Always use JSON so nested objects (reply_markup, etc.) serialize correctly
                encoded_data = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = "application/json"

            req = urllib.request.Request(url, data=encoded_data, headers=headers)
            with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                body = resp.read().decode("utf-8", errors="replace")

            payload = json.loads(body or "{}")
            if not payload.get("ok"):
                self.logger.error("Telegram %s failed: %s", method, payload)
                return None

            return payload.get("result")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            self.logger.error("Telegram %s HTTP error: %s %s", method, exc.code, body)
            return None
        except Exception as exc:
            self.logger.exception("Telegram %s exception: %s", method, exc)
            return None
    
    def _send_message(self, message: str) -> bool:
        """Send message to Telegram"""
        if not self.config.enabled:
            return True

        if not self.config.bot_token:
            self.logger.warning("Telegram not configured: TELEGRAM_BOT_TOKEN is missing")
            return False

        if not self.config.chat_id:
            self.discover_chat_id()

        if not self.config.chat_id:
            bot_profile = self.get_bot_profile() or {}
            username = str(bot_profile.get("username", "") or "").strip()
            now = datetime.now()
            should_warn = (
                self._last_missing_chat_warning_at is None
                or (now - self._last_missing_chat_warning_at).total_seconds() >= self._missing_chat_warning_cooldown_seconds
            )
            if should_warn:
                if username:
                    self.logger.warning(
                        "Telegram chat_id missing. Open https://t.me/%s and send /start, then set TELEGRAM_CHAT_ID or retry.",
                        username,
                    )
                else:
                    self.logger.warning("Telegram chat_id missing. Send /start to the bot and set TELEGRAM_CHAT_ID.")
                self._last_missing_chat_warning_at = now
            return False

        data = {
            "chat_id": self.config.chat_id,
            "text": message,
            "disable_web_page_preview": True
        }
        return self._api_request("sendMessage", data) is not None
    
    def send_photo(self, photo_path: str, caption: str = "") -> bool:
        """Send a photo/image to Telegram."""
        if not self.config.enabled or not self.config.bot_token or not self.config.chat_id:
            return False
        
        if not os.path.exists(photo_path):
            self.logger.error("Photo not found: %s", photo_path)
            return False

        url = f"https://api.telegram.org/bot{self.config.bot_token}/sendPhoto"
        
        try:
            from multipart import MultipartEncoder # I might need to install this or use a simpler way
            # Since I want to avoid adding too many dependencies, I'll use a simpler way if possible
            # Actually, I'll just use a basic multipart implementation or a library if needed.
            # I'll try with a simpler 'requests' based approach if available, but I used urllib.
            
            # I'll use 'requests' if available as it handles files much better.
            import requests
            with open(photo_path, 'rb') as f:
                files = {'photo': f}
                data = {'chat_id': self.config.chat_id, 'caption': caption}
                resp = requests.post(url, data=data, files=files, timeout=20)
                return resp.status_code == 200
        except Exception as exc:
            self.logger.exception("Telegram send_photo exception: %s", exc)
            return False
    
    def register_command(self, command: str, callback: Callable) -> None:
        """Register a command handler (e.g. /status)."""
        cmd = command.lower()
        if not cmd.startswith("/"):
            cmd = "/" + cmd
        self._commands[cmd] = callback

    def register_keyboard_handler(self, callback_data: str, callback: Callable) -> None:
        """Register a handler for a custom inline keyboard callback_data string."""
        self._keyboard_handlers[callback_data] = callback

    # ── Inline keyboard helpers ────────────────────────────────────────────────

    def _get_main_menu_buttons(self) -> list:
        """Return the standard bot command menu as an inline keyboard layout."""
        return [
            [
                {"text": "📊 Status",     "callback_data": "cmd_/status"},
                {"text": "💰 Balance",    "callback_data": "cmd_/balance"},
            ],
            [
                {"text": "📈 Positions",  "callback_data": "cmd_/positions"},
                {"text": "📋 Report",     "callback_data": "cmd_/report"},
            ],
            [
                {"text": "⏸ Pause Bot",  "callback_data": "cmd_/stop"},
                {"text": "▶️ Resume Bot", "callback_data": "cmd_/start"},
            ],
            [
                {"text": "⚙️ Risk",       "callback_data": "cmd_/risk"},
                {"text": "❓ Help",        "callback_data": "cmd_/help"},
            ],
        ]

    def send_menu(self, text: str, buttons: Optional[list] = None) -> bool:
        """Send a message with an inline keyboard.

        buttons: list of rows; each row is a list of dicts with 'text' and
                 'callback_data' keys.  Falls back to the standard main menu
                 when *buttons* is None.
        """
        if not self.config.enabled or not self.config.bot_token:
            return True
        if not self.config.chat_id:
            self.discover_chat_id()
        if not self.config.chat_id:
            return False

        keyboard = buttons if buttons is not None else self._get_main_menu_buttons()
        data: Dict[str, Any] = {
            "chat_id": self.config.chat_id,
            "text": text,
            "reply_markup": {"inline_keyboard": keyboard},
            "disable_web_page_preview": True,
        }
        return self._api_request("sendMessage", data) is not None

    def _answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """ACK an inline-button press (removes the loading spinner on the client)."""
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]  # Telegram limit
        self._api_request("answerCallbackQuery", payload)

    def _handle_callback_query(self, callback_query: Dict[str, Any]) -> None:
        """Dispatch an inline keyboard button press."""
        query_id  = str(callback_query.get("id", ""))
        callback_data = str(callback_query.get("data", ""))
        msg_obj   = callback_query.get("message") or {}
        chat_id   = str((msg_obj.get("chat") or {}).get("id", ""))

        allowed_chat = str(self.config.chat_id or "").strip()
        if allowed_chat and chat_id != allowed_chat:
            return

        # ACK immediately so the button stops spinning
        self._answer_callback_query(query_id)

        # cmd_ prefix means it maps directly to a registered /command
        if callback_data.startswith("cmd_"):
            cmd_key = callback_data[4:]  # e.g. "cmd_/status" → "/status"
            if cmd_key in self._commands:
                try:
                    self.logger.info("Telegram keyboard button: %s", cmd_key)
                    response = self._commands[cmd_key]()
                    if response:
                        self.send(response)
                except Exception as exc:
                    self.logger.exception("Error in keyboard callback %s: %s", cmd_key, exc)
                    self.send(f"❌ Error: {exc}")
            return

        # Custom handlers registered via register_keyboard_handler()
        if callback_data in self._keyboard_handlers:
            try:
                response = self._keyboard_handlers[callback_data]()
                if response:
                    self.send(response)
            except Exception as exc:
                self.logger.exception("Error in custom keyboard handler %s: %s", callback_data, exc)
                self.send(f"❌ Error: {exc}")

    def _send_unknown_command_response(self) -> None:
        """Respond to unrecognized input with the interactive menu."""
        cmds = "  ".join(sorted(self._commands.keys()))
        text = (
            "🤖 RSI-Pullback Trading Bot\n\n"
            "I didn't recognise that. Tap a button or type a command:\n\n"
            f"{cmds}"
        )
        self.send_menu(text)

    # ── Update polling ─────────────────────────────────────────────────────────

    def poll_updates(self) -> None:
        """Poll for new messages / button presses and dispatch them."""
        if not self.config.enabled or not self.config.bot_token:
            return

        data: Dict[str, Any] = {"offset": self._last_update_id + 1, "timeout": 0}
        updates = self._api_request("getUpdates", data)
        if not isinstance(updates, list):
            return

        for update in updates:
            self._last_update_id = int(update.get("update_id", self._last_update_id))

            # ── Inline keyboard button press ──────────────────────────────────
            callback_query = update.get("callback_query")
            if isinstance(callback_query, dict):
                self._handle_callback_query(callback_query)
                continue

            # ── Text message ──────────────────────────────────────────────────
            message = update.get("message")
            if not isinstance(message, dict):
                continue

            raw_text = str(message.get("text", "")).strip()
            chat_id  = str((message.get("chat") or {}).get("id", ""))

            allowed_chat = str(self.config.chat_id or "").strip()
            if not allowed_chat or chat_id != allowed_chat:
                continue

            # Normalise to "/command" form (handle bare words + Bot name suffix)
            # e.g. "/status", "status", "STATUS", "/status@mybot" all → "/status"
            text_lower = raw_text.lower().split("@")[0].strip()
            if text_lower.startswith("/"):
                cmd_key = text_lower
            else:
                # treat first word as the command name
                first_word = text_lower.split()[0] if text_lower else ""
                cmd_key = "/" + first_word if first_word else ""

            if cmd_key and cmd_key in self._commands:
                try:
                    self.logger.info("Telegram command received: %s", cmd_key)
                    response = self._commands[cmd_key]()
                    if response:
                        self.send(response)
                except Exception as exc:
                    self.logger.exception("Error handling command %s: %s", cmd_key, exc)
                    self.send(f"❌ Error executing command: {exc}")
            else:
                # Unknown input — show interactive menu
                self._send_unknown_command_response()
    
    # Helper methods for getting current status (to be implemented with actual data sources)
    def _get_current_daily_loss(self) -> str:
        """Get current daily loss percentage"""
        status = self._get_live_status()
        value = status.get("daily_loss_pct", 0.0)
        try:
            return f"{float(value):.2f}"
        except Exception:
            return str(value)
    
    def _get_current_open_trades(self) -> str:
        """Get current open trades count"""
        status = self._get_live_status()
        value = status.get("open_trades", 0)
        try:
            return str(int(value))
        except Exception:
            return str(value)
    
    def _get_bot_status(self) -> str:
        """Get current bot status"""
        status = self._get_live_status()
        return str(status.get("bot_status", "Running"))


def build_telegram_config_from_env(getenv) -> TelegramConfig:
    enabled_raw = str(getenv("TELEGRAM_ENABLED", "false")).strip().lower()
    enabled = enabled_raw in {"1", "true", "yes", "y", "on"}
    return TelegramConfig(
        enabled=enabled,
        bot_token=str(getenv("TELEGRAM_BOT_TOKEN", "") or ""),
        chat_id=str(getenv("TELEGRAM_CHAT_ID", "") or ""),
    )
