"""
Mood, emotion, and trust logic for Cowai bot (Maicé).
Handles global emotion state, per-user trust, and persona style.

This module combines:
- Affect/mood engine (valence/arousal/dominance, with decay and inertia)
- Persistent per-user trust system (with event log and style guidance)
"""

from __future__ import annotations

import math
import time
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple
from utils.helpers import clamp, now_ts
from utils.errors import CowaiError, log_error

# -------------------------
# Affect/Mood Engine
# -------------------------

@dataclass
class Affect:
	valence: float = 0.0    # unpleasant (-) <-> pleasant (+)
	arousal: float = 0.0    # calm (-) <-> activated (+)
	dominance: float = 0.0  # powerless (-) <-> in-control (+)

class EmotionEngine:
	def __init__(self):
		self._emotion = Affect()
		self._baseline = Affect()
		self._last_update = time.time()
		self._guidance: Dict[str, str] = {
			"furious": "Very annoyed. Short, blunt replies. Avoid emojis.",
			"irritated": "Irritated and impatient. Keep it brief; avoid rambling.",
			"tense": "Slightly tense. Be direct and helpful; de-escalate.",
			"cold": "Colder and more distant. Dry tone, minimal fluff.",
			"neutral": "Calm and neutral. Clear and direct.",
			"calm": "Calm and steady. Helpful and grounded.",
			"friendly": "Friendly and engaged. Light warmth.",
			"upbeat": "Upbeat and expressive. A little playful is OK.",
			"playful": "Playful and energetic. Emojis are OK, but don't spam.",
		}

	@staticmethod
	def _clampf(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
		return max(lo, min(hi, float(x)))

	def _clamp_affect(self, a: Affect) -> None:
		a.valence = self._clampf(a.valence)
		a.arousal = self._clampf(a.arousal)
		a.dominance = self._clampf(a.dominance)

	@staticmethod
	def _mix(current: float, target: float, alpha: float) -> float:
		alpha = max(0.0, min(1.0, float(alpha)))
		return (1.0 - alpha) * current + alpha * target

	def apply(self, delta: int | Dict[str, Any]) -> None:
		self.decay()
		if isinstance(delta, dict):
			dv = float(delta.get("valence", 0.0) or 0.0)
			da = float(delta.get("arousal", 0.0) or 0.0)
			dd = float(delta.get("dominance", 0.0) or 0.0)
			conf = float(delta.get("confidence", 1.0) or 1.0)
			conf = max(0.0, min(1.0, conf))
		else:
			n = float(int(delta))
			dv = n / 3.0
			da = min(0.35, abs(n) / 6.0)
			dd = 0.05 if n > 0 else (-0.05 if n < 0 else 0.0)
			conf = 1.0
		alpha = 0.45 * conf + 0.10
		self._emotion.valence = self._mix(self._emotion.valence, self._emotion.valence + dv, alpha)
		self._emotion.arousal = self._mix(self._emotion.arousal, self._emotion.arousal + da, alpha)
		self._emotion.dominance = self._mix(self._emotion.dominance, self._emotion.dominance + dd, alpha)
		self._clamp_affect(self._emotion)
		base_alpha = 0.04 * conf
		self._baseline.valence = self._mix(self._baseline.valence, self._emotion.valence, base_alpha)
		self._baseline.arousal = self._mix(self._baseline.arousal, self._emotion.arousal, base_alpha)
		self._baseline.dominance = self._mix(self._baseline.dominance, self._emotion.dominance, base_alpha)
		self._clamp_affect(self._baseline)

	def set(self, value: int) -> None:
		v = max(-3, min(3, int(value)))
		self._baseline.valence = v / 3.0
		self._baseline.arousal = 0.15 if v != 0 else 0.0
		self._baseline.dominance = 0.0
		self._emotion = Affect()
		self._clamp_affect(self._baseline)

	def reset(self) -> None:
		self._emotion = Affect()
		self._baseline = Affect()
		self._last_update = time.time()

	def decay(self, step: int = 1) -> None:
		now = time.time()
		dt = max(0.0, now - self._last_update)
		self._last_update = now
		step = max(1, int(step))
		k_fast = 1.2 * step
		k_slow = 0.25 * step
		fast_factor = math.exp(-k_fast * dt / 30.0)
		slow_factor = math.exp(-k_slow * dt / 300.0)
		self._emotion.valence *= fast_factor
		self._emotion.arousal *= fast_factor
		self._emotion.dominance *= fast_factor
		self._baseline.valence *= slow_factor
		self._baseline.arousal *= slow_factor
		self._baseline.dominance *= slow_factor
		self._clamp_affect(self._emotion)
		self._clamp_affect(self._baseline)

	def _overall_valence(self) -> float:
		return self._clampf(self._baseline.valence + 0.65 * self._emotion.valence)

	def _overall_arousal(self) -> float:
		return self._clampf(self._baseline.arousal + 0.75 * self._emotion.arousal)

	def value(self) -> int:
		v = self._overall_valence()
		return int(round(3.0 * v))

	def to_int(self) -> int:
		return self.value()

	@property
	def mood(self) -> int:
		return self.value()

	def label(self) -> str:
		v = self._overall_valence()
		a = self._overall_arousal()
		if v <= -0.80 and a >= 0.40:
			return "furious"
		if v <= -0.55 and a >= 0.25:
			return "irritated"
		if v <= -0.25 and a >= 0.15:
			return "tense"
		if v <= -0.25 and a < 0.15:
			return "cold"
		if v >= 0.75 and a >= 0.25:
			return "playful"
		if v >= 0.45 and a >= 0.20:
			return "upbeat"
		if v >= 0.25 and a < 0.20:
			return "friendly"
		if abs(v) < 0.18 and a < 0.10:
			return "neutral"
		if a < 0.10:
			return "calm"
		return "neutral"

	def description(self) -> str:
		label = self.label()
		guidance = self._guidance.get(label, self._guidance["neutral"])
		v = self._overall_valence()
		a = self._overall_arousal()
		d = self._clampf(self._baseline.dominance + 0.50 * self._emotion.dominance)
		return (
			f"Mood: {label}. Guidance: {guidance} "
			f"(valence={v:+.2f}, arousal={a:+.2f}, dominance={d:+.2f})"
		)

	def metrics(self) -> Dict[str, float]:
		return {
			"valence": self._overall_valence(),
			"arousal": self._overall_arousal(),
			"dominance": self._clampf(self._baseline.dominance + 0.50 * self._emotion.dominance),
			"baseline_valence": self._baseline.valence,
			"baseline_arousal": self._baseline.arousal,
			"baseline_dominance": self._baseline.dominance,
			"emotion_valence": self._emotion.valence,
			"emotion_arousal": self._emotion.arousal,
			"emotion_dominance": self._emotion.dominance,
		}

# Global singleton for emotion
emotion = EmotionEngine()

# -------------------------
# Trust System
# -------------------------

@dataclass(frozen=True)
class TrustStyle:
	score: float
	relax: float
	mood_multiplier: float

class TrustStore:
	def __init__(self, db_path: str = "memory/trust.db"):
		self.db_path = str(db_path)
		parent = Path(self.db_path).parent
		if str(parent) != ".":
			parent.mkdir(parents=True, exist_ok=True)
		self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
		self.conn.row_factory = sqlite3.Row
		self._lock = threading.Lock()
		self._init_schema()

	def _init_schema(self) -> None:
		try:
			with self._lock:
				cur = self.conn.cursor()
				cur.execute(
					"""
					CREATE TABLE IF NOT EXISTS trust (
						user_id TEXT PRIMARY KEY,
						score REAL NOT NULL,
						updated_ts INTEGER NOT NULL
					)
					"""
				)
				cur.execute(
					"""
					CREATE TABLE IF NOT EXISTS trust_events (
						id INTEGER PRIMARY KEY AUTOINCREMENT,
						user_id TEXT NOT NULL,
						ts INTEGER NOT NULL,
						delta REAL NOT NULL,
						reason TEXT NOT NULL
					)
					"""
				)
				cur.execute(
					"CREATE INDEX IF NOT EXISTS idx_trust_events_user_ts ON trust_events(user_id, ts DESC)"
				)
				self.conn.commit()
		except Exception as exc:
			log_error("Failed to initialize trust schema.", exc)

	def get_score(self, user_id: int, default: float = 0.50) -> float:
		uid = str(int(user_id))
		try:
			with self._lock:
				cur = self.conn.cursor()
				cur.execute("SELECT score FROM trust WHERE user_id=?", (uid,))
				row = cur.fetchone()
			if not row:
				return float(clamp(default, 0.0, 1.0))
			return float(clamp(row["score"], 0.0, 1.0))
		except Exception as exc:
			log_error("Failed to get trust score.", exc)
			return float(clamp(default, 0.0, 1.0))

	def set_score(self, user_id: int, score: float, *, reason: str = "manual set") -> float:
		uid = str(int(user_id))
		score = float(clamp(score, 0.0, 1.0))
		now = now_ts()
		with self._lock:
			cur = self.conn.cursor()
			cur.execute(
				"""
				INSERT INTO trust(user_id, score, updated_ts)
				VALUES(?, ?, ?)
				ON CONFLICT(user_id) DO UPDATE SET
				  score=excluded.score,
				  updated_ts=excluded.updated_ts
				""",
				(uid, score, now),
			)
			cur.execute(
				"INSERT INTO trust_events(user_id, ts, delta, reason) VALUES(?, ?, ?, ?)",
				(uid, now, 0.0, str(reason)[:500]),
			)
			self.conn.commit()
		return score

	def add(self, user_id: int, delta: float, *, reason: str) -> float:
		delta = float(delta)
		current = self.get_score(user_id)
		new_score = float(clamp(current + delta, 0.0, 1.0))
		uid = str(int(user_id))
		now = now_ts()
		with self._lock:
			cur = self.conn.cursor()
			cur.execute(
				"""
				INSERT INTO trust(user_id, score, updated_ts)
				VALUES(?, ?, ?)
				ON CONFLICT(user_id) DO UPDATE SET
				  score=excluded.score,
				  updated_ts=excluded.updated_ts
				""",
				(uid, new_score, now),
			)
			cur.execute(
				"INSERT INTO trust_events(user_id, ts, delta, reason) VALUES(?, ?, ?, ?)",
				(uid, now, float(delta), str(reason)[:500]),
			)
			self.conn.commit()
		return new_score

	def recent_events(self, user_id: int, limit: int = 6) -> List[Tuple[int, float, str]]:
		uid = str(int(user_id))
		limit = max(1, min(int(limit), 20))
		with self._lock:
			cur = self.conn.cursor()
			cur.execute(
				"SELECT ts, delta, reason FROM trust_events WHERE user_id=? ORDER BY ts DESC LIMIT ?",
				(uid, limit),
			)
			rows = cur.fetchall()
		out: List[Tuple[int, float, str]] = []
		for r in rows:
			try:
				out.append((int(r["ts"]), float(r["delta"]), str(r["reason"])) )
			except Exception:
				continue
		return out

	def style(self, user_id: int) -> TrustStyle:
		score = self.get_score(user_id)
		relax = float(clamp((score - 0.20) / 0.80, 0.0, 1.0))
		mood_multiplier = float(clamp(1.0 + (score - 0.50) * 1.2, 0.6, 1.8))
		return TrustStyle(score=score, relax=relax, mood_multiplier=mood_multiplier)

	def prompt_block(self, user_id: int) -> str:
		s = self.style(user_id)
		if s.relax >= 0.70:
			vibe = "high"
		elif s.relax >= 0.35:
			vibe = "medium"
		else:
			vibe = "low"
		return (
			"User trust context (internal):\n"
			f"- Trust score: {s.score:.2f} / 1.00\n"
			f"- Relax level: {vibe} (be more casual/expressive as trust increases)\n"
			"Guidance:\n"
			"- Higher trust: be more relaxed, playful, and emotionally expressive; you may use light humor and be less formal.\n"
			"- Lower trust: keep a more neutral, professional tone; avoid overly personal assumptions.\n"
			"- Always follow safety rules and avoid disallowed content."
		).strip()

# Global singleton for trust
trust = TrustStore()
