# Documenting Changes for Windows

**Purpose:** a porting guide so the **Windows app** (`Itsme23476/App-interface`) can replay
every change we make in the **Mac app** (`Itsme23476/Mac-version`), the *correct* way.

**How to use (fresh Claude Code on the Windows repo):**
> Read the entire `Documenting-Changes-for-Windows/` folder first. It describes, per feature,
> what we changed in the Mac app, what is **shared backend** (already done — do NOT redeploy),
> and the **Windows-specific** adjustments. Apply the same changes to this Windows repo.

**Rules for this folder (for the Mac-side Claude that maintains it):**
- Document only the **final, corrected** approach. If something we tried turned out wrong and
  we fixed it, replace the entry with the corrected version and add a one-line "❌ pitfall" note
  so Windows doesn't repeat the mistake.
- Note clearly what is **shared** (Supabase backend, the same project serves both apps) vs
  **per-platform** (client code, build config, permissions).
- Keep exact identifiers (endpoints, model names, function names, settings keys) so they can be
  copied verbatim.

---

## Shared infrastructure (do this ONCE, already done from the Mac side)

Both apps use the **same Supabase project**: `gsvccxhdgcshiwgjvgfi`.
Anything deployed there is **already live for Windows too** — the Windows client just calls it.

| Resource | Status | Windows action |
|---|---|---|
| `transcribe` edge function (Grok Voice Transcribe 2.0) | Deployed (verify_jwt=false) | None — call the same URL |
| `voice_usage` table (fair-use logging) | Created | None |
| `XAI_API_KEY` secret | Set in Supabase | None |
| `get_entitlement` RPC | Exists | None |

So for a feature whose backend is shared, the Windows work is **client-side only**.

---

## Feature guides

| # | Feature | Guide | Mac status |
|---|---------|-------|------------|
| 1 | Voice dictation (Grok Voice Transcribe 2.0) | [voice-dictation.md](voice-dictation.md) | Phases 0–2 done; 3–5 pending |

---

## Global gotchas (apply to everything)

- **"Grok" ≠ "Groq".** We use **Grok** = xAI (grok.com), model `grok-voice-transcribe-2.0`, endpoint
  `https://api.x.ai/v1/stt`. **Groq** (groq.com) is a different company (fast Whisper hosting) we evaluated but did not choose as default.
- **Run Qt tests headless:** `QT_QPA_PLATFORM=offscreen python -m pytest ...` — otherwise a Qt
  test can hang trying to reach the window server.
- **Use the full venv** with `numpy`, `scipy`, `sounddevice`, `requests`, `PySide6` for tests/runtime.
- **Never commit secrets.** API keys live in Supabase secrets (server) or a gitignored `.env` (local test only).
- **Don't push to GitHub unless the user explicitly says so.**
