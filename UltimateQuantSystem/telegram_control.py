"""
Two-way Telegram control layer.

This intentionally exposes a small command surface instead of arbitrary shell
execution. A Telegram chat is not a safe remote terminal.
"""

import os
import subprocess
import sys
import time
import threading
import requests
from html import escape
from pathlib import Path
from loguru import logger

from config import config


class TelegramControlBot:
    def __init__(self, registry, risk, journal, head_ai, report_fn):
        self.registry = registry
        self.risk = risk
        self.journal = journal
        self.head_ai = head_ai
        self.report_fn = report_fn

        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.enabled = os.getenv("TELEGRAM_CONTROL_ENABLED", "false").lower() == "true"
        self.allow_actions = os.getenv("TELEGRAM_CONTROL_ALLOW_ACTIONS", "false").lower() == "true"
        self.allow_runbook = os.getenv("TELEGRAM_CONTROL_ALLOW_RUNBOOK", "false").lower() == "true"
        self.allow_llm_chat = os.getenv("TELEGRAM_CONTROL_ALLOW_LLM_CHAT", "true").lower() == "true"
        self.poll_s = max(2, int(os.getenv("TELEGRAM_CONTROL_POLL_SECONDS", "5")))
        self.root = Path(__file__).resolve().parent
        self._offset = None
        self._thread = None
        self._stop_event = threading.Event()

    def start(self):
        if not self.enabled:
            return
        if not self.token or not self.chat_id:
            logger.warning("TelegramControl | disabled: missing token/chat id")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="telegram-control", daemon=True)
        self._thread.start()
        logger.info("TelegramControl | started")

    def stop(self, join: bool = False, timeout: float = 10.0):
        self._stop_event.set()
        if join and self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                logger.warning(f"TelegramControl | poll error: {e}")
            self._stop_event.wait(self.poll_s)

    def _poll_once(self):
        resp = requests.get(
            f"https://api.telegram.org/bot{self.token}/getUpdates",
            params={"timeout": 5, "offset": self._offset},
            timeout=10,
        )
        resp.raise_for_status()
        for update in resp.json().get("result", []):
            self._offset = update["update_id"] + 1
            msg = update.get("message") or update.get("edited_message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            if chat != self.chat_id:
                logger.warning(f"TelegramControl | ignored unauthorized chat={chat}")
                continue
            text = (msg.get("text") or "").strip()
            if text:
                self._handle(text)

    def _handle(self, text: str):
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("/help", "help"):
            self._send(self._help())
        elif cmd in ("/status", "status"):
            self._send(self.report_fn())
        elif cmd in ("/risk", "risk"):
            self._send(self._risk())
        elif cmd in ("/bots", "bots"):
            self._send(self._bots())
        elif cmd in ("/positions", "positions", "/open"):
            self._send(self._positions())
        elif cmd in ("/analyze", "analyze") or "working" in text.lower():
            self._send(self._analyze())
        elif cmd == "/run":
            self._send(self._runbook_command(args))
        elif cmd in ("/pause", "/suspend"):
            self._action(args, self._pause)
        elif cmd == "/resume":
            self._action(args, self._resume)
        elif cmd == "/reduce":
            self._action(args, self._reduce)
        elif cmd == "/boost":
            self._action(args, self._boost)
        elif not text.startswith("/") and self.allow_llm_chat:
            self._send(self._llm_chat(text))
        else:
            self._send("Unknown command. Send /help, or enable TELEGRAM_CONTROL_ALLOW_LLM_CHAT=true for normal chat.")

    def _action(self, args: list, fn):
        if not self.allow_actions:
            self._send("Control actions are disabled. Set TELEGRAM_CONTROL_ALLOW_ACTIONS=true to enable them.")
            return
        try:
            self._send(fn(args))
        except Exception as e:
            self._send(f"Command failed: <code>{escape(str(e))}</code>")

    def _help(self) -> str:
        readonly = [
            "/status - portfolio report",
            "/analyze - HeadAI review of what is working/not working",
            "/bots - bot status and ranking",
            "/positions - open trades",
            "/risk - risk engine status",
            "Normal chat - ask questions about bot performance, risk, PnL, and what is working",
        ]
        actions = [
            "/pause BOT [hours] - suspend a bot",
            "/resume BOT - resume a bot",
            "/reduce BOT [mult] - reduce allocation",
            "/boost BOT - restore full HeadAI allocation",
        ]
        runbook = [
            "/run test - run unit tests",
            "/run compile - compile project source",
            "/run git_status - show changed files",
            "/run last_log - show latest log tail",
        ]
        lines = ["<b>Telegram control commands</b>", *readonly, ""]
        lines.extend(actions if self.allow_actions else ["Actions disabled on this host."])
        lines.extend(["", *runbook] if self.allow_runbook else ["", "Runbook commands disabled on this host."])
        return "\n".join(lines)

    def _risk(self) -> str:
        rs = self.risk.status()
        return (
            "<b>Risk</b>\n"
            f"Mode: <code>{escape(rs['mode'])}</code>\n"
            f"Capital: Rs{rs['capital']:,.2f}\n"
            f"Drawdown: {rs['drawdown_pct']:.2f}% | Daily loss: {rs['daily_loss_pct']:.2f}%\n"
            f"Open risk: Rs{rs['open_risk']:,.2f} | Positions: {rs['open_positions']}"
        )

    def _bots(self) -> str:
        metrics = sorted(
            self.registry.status_all(),
            key=lambda m: (m.get("total_pnl", 0), m.get("win_rate", 0)),
            reverse=True,
        )
        lines = ["<b>Bots</b>"]
        for i, m in enumerate(metrics[:20], start=1):
            lines.append(
                f"#{i} <code>{escape(m.get('agent_id','')[:24])}</code> "
                f"{escape(m.get('status',''))} | Rs{m.get('total_pnl',0):+,.0f} | "
                f"T{m.get('trades',0)} | AI {m.get('head_ai_mult',1.0):.2f}"
            )
        return "\n".join(lines)[:3900]

    def _positions(self) -> str:
        trades = self.journal.open_trades()
        if not trades:
            return "<b>Open trades</b>\nNone"
        lines = ["<b>Open trades</b>"]
        for t in trades[:20]:
            lines.append(
                f"<code>{escape(t.agent_id[:20])}</code> {escape(t.symbol[:26])} "
                f"{escape(t.direction)} qty={t.quantity:g} risk=Rs{t.risk_amount:,.0f}"
            )
        return "\n".join(lines)[:3900]

    def _analyze(self) -> str:
        report = self.head_ai.analyze()
        lines = ["<b>HeadAI analysis</b>"]
        for alert in report.get("alerts", [])[:5]:
            lines.append(f"Alert: {escape(alert[:120])}")
        for decision in report.get("decisions", [])[:8]:
            lines.append(
                f"{escape(decision.get('action',''))} "
                f"<code>{escape(decision.get('agent_id',''))}</code>: "
                f"{escape(decision.get('reason','')[:100])}"
            )
        for insight in report.get("insights", [])[:8]:
            lines.append(f"- {escape(insight[:140])}")
        return "\n".join(lines)[:3900]

    def _llm_chat(self, user_text: str) -> str:
        context = self._chat_context()
        prompt = f"""You are the private operator assistant for a personal algorithmic trading bot fleet.

Answer the user's question using only the telemetry below.
Be direct and practical. Explain what is working, what is not working, and what to check next.
Do not claim live profitability unless the telemetry shows closed PnL.
Do not execute trades, change bot allocation, or run shell commands from free-form chat.
If the user asks for an action, tell them the exact explicit command to use.

Telemetry:
{context}

User question:
{user_text}

Respond in concise Telegram-friendly text, under 1200 characters."""

        try:
            if config.groq_api_key:
                text = self._call_groq_chat(prompt)
            elif config.anthropic_api_key:
                text = self._call_anthropic_chat(prompt)
            else:
                text = self._fallback_chat_answer(user_text)
        except Exception as e:
            logger.warning(f"TelegramControl | LLM chat failed: {e}")
            text = self._fallback_chat_answer(user_text)

        return escape(text[:1800])

    def _chat_context(self) -> str:
        rs = self.risk.status()
        portfolio = self.journal.summary()
        metrics = sorted(
            self.registry.status_all(),
            key=lambda m: (m.get("total_pnl", 0), m.get("win_rate", 0), -m.get("error_count", 0)),
            reverse=True,
        )
        open_trades = self.journal.open_trades()
        decisions = self.registry.recent_decisions(8)

        bot_lines = []
        for m in metrics[:15]:
            bot_lines.append(
                f"{m.get('agent_id')} market={m.get('market')} status={m.get('status')} "
                f"trades={m.get('trades',0)} pnl={m.get('total_pnl',0)} "
                f"win={m.get('win_rate',0)} ai_mult={m.get('head_ai_mult',1.0)} "
                f"errors={m.get('error_count',0)} last_error={m.get('last_error')}"
            )

        trade_lines = [
            f"{t.agent_id} {t.symbol} {t.direction} qty={t.quantity:g} risk={t.risk_amount:.2f}"
            for t in open_trades[:12]
        ]
        decision_lines = [
            f"{d.get('time')} {d.get('agent_id')} {d.get('action')} {d.get('reason')}"
            for d in decisions
        ]

        return "\n".join([
            f"mode={config.trading_mode} capital={rs['capital']} drawdown_pct={rs['drawdown_pct']} "
            f"daily_loss_pct={rs['daily_loss_pct']} open_risk={rs['open_risk']} open_positions={rs['open_positions']}",
            f"closed_trades={portfolio.get('trades',0)} total_closed_pnl={portfolio.get('total_pnl',0)} "
            f"win_rate={portfolio.get('win_rate',0)} sharpe={portfolio.get('sharpe',0)}",
            "bots:",
            *bot_lines,
            "open_trades:",
            *(trade_lines or ["none"]),
            "recent_controls:",
            *(decision_lines or ["none"]),
        ])

    def _call_groq_chat(self, prompt: str) -> str:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.groq_api_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500,
                "temperature": 0.2,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def _call_anthropic_chat(self, prompt: str) -> str:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": config.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()

    def _fallback_chat_answer(self, user_text: str) -> str:
        rs = self.risk.status()
        portfolio = self.journal.summary()
        metrics = self.registry.status_all()
        running = sum(1 for m in metrics if m.get("status") == "running")
        no_data = [m["agent_id"] for m in metrics if m.get("status") == "no_data"]
        errored = [m["agent_id"] for m in metrics if m.get("status") == "error"]
        best = max(metrics, key=lambda m: m.get("total_pnl", 0), default={})

        lines = [
            f"Current state: capital Rs{rs['capital']:,.2f}, open risk Rs{rs['open_risk']:,.2f}, "
            f"drawdown {rs['drawdown_pct']:.2f}%.",
            f"Closed PnL is Rs{portfolio.get('total_pnl', 0):+,.2f} across {portfolio.get('trades', 0)} closed trades.",
            f"{running}/{len(metrics)} bots are running. Top closed-PnL bot: {best.get('agent_id', 'none')}.",
        ]
        if no_data:
            lines.append(f"No-data bots: {', '.join(no_data[:6])}.")
        if errored:
            lines.append(f"Errored bots: {', '.join(errored[:6])}.")
        lines.append("Use /analyze for a fresh HeadAI review or /status for the full report.")
        return "\n".join(lines)

    def _runbook_command(self, args: list) -> str:
        if not self.allow_runbook:
            return "Runbook commands are disabled. Set TELEGRAM_CONTROL_ALLOW_RUNBOOK=true to enable them."
        if not args:
            return "Usage: /run test|compile|git_status|last_log"

        name = args[0].lower()
        if name == "last_log":
            return self._latest_log()

        commands = {
            "test": [sys.executable, "-m", "pytest", "-q"],
            "compile": [
                sys.executable, "-m", "compileall", "-q",
                "agents", "ai", "backtester", "bots", "broker", "config", "dashboard",
                "data", "head_ai", "journal", "markets", "registry", "resources", "risk",
                "main.py", "notify.py", "telegram_control.py", "reporting_agent.py", "tax.py", "tests",
            ],
            "git_status": ["git", "-c", f"safe.directory={self.root.parent.as_posix()}", "status", "--short"],
        }
        command = commands.get(name)
        if not command:
            return "Unknown runbook command. Use /run test|compile|git_status|last_log"

        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=120,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return f"<b>Runbook</b> <code>{escape(name)}</code>\nTimed out after 120s."
        except Exception as e:
            return f"<b>Runbook</b> <code>{escape(name)}</code>\nFailed: <code>{escape(str(e))}</code>"

        output = (result.stdout or result.stderr or "").strip() or "(no output)"
        return (
            f"<b>Runbook</b> <code>{escape(name)}</code> exit={result.returncode}\n"
            f"<pre>{escape(output[-3400:])}</pre>"
        )[:3900]

    def _latest_log(self) -> str:
        log_dir = self.root / "logs"
        files = sorted(log_dir.glob("ultimate_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return "No log files found."
        path = files[0]
        try:
            lines = path.read_text(errors="replace").splitlines()[-60:]
        except Exception as e:
            return f"Could not read latest log: <code>{escape(str(e))}</code>"
        output = "\n".join(lines) or "(empty log)"
        return f"<b>Latest log</b> <code>{escape(path.name)}</code>\n<pre>{escape(output[-3400:])}</pre>"[:3900]

    def _pause(self, args: list) -> str:
        if not args:
            return "Usage: /pause BOT [hours]"
        bot = args[0]
        hours = float(args[1]) if len(args) > 1 else 24.0
        ok = self.registry.suspend_bot(bot, "telegram command", hours)
        return f"{'Suspended' if ok else 'Unknown bot'}: <code>{escape(bot)}</code>"

    def _resume(self, args: list) -> str:
        if not args:
            return "Usage: /resume BOT"
        bot = args[0]
        ok = self.registry.resume_bot(bot)
        return f"{'Resumed' if ok else 'Unknown bot'}: <code>{escape(bot)}</code>"

    def _reduce(self, args: list) -> str:
        if not args:
            return "Usage: /reduce BOT [mult]"
        bot = args[0]
        mult = float(args[1]) if len(args) > 1 else 0.5
        ok = self.registry.set_ai_mult(bot, mult, "telegram command")
        return f"{'Reduced' if ok else 'Unknown bot'}: <code>{escape(bot)}</code> mult={mult:.2f}"

    def _boost(self, args: list) -> str:
        if not args:
            return "Usage: /boost BOT"
        bot = args[0]
        ok = self.registry.set_ai_mult(bot, 1.0, "telegram command")
        return f"{'Boosted' if ok else 'Unknown bot'}: <code>{escape(bot)}</code>"

    def _send(self, message: str):
        requests.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": message[:3900],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
