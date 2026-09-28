# Auth session + sign-out fixes — port notes

Bugs we hit on Mac and how we fixed them. Read before wiring auth / sign-out on Windows.
Two are **cross-platform** (do them on Windows too); one is **Mac-only** (skip on Windows,
but know why, because the *lesson* changes the sign-out UX).

---

## 1. Auth token goes stale mid-session → 401 "session expired"  ← CROSS-PLATFORM, fix on Windows

**Symptom:** voice dictation / voice search / vision worked for a while, then started failing
with "Your session expired — please sign in again" even though the user never signed out. On
the next app launch: `Invalid Refresh Token: Already Used`, forcing a re-login.

**Root cause:** the Supabase gotrue client **rotates the access + refresh token in the
background**. The app had cached the token in several places that never got updated after a
rotation:
- an in-memory `self._access_token` on the auth object, used as the bearer token, and
- the on-disk tokens (`settings.set_auth_tokens`), saved only at sign-in/restore.

So after the first background rotation, every request sent a **dead** token → 401; and the
stored refresh token was the **old** one → "Already Used" on restart.

**Fix (Mac — `app/core/supabase_client.py`):**
- New `get_access_token()` — returns the **live** token from `self._auth_client.get_session()`
  (gotrue auto-refreshes it when near expiry), then syncs the in-memory + on-disk copies.
  Falls back to the cached token if the live fetch fails (never hard-crashes).
- Registered `on_auth_state_change` → on `TOKEN_REFRESHED` / `SIGNED_IN` / `USER_UPDATED`,
  persist the rotated tokens to disk immediately.
- Routed **every** bearer-token reader through `get_access_token()`: `transcribe_audio` /
  `distill_search_query` (`core/transcription.py`), vision (`core/vision.py`), the PostgREST
  client (`_get_db_client`), and the telemetry `track()` helper.

❌ **Pitfall:** don't read a cached `self._access_token` for API calls — it's stale the moment
gotrue rotates. Always source the token from the live session object.

**Windows action:** the Windows app uses the **same** supabase client pattern. Apply the same
change: add a `get_access_token()` that pulls from the live gotrue session, persist on
`on_auth_state_change`, and route all token readers through it. Same Supabase project
(`gsvccxhdgcshiwgjvgfi`); backend unchanged.

---

## 2. Sign-out froze the whole app  ← MAC-ONLY cause; Windows keeps a normal dialog

**Symptom (Mac):** clicking **Sign Out** froze the entire app — you couldn't click anything.
Sometimes it worked, then after signing in again it froze again (intermittent).

**Root cause (Mac-specific):** the app is a menu-bar **agent app** (`LSUIElement=True`). A modal
confirm dialog (`QDialog.exec()`) opened on a **different macOS Space** than the one the user
was viewing — especially after the Google-login browser flow left the user on a **secondary /
full-screen Space**. The dialog was visible + key **internally** (`isVisible=True`,
`isKeyWindow=True`) but `isOnActiveSpace=False`, so the user never saw it, while its modal
event loop blocked the main thread → total freeze. Proven with per-window logging
(`onActiveSpace` stayed `False` on Space 680).

**What did NOT work (so Windows/Mac don't retry):**
- ❌ `raise_()` / `activateWindow()` / `NSApp.activateIgnoringOtherApps_` — activates the app but
  does not move a window across Spaces.
- ❌ Private `CGSAddWindowsToSpaces` — returns an error on macOS 26 (`-1342177280`).
- ❌ `setCollectionBehavior(CanJoinAllSpaces)` — works on the **primary** Space only; on a
  secondary / full-screen Space the dialog still opened off-Space.
- ❌ Making the dialog a native `QMessageBox` instead of the custom frameless one — same freeze
  (it was never the frameless-ness; it was the Space).

**Fix (Mac — `app/ui/main_window.py`):** stop using a **separate dialog window** for the confirm.
The **main window is always on the user's active Space** (they're clicking it), so the confirm
now lives **inside** it: an inline **two-click** button — "Sign Out" → "Click again to confirm"
(auto-reverts after 4 s) → then `_perform_sign_out_and_reauth()`. No modal window ⇒ nothing can
be stranded off-Space ⇒ can't freeze.

**Windows action:** Windows has **no Spaces**, so this specific freeze does not exist there — a
normal modal confirm dialog is fine on Windows. You do **not** need the inline two-click button.
Just keep a standard "Are you sure?" dialog. (Only port the *behavior* — confirm then sign out;
the Mac inline UI is a Mac workaround.)

---

## 3. macOS Spaces window-flag reference  ← MAC-ONLY, skip on Windows

If a Mac window must appear on the user's current Space (overlays, and why the login dialog now
works): set it on the `NSWindow` **before** it's ordered on screen.
- `NSWindowCollectionBehaviorCanJoinAllSpaces` = `1 << 0`
- `NSWindowCollectionBehaviorMoveToActiveSpace` = `1 << 1`
- `NSWindowCollectionBehaviorFullScreenAuxiliary` = `1 << 8`  ← **required to show over a
  full-screen app's Space**

❌ **Pitfall:** `CanJoinAllSpaces` and `MoveToActiveSpace` are **mutually exclusive** — OR-ing
them (`(1<<0)|(1<<1)`) throws `NSInternalInconsistencyException` and sets nothing. Use
`(1<<0)|(1<<8)` (CanJoinAllSpaces | FullScreenAuxiliary). The login dialog
(`app/ui/auth_dialog.py`, in `showEvent`) does exactly this so it's reachable on any Space.

Windows: none of this applies (no Spaces / no `NSWindow`).

---

## 4. Build & environment gotchas (recap)  ← both platforms

- **Never build in a cloud-synced folder** (Mac: iCloud `~/Desktop`; Windows: OneDrive Desktop/
  Documents). Every file op goes through the sync provider and builds crawl / hang. Build from a
  non-synced path (Mac: `~/Developer/Mac_app`). Keep ≥15–20 GB free.
- **Diagnosing a "frozen" native app:** `/usr/bin/sample <pid>` (Mac) shows the main-thread
  stack — that's how we proved it was parked in `QDialog::exec()` waiting for input, not a
  network hang. Windows equivalent: attach a debugger / capture a stack dump.
- **Debugging via logs:** the app logs to `~/.config/ai-file-organizer/logs/ai_file_organizer.log`
  (both platforms use `settings.get_app_data_dir()`; Windows = `%APPDATA%/ai-file-organizer`).
  When a UI bug is hard to see, add temporary state logging (we logged every window's
  `onActiveSpace` / `isKeyWindow`) — that pinned the Spaces cause after several wrong guesses.
